"""GitHub transport adapter.

All network access of the core goes through one object with a small surface:
``get``, ``get_paginated``, ``get_text`` and ``request``. Every method returns
``(data, error)`` instead of raising, so callers can report partial failures.

The default implementation shells out to the GitHub CLI (``gh api``). The CLI
owns authentication; the core never stores a token. Tests and plugins can pass
any object with the same methods (for example an in-memory fake).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import re
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .coordination import Coordinator, Hold
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

API_VERSION = "2026-03-10"
# Explicit, definitive client rejection classes. In particular, HTTP 408,
# nonstandard errors and all 5xx replies do not prove the request's outcome.
_DEFINITE_CLIENT_REJECTIONS = frozenset({400, 401, 404, 405, 409, 410, 412, 413, 414, 415, 422})


class Transport(Protocol):
    def get(self, endpoint: str) -> tuple[Any, Optional[str]]: ...

    def get_paginated(self, endpoint: str) -> tuple[list, Optional[str]]: ...

    def get_text(self, endpoint: str, accept: str) -> tuple[str, Optional[str]]: ...

    def request(self, method: str, endpoint: str, body: Optional[dict] = None) -> tuple[Any, Optional[str]]: ...


@dataclass(frozen=True)
class _Response:
    body: str
    error: Optional[str]
    headers: Mapping[str, str]
    state_confirmed: bool = False


class GhCliTransport:
    """``gh api`` based transport. Authentication stays with the GitHub CLI."""

    def __init__(self, gh: str = "gh", timeout: int = 60, runner: Optional[Callable] = None, coordinator=None):
        self.gh = gh
        self.timeout = timeout
        self._runner = runner
        self.coordinator = coordinator or Coordinator()

    def _invoke(
        self,
        args: list[str],
        stdin: Optional[str] = None,
        *,
        env: Optional[dict[str, str]] = None,
        generic_errors: bool = False,
        before_effect=None,
    ) -> tuple[str, Optional[str]]:
        response = self._invoke_response(
            args, stdin, env=env, generic_errors=generic_errors, before_effect=before_effect
        )
        return response.body, response.error

    def _invoke_response(self, args, stdin=None, *, env=None, generic_errors=False, before_effect=None):
        request_attempted = False
        # One persistent host-wide guard, shared across accounts. No retry here.
        mutation = "--method" in args and args[args.index("--method") + 1].upper() != "GET"
        if mutation and "graphql" in args and stdin:
            # Preserve the existing fixed merge readback query without treating
            # arbitrary GraphQL POSTs as reads or parsing a general query language.
            from .typed_actions import MERGE_READBACK_QUERY

            try:
                payload = json.loads(stdin)
                if isinstance(payload, dict) and payload.get("query") == MERGE_READBACK_QUERY:
                    mutation = False
            except (ValueError, TypeError):
                pass
        try:
            with self.coordinator.lock("github-com-transport"):
                request_id = self.coordinator.begin_request(mutation)
                if mutation and before_effect is not None:
                    before_effect()
                request_attempted = True
                response = self._invoke_once(args, stdin, env=env, generic_errors=generic_errors)
                if response.state_confirmed:
                    self.coordinator.finish_request(request_id)
                return response
        except (Hold, OSError):
            message = (
                "GitHub coordination HOLD after request attempt; outcome unconfirmed"
                if request_attempted
                else "GitHub coordination HOLD; no request sent"
            )
            return _Response("", message, MappingProxyType({}))

    def _invoke_once(self, args, stdin=None, *, env=None, generic_errors=False):
        cmd = [
            self.gh,
            "api",
            "--hostname",
            "github.com",
            "--include",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            *args,
        ]
        run = self._runner or subprocess.run
        headers = {}

        def response(body, error, state_confirmed=False):
            return _Response(body, error, MappingProxyType(dict(headers)), state_confirmed)

        try:
            result = run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                **({"env": env} if env is not None else {}),
            )
        except subprocess.TimeoutExpired:
            return response("", "GitHub request failed" if generic_errors else "Timeout; outcome unconfirmed")
        except Exception:
            return response("", "GitHub request failed")
        stdout = result.stdout or ""
        status = None
        if stdout.startswith("HTTP/"):
            header, separator, stdout = stdout.replace("\r\n", "\n").partition("\n\n")
            first, *lines = header.splitlines()
            match = re.fullmatch(r"HTTP/[0-9.]+ ([0-9]{3})(?: .*)?", first)
            if not separator or not match:
                return response("", "GitHub response headers unreadable; outcome unconfirmed")
            status = int(match[1])
            for line in lines:
                key, colon, value = line.partition(":")
                if colon and key.lower() in {"retry-after", "x-ratelimit-remaining", "x-ratelimit-reset", "link"}:
                    if key.lower() in headers:
                        return response("", "GitHub response headers ambiguous; outcome unconfirmed")
                    headers[key.lower()] = value.strip()
        elif self._runner is None and "--method" in args:
            return response("", "GitHub response headers missing; outcome unconfirmed")
        diagnostic = result.stderr or ""
        # CLI error channel only; never infer a limit from arbitrary response bodies.
        if status is None and result.returncode:
            match = re.search(r"(?:\(HTTP |^gh: HTTP |^HTTP/[0-9.]+ )(403|429)\b", diagnostic, re.I | re.M)
            status = int(match[1]) if match else None
        rate = status == 429 or bool(re.search(r"rate[ -]+limit|abuse detection|retry-after", diagnostic, re.I))
        rate = rate or (status == 403 and headers.get("x-ratelimit-remaining") == "0")
        exhausted = headers.get("x-ratelimit-remaining") == "0"
        if rate or exhausted:
            seconds = 60.0
            try:
                retry = headers.get("retry-after")
                if retry is not None:
                    seconds = (
                        float(retry)
                        if retry.isdigit()
                        else parsedate_to_datetime(retry).timestamp() - self.coordinator.clock()
                    )
                if headers.get("x-ratelimit-remaining") == "0":
                    seconds = max(seconds, float(headers["x-ratelimit-reset"]) - self.coordinator.clock())
                if not 0 <= seconds < float("inf"):
                    raise ValueError
            except (ValueError, TypeError, KeyError, OverflowError):
                self.coordinator.rate_hold(policy=True)
                return response("", "GitHub rate metadata invalid; operator HOLD", True)
            self.coordinator.rate_hold(seconds=seconds)
            if rate:
                return response("", "GitHub rate HOLD; no automatic retry or account fallback", True)
        if status == 403:
            self.coordinator.rate_hold(policy=True)
            return response("", "GitHub policy HOLD; no automatic retry or account fallback", True)
        if result.returncode or (status is not None and status >= 400):
            confirmed_rejection = status in _DEFINITE_CLIENT_REJECTIONS
            detail = (
                f"GitHub request refused (HTTP {status}); action readback may be required"
                if confirmed_rejection
                else "GitHub request failed; outcome unconfirmed"
            )
            return response("", "GitHub request failed" if generic_errors else detail, confirmed_rejection)
        return response(stdout, None, True)

    def get_for_token(self, token: str, endpoint: str) -> tuple[Any, Optional[str]]:
        """Read GitHub.com with exactly this token; never use ambient auth fallback."""
        if not isinstance(token, str) or not token:
            return None, "authorized credential unavailable"
        env = os.environ.copy()
        for name in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            env.pop(name, None)
        env["GH_TOKEN"] = token
        env["GH_HOST"] = "github.com"
        try:
            text, error = self._invoke(
                ["--hostname", "github.com", "-H", "Accept: application/vnd.github+json", endpoint],
                env=env,
                generic_errors=True,
            )
        finally:
            env.pop("GH_TOKEN", None)
            env.pop("GH_HOST", None)
        if error:
            return None, error
        try:
            return json.loads(text), None
        except (TypeError, json.JSONDecodeError):
            return None, "GitHub response unreadable"

    @staticmethod
    def _json(text: str) -> tuple[Any, Optional[str]]:
        if not text.strip():
            return None, None
        try:
            return json.loads(text), None
        except json.JSONDecodeError as exc:
            return None, f"JSON error: {exc}"

    def get(self, endpoint: str) -> tuple[Any, Optional[str]]:
        text, error = self._invoke(["-H", "Accept: application/vnd.github+json", endpoint])
        return (None, error) if error else self._json(text)

    def get_text(self, endpoint: str, accept: str) -> tuple[str, Optional[str]]:
        return self._invoke(["-H", f"Accept: {accept}", endpoint])

    def get_paginated(self, endpoint: str) -> tuple[list, Optional[str]]:
        items, seen = [], set()
        while endpoint:
            if endpoint in seen or len(seen) >= 1000:
                return [], "pagination cycle or page limit; no partial result"
            seen.add(endpoint)
            response = self._invoke_response(["-H", "Accept: application/vnd.github+json", endpoint])
            text, error = response.body, response.error
            if error:
                return [], error
            page, error = parse_concatenated_json(text)
            if error:
                return [], error
            items.extend(page)
            links = re.findall(r'<([^>]+)>; rel="next"', response.headers.get("link", ""))
            if len(links) > 1:
                return [], "pagination links ambiguous"
            endpoint = links[0] if links else ""
            if endpoint:
                parsed = urlsplit(endpoint)
                if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.fragment:
                    return [], "pagination target refused"
        return items, None

    def request(self, method: str, endpoint: str, body: Optional[dict] = None) -> tuple[Any, Optional[str]]:
        return self.request_with_intent(method, endpoint, body)

    def request_with_intent(self, method, endpoint, body=None, before_effect=None):
        args = ["--method", method.upper(), "-H", "Accept: application/vnd.github+json", endpoint]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        text, error = self._invoke(args, stdin=stdin, before_effect=before_effect)
        return (None, error) if error else self._json(text)


def parse_concatenated_json(text: str) -> tuple[list, Optional[str]]:
    """Parse the concatenated JSON documents that ``gh api --paginate`` prints."""
    text = (text or "").strip()
    decoder = json.JSONDecoder()
    items: list = []
    idx = 0
    try:
        while idx < len(text):
            while idx < len(text) and text[idx].isspace():
                idx += 1
            if idx >= len(text):
                break
            obj, idx = decoder.raw_decode(text, idx)
            if isinstance(obj, list):
                items.extend(obj)
            elif isinstance(obj, dict) and "items" in obj:
                items.extend(obj.get("items") or [])
            else:
                items.append(obj)
    except json.JSONDecodeError as exc:
        return [], f"JSON error: {exc}"
    return items, None


class ReadOnlyTransport:
    """Wraps a transport and refuses writes. Handed to plugins during planning."""

    def __init__(self, inner: Transport):
        self._inner = inner

    def get(self, endpoint: str):
        return self._inner.get(endpoint)

    def get_paginated(self, endpoint: str):
        return self._inner.get_paginated(endpoint)

    def get_text(self, endpoint: str, accept: str):
        return self._inner.get_text(endpoint, accept)

    def request(self, method: str, endpoint: str, body: Optional[dict] = None):
        if method.upper() != "GET":
            return None, "read-only transport: writes are only possible through an applied action plan"
        return self._inner.get(endpoint)
