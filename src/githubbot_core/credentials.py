"""Credential adapter: tokens are looked up at runtime, never stored or bundled.

Lookup order (first hit wins): environment variables ``GH_TOKEN`` and
``GITHUB_TOKEN``, then ``gh auth token``. A deployment can pass its own
provider chain, for example a secret manager adapter. Whatever is found is
only kept in memory for the current call and is redacted from any text that
leaves the process through :func:`redact`.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Iterable, Optional, Protocol

ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")


class CredentialUnavailable(RuntimeError):
    """No provider produced a token."""


class TokenProvider(Protocol):
    name: str

    def token(self) -> Optional[str]: ...


class EnvTokenProvider:
    name = "env"

    def __init__(self, names: Iterable[str] = ENV_NAMES, environ: Optional[dict] = None):
        self.names = tuple(names)
        self.environ = os.environ if environ is None else environ

    def token(self) -> Optional[str]:
        for name in self.names:
            value = (self.environ.get(name) or "").strip()
            if value:
                return value
        return None


class GhCliTokenProvider:
    name = "gh-cli"

    def __init__(self, gh: str = "gh", runner: Optional[Callable] = None):
        self.gh = gh
        self._runner = runner

    def token(self) -> Optional[str]:
        run = self._runner or subprocess.run
        try:
            result = run(
                [self.gh, "auth", "token"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = (result.stdout or "").strip()
        return value if result.returncode == 0 and value else None


def default_providers() -> list[TokenProvider]:
    return [EnvTokenProvider(), GhCliTokenProvider()]


def resolve_token(providers: Optional[Iterable[TokenProvider]] = None) -> tuple[str, str]:
    """Return ``(token, provider_name)`` or raise :class:`CredentialUnavailable`."""
    chain = list(providers) if providers is not None else default_providers()
    for provider in chain:
        value = provider.token()
        if value:
            return value, provider.name
    names = ", ".join(p.name for p in chain) or "none"
    raise CredentialUnavailable(f"no GitHub token available (providers tried: {names})")


def redact(text: str, *secrets: Optional[str]) -> str:
    """Replace every given secret in ``text`` with ``***``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
