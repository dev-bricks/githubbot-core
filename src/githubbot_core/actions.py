"""Preview/apply contract for every write the core performs.

Nothing in the core writes to GitHub directly. Features and plugins produce a
list of :class:`Action` objects (a plan). ``preview`` shows the plan and writes
nothing; ``apply`` executes it, but only actions whose kind is enabled by the
configuration and inside the permission ceiling. Each executed action leaves a
receipt; with a receipt store, an action that was already applied is skipped,
so a repeated run never repeats a write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import typed_actions

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class Action:
    kind: str
    params: dict = field(default_factory=dict)
    reason: str = ""
    source: str = "core"

    @property
    def action_id(self) -> str:
        canonical = json.dumps({"kind": self.kind, "params": self.params}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {"id": self.action_id, **asdict(self)}


def plan_digest(actions: list[Action]) -> str:
    joined = "\n".join(action.action_id for action in actions)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def denial_reason(action: Action, config: dict) -> Optional[str]:
    """Return why ``action`` may not be applied, or ``None`` if it is permitted."""
    permissions = config.get("permissions") or {}
    if action.kind not in (permissions.get("ceiling") or []):
        return f"{action.kind} is outside the permission ceiling"
    if action.kind not in (permissions.get("enabled") or []):
        return f"{action.kind} is not enabled in permissions.enabled"
    if action.kind not in EXECUTORS:
        return f"{action.kind} has no executor"
    reason = typed_actions.switch_denial(action.kind, config) or typed_actions.param_denial(
        action.kind, action.params, config
    )
    if reason:
        return reason
    if action.kind == "api.request":
        method = str(action.params.get("method", "")).upper()
        endpoint = str(action.params.get("endpoint", "")).lstrip("/")
        prefixes = permissions.get("api_write_prefixes") or []
        if method not in WRITE_METHODS:
            return f"api.request method {method or '?'} is not a write method"
        if ".." in endpoint or not any(endpoint.startswith(p) for p in prefixes):
            return f"api.request endpoint {endpoint!r} is not covered by permissions.api_write_prefixes"
    return None


def preview(actions: list[Action], config: dict) -> dict:
    items = []
    for action in actions:
        reason = denial_reason(action, config)
        items.append({**action.to_dict(), "permitted": reason is None, "denied_reason": reason})
    return {"mode": "preview", "plan_sha256": plan_digest(actions), "actions": items, "writes": 0}


def _exec_add_labels(transport, params: dict, config: dict):
    if "multi_bot" not in config:
        return transport.request(
            "POST",
            f"repos/{params['repo']}/issues/{int(params['number'])}/labels",
            {"labels": list(params["labels"])},
        )
    _, error = _require_multibot_identity(transport, config)
    if error:
        return None, error
    _, error = transport.request(
        "POST", f"repos/{params['repo']}/issues/{int(params['number'])}/labels", {"labels": list(params["labels"])}
    )
    if error:
        return None, error
    issue, error = transport.get(f"repos/{params['repo']}/issues/{int(params['number'])}")
    if error or not isinstance(issue, dict):
        return None, f"labels submitted, but issue readback failed: {error or 'no issue data'}"
    if issue.get("number") != int(params["number"]):
        return None, "labels submitted, but issue readback returned a different issue"
    labels = {str(label.get("name", "")) for label in issue.get("labels", []) if isinstance(label, dict)}
    requested = {str(label) for label in params["labels"]}
    if not requested <= labels:
        return None, "labels submitted, but issue readback did not confirm every requested label"
    return {"readback": {"number": int(params["number"]), "labels": sorted(requested)}}, None


def _exec_comment(transport, params: dict, config: dict):
    if "multi_bot" not in config:
        return transport.request(
            "POST",
            f"repos/{params['repo']}/issues/{int(params['number'])}/comments",
            {"body": str(params["body"])},
        )
    _, error = _require_multibot_identity(transport, config)
    if error:
        return None, error
    created, error = transport.request(
        "POST", f"repos/{params['repo']}/issues/{int(params['number'])}/comments", {"body": str(params["body"])}
    )
    if error or not isinstance(created, dict) or not str(created.get("id", "")).isdecimal():
        return None, error or "comment submitted, but create response has no numeric id"
    comment_id = int(created["id"])
    comment, error = transport.get(f"repos/{params['repo']}/issues/comments/{comment_id}")
    if error or not isinstance(comment, dict):
        return None, f"comment submitted, but comment readback failed: {error or 'no comment data'}"
    if comment.get("body") != str(params["body"]):
        return None, "comment submitted, but readback body did not match"
    if not str(comment.get("issue_url", "")).endswith(f"/issues/{int(params['number'])}"):
        return None, "comment submitted, but readback issue did not match the requested issue"
    expected_account = str((config.get("multi_bot") or {}).get("merge_account") or "").strip()
    if expected_account and str((comment.get("user") or {}).get("login", "")).casefold() != expected_account.casefold():
        return None, "comment submitted, but readback author did not match the configured account"
    return {"readback": {"comment_id": comment_id, "number": int(params["number"]), "url": comment.get("html_url")}}, None


def _require_multibot_identity(transport, config: dict):
    """Bind MULTI-BOT issue writes to its configured account; leave generic triage unchanged."""
    if "multi_bot" not in config:
        return None, None
    settings = config.get("multi_bot")
    if not isinstance(settings, dict):
        return None, "multi_bot configuration must be an object"
    expected = str(settings.get("merge_account") or "").strip()
    if not expected:
        return None, "multi_bot.merge_account is required for issue writes"
    user, error = transport.get("user")
    if error or not isinstance(user, dict) or str(user.get("login", "")).casefold() != expected.casefold():
        return None, f"identity does not match the configured MULTI-BOT account {expected!r}"
    return user, None


def _exec_api_request(transport, params: dict, config: dict):
    return transport.request(str(params["method"]).upper(), str(params["endpoint"]).lstrip("/"), params.get("body"))


EXECUTORS = {
    "issue.add_labels": _exec_add_labels,
    "issue.comment": _exec_comment,
    "api.request": _exec_api_request,
    **typed_actions.EXECUTORS,
}


def _load_applied(store: Optional[Path]) -> set[str]:
    if store is None or not store.is_file():
        return set()
    applied = set()
    for line in store.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("status") == "applied":
            applied.add(record.get("id"))
    return applied


def apply(
    actions: list[Action],
    transport,
    config: dict,
    receipt_store: Optional[Path] = None,
    expected_plan_sha256: Optional[str] = None,
) -> dict:
    """Execute permitted actions. Every action gets exactly one receipt."""
    digest = plan_digest(actions)
    if expected_plan_sha256 and expected_plan_sha256 != digest:
        return {
            "mode": "apply",
            "plan_sha256": digest,
            "receipts": [],
            "writes": 0,
            "error": "plan changed since preview (plan_sha256 mismatch); nothing applied",
        }
    applied_before = _load_applied(receipt_store)
    receipts: list[dict[str, Any]] = []
    writes = 0
    for action in actions:
        record: dict[str, Any] = {
            "id": action.action_id,
            "kind": action.kind,
            "source": action.source,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        reason = denial_reason(action, config)
        if reason:
            record.update(status="denied", detail=reason)
        elif action.action_id in applied_before:
            record.update(status="skipped_duplicate", detail="already applied according to the receipt store")
        else:
            data, error = EXECUTORS[action.kind](transport, action.params, config)
            writes += 1
            record.update(status="failed" if error else "applied", detail=error or "")
            if not error and isinstance(data, dict) and "readback" in data:
                record["readback"] = data["readback"]
        receipts.append(record)
        if receipt_store is not None:
            receipt_store.parent.mkdir(parents=True, exist_ok=True)
            with receipt_store.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"mode": "apply", "plan_sha256": digest, "receipts": receipts, "writes": writes}
