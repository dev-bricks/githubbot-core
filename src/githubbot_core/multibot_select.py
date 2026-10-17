"""Pure decisions from caller-supplied, evidenced exact-revision snapshots."""

from __future__ import annotations

import re

MODES = ("operator", "bot-triage", "github-maintain")
CATEGORIES = ("prs", "fixes", "policies", "care")


def _identity(row):
    name, revision = row.get("id"), row.get("revision")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,199}", name)
        or any(part in (".", "..", "") for part in re.split(r"[/#:]", name))
    ):
        raise ValueError("Invalid candidate id")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be an exact lowercase 40-character commit SHA")
    return name, revision


def _candidate(row, *, processed=False):
    if not isinstance(row, dict):
        raise ValueError("Candidates and completion receipts must be objects")
    allowed = {"id", "revision", "category", "status", "urgent"} | ({"evidence"} if processed else set())
    if set(row) - allowed or not {"id", "revision", "category", "status"} <= set(row):
        raise ValueError("Unknown or missing candidate fields")
    key = _identity(row)
    if row["category"] not in CATEGORIES or row["status"] not in ("ready", "blocked", "done"):
        raise ValueError("Unknown category or status")
    if type(row.get("urgent", False)) is not bool:
        raise ValueError("urgent must be a boolean")
    if processed and (
        row["status"] not in ("blocked", "done")
        or not isinstance(row.get("evidence"), str)
        or not row["evidence"].strip()
        or len(row["evidence"]) > 2048
    ):
        raise ValueError("Processed exact revisions require blocked/done status and evidence")
    return key


def select_work(snapshot, *, mode="operator"):
    """Read only; selecting or idling never records completion or advances alternation."""
    if mode not in MODES:
        raise ValueError("Unknown selector mode")
    if not isinstance(snapshot, dict) or set(snapshot) - {"candidates", "processed", "lastMaintenance"}:
        raise ValueError("Unknown snapshot fields")
    candidates, processed = snapshot.get("candidates"), snapshot.get("processed", [])
    if not isinstance(candidates, list) or not isinstance(processed, list):
        raise ValueError("candidates and processed must be lists")
    known = {}
    for row in processed:
        key = _candidate(row, processed=True)
        if key in known:
            raise ValueError("Duplicate processed exact revision")
        known[key] = row
    last = snapshot.get("lastMaintenance")
    last_category = None
    if last is not None:
        if not isinstance(last, dict) or set(last) != {"id", "revision"}:
            raise ValueError("lastMaintenance must reference an evidenced completion")
        completed = known.get(_identity(last))
        if completed is None or completed["status"] != "done" or completed["category"] not in ("policies", "care"):
            raise ValueError("lastMaintenance is not an evidenced successful maintenance completion")
        last_category = completed["category"]
    seen, ready = set(), []
    for row in candidates:
        key = _candidate(row)
        if key in seen:
            raise ValueError("Duplicate candidate exact revision")
        seen.add(key)
        if key in known and known[key]["category"] != row["category"]:
            raise ValueError("Processed revision category differs from candidate")
        if row["status"] == "ready" and key not in known:
            ready.append(row)
    ready.sort(key=lambda row: _identity(row))
    categories = ("prs",) if mode == "bot-triage" else CATEGORIES if mode == "operator" else CATEGORIES[1:]
    selected, reason = None, "no_ready_candidate"
    for category in ("prs", "fixes"):
        if category in categories:
            selected = next((row for row in ready if row["category"] == category), None)
            if selected:
                reason = "fresh_pr_priority" if category == "prs" else "fixes_before_maintenance"
                break
    if selected is None and "policies" in categories:
        selected = next((row for row in ready if row["category"] == "policies" and row.get("urgent", False)), None)
        if selected:
            reason = "urgent_policy_before_care"
        else:
            preferred = "care" if last_category == "policies" else "policies"
            order = (preferred, "policies" if preferred == "care" else "care")
            for category in order:
                selected = next((row for row in ready if row["category"] == category), None)
                if selected:
                    if category != preferred:
                        reason = "preferred_maintenance_unavailable"
                    else:
                        reason = (
                            "initial_policy_preference"
                            if last_category is None
                            else "evidenced_maintenance_alternation"
                        )
                    break
    return {
        "mode": mode,
        "candidate": dict(selected) if selected else None,
        "revision_key": "@".join(_identity(selected)) if selected else None,
        "reason": reason,
        "next_check": "prs",
        "idle_cooldown_seconds": 60,
    }
