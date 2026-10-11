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


def _exec_add_labels(transport, params: dict):
    return transport.request(
        "POST", f"repos/{params['repo']}/issues/{int(params['number'])}/labels", {"labels": list(params["labels"])}
    )


def _exec_comment(transport, params: dict):
    return transport.request(
        "POST", f"repos/{params['repo']}/issues/{int(params['number'])}/comments", {"body": str(params["body"])}
    )


def _exec_api_request(transport, params: dict):
    return transport.request(str(params["method"]).upper(), str(params["endpoint"]).lstrip("/"), params.get("body"))


EXECUTORS = {
    "issue.add_labels": _exec_add_labels,
    "issue.comment": _exec_comment,
    "api.request": _exec_api_request,
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
            _, error = EXECUTORS[action.kind](transport, action.params)
            writes += 1
            record.update(status="failed" if error else "applied", detail=error or "")
        receipts.append(record)
        if receipt_store is not None:
            receipt_store.parent.mkdir(parents=True, exist_ok=True)
            with receipt_store.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"mode": "apply", "plan_sha256": digest, "receipts": receipts, "writes": writes}
