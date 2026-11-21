"""Generic, explicit write-owner ceiling and expected-actor contract.

No instance identities or organizations belong here. An unbound scope denies
writes; previews and reads remain available. Raw opaque targets fail closed.
"""

from __future__ import annotations

import re

OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", re.ASCII)
ACTOR = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?", re.ASCII)
REPOSITORY = re.compile(r"([^/]+)/([A-Za-z0-9_.-]+)", re.ASCII)
REPO_ACTIONS = {
    "issue.comment",
    "issue.add_labels",
    "pr.merge",
    "pr.ready",
    "pr.review",
    "pr.close",
    "pr.create",
    "git.push_branch",
    "actions.dispatch",
    "actions.rerun",
}


def normalize_owners(value):
    if not isinstance(value, list) or any(not isinstance(v, str) or not OWNER.fullmatch(v) for v in value):
        raise ValueError("permissions.write_owners must be an explicit list of ASCII owner names")
    normalized = [v.casefold() for v in value]
    if len(normalized) != len(set(normalized)):
        raise ValueError("permissions.write_owners contains duplicate normalized owners")
    return sorted(normalized)


def actor(config):
    settings = config.get("multi_bot") or {}
    value = settings.get("merge_account") if isinstance(settings, dict) else None
    if not isinstance(value, str) or not ACTOR.fullmatch(value):
        raise ValueError("writes require an explicit valid multi_bot.merge_account")
    return value.casefold()


def repository(value):
    match = REPOSITORY.fullmatch(value) if isinstance(value, str) else None
    if not match or not OWNER.fullmatch(match[1]) or match[2] in {".", ".."}:
        raise ValueError("write target must be an explicit ASCII owner/repository")
    return value.casefold()


def target(kind, params):
    if not isinstance(params, dict):
        raise ValueError("write parameters must be an object")
    if kind != "api.request":
        if kind not in REPO_ACTIONS:
            raise ValueError("write action has no supported repository target contract")
        return repository(params.get("repo"))
    endpoint = params.get("endpoint")
    method = params.get("method")
    if not isinstance(method, str) or method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("api.request method is not a write method")
    if (
        not isinstance(endpoint, str)
        or any(c in endpoint for c in "%?#{}\\")
        or ".." in endpoint
        or endpoint.startswith("//")
    ):
        raise ValueError("raw write target is ambiguous")
    star = re.fullmatch(r"/?user/starred/([^/]+/[^/]+)", endpoint)
    if star:
        if method.upper() not in {"PUT", "DELETE"}:
            raise ValueError("repository star writes require PUT or DELETE")
        return repository(star[1])
    match = re.fullmatch(r"/?repos/([^/]+/[^/]+)/(.*)", endpoint)
    if not match:
        raise ValueError("raw writes require a supported repository endpoint; opaque GraphQL is refused")
    # Do not infer authority for transfer/rename, identities hidden in payloads,
    # repository visibility, refs, checks or release publication from a prefix.
    if method.upper() != "POST" or not re.fullmatch(r"issues/[1-9][0-9]*/(?:comments|labels)", match[2]):
        raise ValueError("raw write capability unsupported; use a typed action")
    return repository(match[1])


def denial(kind, params, config):
    try:
        permissions = config.get("permissions") or {}
        owners = normalize_owners(permissions.get("write_owners"))
        repo = target(kind, params)
        if repo.split("/")[0] not in owners:
            return "write owner is outside permissions.write_owners"
        actor(config)
    except (ValueError, AttributeError) as exc:
        return str(exc)
    return None


def binding(config):
    """Canonical authorization inputs carried in every preview/apply digest."""
    permissions = config.get("permissions") or {}
    owners = permissions.get("write_owners") if isinstance(permissions, dict) else None
    try:
        owners = normalize_owners(owners)
    except ValueError:
        owners = None  # Unbound/invalid never authorizes a write.
    try:
        expected = actor(config)
    except ValueError:
        expected = None
    return {"write_owners": owners, "expected_actor": expected}


def transport_denial(transport, kind, params, config):
    """Re-read the actor and canonical repository before each executor effect."""
    try:
        expected = actor(config)
        user, error = transport.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if error or not isinstance(login, str) or login.casefold() != expected:
            return "configured write actor could not be verified"
        repo = target(kind, params)
        metadata, error = transport.get(f"repos/{repo}")
        actual = metadata.get("full_name") if isinstance(metadata, dict) else None
        if error or not isinstance(actual, str) or actual.casefold() != repo:
            return "canonical write repository could not be verified; re-plan target changes"
    except Exception:
        # Never leak transport exception text, credentials or API payloads.
        return "write authority verification unavailable"
    return None
