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
import subprocess
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

API_VERSION = "2026-03-10"


class Transport(Protocol):
    def get(self, endpoint: str) -> tuple[Any, Optional[str]]: ...

    def get_paginated(self, endpoint: str) -> tuple[list, Optional[str]]: ...

    def get_text(self, endpoint: str, accept: str) -> tuple[str, Optional[str]]: ...

    def request(self, method: str, endpoint: str, body: Optional[dict] = None) -> tuple[Any, Optional[str]]: ...


class GhCliTransport:
    """``gh api`` based transport. Authentication stays with the GitHub CLI."""

    def __init__(self, gh: str = "gh", timeout: int = 60, runner: Optional[Callable] = None):
        self.gh = gh
        self.timeout = timeout
        self._runner = runner

    def _invoke(self, args: list[str], stdin: Optional[str] = None) -> tuple[str, Optional[str]]:
        cmd = [self.gh, "api", "-H", f"X-GitHub-Api-Version: {API_VERSION}", *args]
        run = self._runner or subprocess.run
        try:
            result = run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return "", "Timeout"
        except Exception as exc:  # the transport reports, it never raises (callers collect partial errors)
            return "", f"{type(exc).__name__}: {exc}"
        if result.returncode != 0:
            detail = " ".join((result.stderr or result.stdout or "").split())
            return "", detail[:500] or f"gh api exit {result.returncode}"
        return result.stdout or "", None

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
        text, error = self._invoke(["--paginate", "-H", "Accept: application/vnd.github+json", endpoint])
        if error:
            return [], error
        return parse_concatenated_json(text)

    def request(self, method: str, endpoint: str, body: Optional[dict] = None) -> tuple[Any, Optional[str]]:
        args = ["--method", method.upper(), "-H", "Accept: application/vnd.github+json", endpoint]
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        text, error = self._invoke(args, stdin=stdin)
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
