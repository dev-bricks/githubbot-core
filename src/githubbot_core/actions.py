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
import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import stop_gate, typed_actions, write_scope
from .coordination import Coordinator, Hold

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


def plan_digest(actions: list[Action], config: Optional[dict] = None) -> str:
    joined = "\n".join(action.action_id for action in actions)
    payload = {"actions": joined, "write_authority": write_scope.binding(config or {})}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def denial_reason(action: Action, config: dict) -> Optional[str]:
    """Return why ``action`` may not be applied, or ``None`` if it is permitted."""
    permissions = config.get("permissions") or {}
    if action.kind not in (permissions.get("ceiling") or []):
        return f"{action.kind} is outside the permission ceiling"
    if action.kind not in (permissions.get("enabled") or []):
        return f"{action.kind} is not enabled in permissions.enabled"
    if action.kind not in EXECUTORS:
        return f"{action.kind} has no executor"
    reason = (
        typed_actions.switch_denial(action.kind, config)
        or typed_actions.param_denial(action.kind, action.params, config)
        or stop_gate.write_denial(action.kind, action.params, config)
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
    return write_scope.denial(action.kind, action.params, config)


def preview(actions: list[Action], config: dict) -> dict:
    actions, config = copy.deepcopy(actions), copy.deepcopy(config)
    items = []
    for action in actions:
        reason = denial_reason(action, config)
        items.append({**action.to_dict(), "permitted": reason is None, "denied_reason": reason})
    return {"mode": "preview", "plan_sha256": plan_digest(actions, config), "actions": items, "writes": 0}


def _exec_add_labels(transport, params: dict, config: dict):
    return transport.request(
        "POST", f"repos/{params['repo']}/issues/{int(params['number'])}/labels", {"labels": list(params["labels"])}
    )


def _exec_comment(transport, params: dict, config: dict):
    return transport.request(
        "POST", f"repos/{params['repo']}/issues/{int(params['number'])}/comments", {"body": str(params["body"])}
    )


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
    actions, config = copy.deepcopy(actions), copy.deepcopy(config)
    digest = plan_digest(actions, config)
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
            authority_error = write_scope.transport_denial(transport, action.kind, action.params, config)
            if authority_error:
                record.update(status="denied", detail=authority_error)
            else:
                try:
                    result = _coordinated_action(action, transport, config, digest)
                    record.update(result)
                    writes += int(result.pop("attempted", False))
                    record.pop("attempted", None)
                except (Hold, OSError):
                    record.update(
                        status="failed", detail="coordination HOLD; outcome unconfirmed; do not retry blindly"
                    )

        receipts.append(record)
        if receipt_store is not None:
            receipt_store.parent.mkdir(parents=True, exist_ok=True)
            with receipt_store.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"mode": "apply", "plan_sha256": digest, "receipts": receipts, "writes": writes}


def _confirmed_unknown(action, transport, config):
    """Conservative read-only reconciliation; unsupported effects remain HOLD."""
    p = action.params
    account = (config.get("multi_bot") or {}).get("merge_account")
    if action.kind == "pr.review":
        rows, error = transport.get_paginated(f"repos/{p['repo']}/pulls/{int(p['number'])}/reviews?per_page=100")
        if error or not isinstance(rows, list):
            return None
        if any(
            not isinstance(r, dict)
            or not isinstance(r.get("user"), dict)
            or not isinstance(r["user"].get("login"), str)
            or type(r.get("id")) is not int
            or r["id"] < 1
            or not typed_actions.SHA.fullmatch(str(r.get("commit_id") or ""))
            for r in rows
        ):
            return None
        matched = [
            r
            for r in rows
            if isinstance(r, dict)
            and (r.get("user") or {}).get("login") == account
            and r.get("commit_id") == p["sha"]
            and r.get("body") == p["body"]
            and r.get("state") == typed_actions.REVIEW_STATES[p["event"]]
            and type(r.get("id")) is int
            and r["id"] > 0
        ]
        pull, error = typed_actions._pull(transport, p["repo"], p["number"])
        if (
            len(matched) == 1
            and not error
            and pull.get("state") == "open"
            and type(pull.get("number")) is int
            and pull["number"] == int(p["number"])
            and isinstance(pull.get("head"), dict)
            and pull["head"].get("sha") == p["sha"]
        ):
            return {"review_id": matched[0]["id"], "head": p["sha"], "account": account, "state": matched[0]["state"]}
    if action.kind == "pr.merge":
        pull, error = typed_actions._pull(transport, p["repo"], p["number"])
        sha = pull.get("merge_commit_sha")
        if (
            not error
            and pull.get("merged") is True
            and type(pull.get("number")) is int
            and pull["number"] == int(p["number"])
            and isinstance(pull.get("head"), dict)
            and pull["head"].get("sha") == p["sha"]
            and typed_actions.SHA.fullmatch(str(sha or ""))
        ):
            return {"merged": True, "merge_commit_sha": sha, "head": p["sha"]}
    return None


def _coordinated_action(action, transport, config, digest):
    coordinator = getattr(transport, "coordinator", None) or Coordinator()
    # Fixed ordering: operation lock -> transport lock. The parent wrapper must
    # not retain a transport lock while launching a core child.
    with coordinator.lock("github-com-actions"):
        coordinator.check_rate()
        error = write_scope.transport_denial(transport, action.kind, action.params, config)
        if error:
            return {"status": "denied", "detail": error}
        key = coordinator.action_key(action.action_id)
        prior = coordinator.load(key)
        if prior:
            if (
                set(prior) != {"schema", "id", "plan", "status"}
                or prior.get("schema") != "githubbot-core.intent/v1"
                or not isinstance(prior.get("plan"), str)
                or len(prior["plan"]) != 64
                or any(c not in "0123456789abcdef" for c in prior["plan"])
            ):
                raise Hold("invalid action journal schema")
            if prior.get("status") == "applied" and prior.get("id") == action.action_id:
                return {"status": "skipped_duplicate", "detail": "durable action journal already applied"}
            if prior.get("status") not in {"inflight", "unknown"} or prior.get("id") != action.action_id:
                raise Hold("invalid action journal")
            if prior.get("plan") != digest:
                raise Hold("unknown action belongs to another plan or authority")
            readback = _confirmed_unknown(action, transport, config)
            if readback is None:
                return {
                    "status": "failed",
                    "detail": "unknown write HOLD; readback unavailable or ambiguous; nothing repeated",
                }
            coordinator.save(key, {**prior, "status": "applied"})
            return {
                "status": "skipped_confirmed",
                "detail": "unknown write confirmed by read-only reconciliation",
                "readback": readback,
            }
        intent = {"schema": "githubbot-core.intent/v1", "id": action.action_id, "plan": digest, "status": "inflight"}
        started = False

        def before_effect():
            nonlocal started
            if not started:
                coordinator.save(key, intent)
                started = True

        class JournalTransport:
            def __getattr__(self, name):
                return getattr(transport, name)

            def request(self, method, endpoint, body=None):
                native = getattr(transport, "request_with_intent", None)
                if native is not None:
                    return native(method, endpoint, body, before_effect=before_effect)
                before_effect()
                return transport.request(method, endpoint, body)

            def before_git_effect(self):
                before_effect()

        try:
            data, error = EXECUTORS[action.kind](JournalTransport(), action.params, config)
        except Exception:
            if started:
                coordinator.save(key, {**intent, "status": "unknown"})
            return {
                "status": "failed",
                "detail": "executor exception; unknown write HOLD"
                if started
                else "executor preflight failed; nothing sent",
                "attempted": started,
            }
        if started:
            coordinator.save(key, {**intent, "status": "unknown" if error else "applied"})

        result = {"status": "failed" if error else "applied", "detail": error or "", "attempted": started}
        if not error and isinstance(data, dict) and "readback" in data:
            result["readback"] = data["readback"]
        return result
