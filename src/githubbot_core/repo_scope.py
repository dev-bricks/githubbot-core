"""Runtime repository-scope checks for GitHub write actions.

The portable MULTI-BOT root owns ``config/orgs.json``. This module reads that
canonical policy for each review action; it deliberately keeps no copy of the
organization or repository allow/deny lists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Optional

_OWNER_OR_REPO = re.compile(r"[A-Za-z0-9_.-]+")
_FULL_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class _PolicyError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _PolicyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _normalize_exclusion(value: object, field: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise _PolicyError(f"{field} entries must be non-empty strings without surrounding whitespace")
    if value in (".", ".."):
        raise _PolicyError(f"{field} contains an invalid repository target")
    if "/" in value:
        if not _FULL_REPO.fullmatch(value):
            raise _PolicyError(f"{field} entries must be repository names or owner/repository")
        owner, name = value.split("/", maxsplit=1)
        if owner in (".", "..") or name in (".", ".."):
            raise _PolicyError(f"{field} contains an invalid repository target")
    elif not _OWNER_OR_REPO.fullmatch(value):
        raise _PolicyError(f"{field} entries must be repository names or owner/repository")
    return value.casefold()


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise _PolicyError(f"{field} must be a list")
    result = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise _PolicyError(f"{field} must contain non-empty strings without surrounding whitespace")
        result.append(item.casefold())
    return result


def _policy_path(config: dict) -> Path:
    settings = config.get("multi_bot")
    if not isinstance(settings, dict):
        raise _PolicyError("multi_bot must be an object for a formal PR review")

    raw_root = settings.get("root")
    if raw_root is None or raw_root == "":
        root = Path.cwd()
    elif isinstance(raw_root, (str, os.PathLike)):
        rendered = os.fspath(raw_root)
        if not isinstance(rendered, str) or not rendered or rendered != rendered.strip():
            raise _PolicyError("multi_bot.root must be a non-empty path")
        root = Path(rendered).expanduser()
    else:
        raise _PolicyError("multi_bot.root must be a path")

    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _PolicyError("configured MULTI-BOT root is unavailable") from exc
    if not root.is_dir():
        raise _PolicyError("configured MULTI-BOT root is not a directory")
    return root / "config" / "orgs.json"


def _load_policy(path: Path) -> tuple[dict[str, dict], list[str]]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise _PolicyError("required config/orgs.json is unavailable or unreadable") from exc
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, _PolicyError, RecursionError) as exc:
        raise _PolicyError("required config/orgs.json is invalid JSON or contains duplicate keys") from exc
    if not isinstance(document, dict):
        raise _PolicyError("config/orgs.json must contain an object")

    raw_global = document.get("global_exclude_writes")
    if "global_exclude_writes" not in document:
        raise _PolicyError("config/orgs.json must define global_exclude_writes")
    global_exclusions = [
        _normalize_exclusion(item, "global_exclude_writes")
        for item in _string_list(raw_global, "global_exclude_writes")
    ]

    raw_organizations = document.get("organizations")
    if not isinstance(raw_organizations, list) or not raw_organizations:
        raise _PolicyError("config/orgs.json organizations must be a non-empty list")

    organizations: dict[str, dict] = {}
    for index, row in enumerate(raw_organizations):
        prefix = f"organizations[{index}]"
        if not isinstance(row, dict):
            raise _PolicyError(f"{prefix} must be an object")
        name = row.get("name")
        if not isinstance(name, str) or not _OWNER_OR_REPO.fullmatch(name) or name in (".", ".."):
            raise _PolicyError(f"{prefix}.name must be a GitHub namespace")
        key = name.casefold()
        if key in organizations:
            raise _PolicyError(f"config/orgs.json contains duplicate namespace {name!r}")
        namespace_type = row.get("type")
        if namespace_type not in ("organization", "personal"):
            raise _PolicyError(f"{prefix}.type must be organization or personal")
        if type(row.get("enabled")) is not bool:
            raise _PolicyError(f"{prefix}.enabled must be a boolean")
        exclusions = []
        if "exclude_writes" in row:
            exclusions = [
                _normalize_exclusion(item, f"{prefix}.exclude_writes")
                for item in _string_list(row["exclude_writes"], f"{prefix}.exclude_writes")
            ]
        organizations[key] = {
            "name": name,
            "type": namespace_type,
            "enabled": row["enabled"],
            "exclude_writes": exclusions,
        }
    return organizations, global_exclusions


def review_write_scope_denial(repo: object, config: dict) -> Optional[str]:
    """Return a fail-closed denial for a PR review outside the canonical scope."""
    if not isinstance(config, dict):
        return "review write scope denied: action config must be an object"
    if not isinstance(repo, str) or not _FULL_REPO.fullmatch(repo):
        return "review write scope requires repo in owner/name form"

    try:
        policy_path = _policy_path(config)
        organizations, global_exclusions = _load_policy(policy_path)
    except _PolicyError as exc:
        return f"review write scope denied: {exc}"

    owner, name = repo.split("/", maxsplit=1)
    if owner in (".", "..") or name in (".", ".."):
        return "review write scope requires a GitHub owner and repository name"
    owner_key, name_key = owner.casefold(), name.casefold()
    full_key = f"{owner_key}/{name_key}"
    if name_key in global_exclusions or full_key in global_exclusions:
        return f"review write scope denies globally excluded repository {repo!r}"

    namespace = organizations.get(owner_key)
    if namespace is None:
        return f"review write scope denies unknown namespace {owner!r}"
    if namespace["type"] != "organization":
        return f"review write scope denies non-organization namespace {owner!r}"
    if not namespace["enabled"]:
        return f"review write scope denies disabled organization {owner!r}"
    if name_key in namespace["exclude_writes"] or full_key in namespace["exclude_writes"]:
        return f"review write scope denies organization-excluded repository {repo!r}"
    return None
