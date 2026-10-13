"""Offline scheduling decisions are deterministic reads of exact-revision snapshots."""

from copy import deepcopy
import json

import pytest

from githubbot_core import cli, multibot
from githubbot_core.multibot_select import select_work

A, B = "a" * 40, "b" * 40


def item(name, category, revision=A, status="ready", urgent=False):
    return {"id": name, "revision": revision, "category": category, "status": status, "urgent": urgent}


def snapshot(*candidates, processed=(), last=None):
    return {"candidates": list(candidates), "processed": list(processed), "lastMaintenance": last}


def completed(candidate, status="done"):
    return dict(candidate, status=status, evidence="receipt:verified-exact-head")


def test_numbered_menu_and_default_are_new_roles_with_legacy_names_preserved():
    assert multibot.MODES == ("operator", "bot-triage", "github-maintain")
    assert [multibot.choose_mode(n) for n in ("1", "2", "3")] == list(multibot.MODES)
    assert multibot.choose_mode(chooser=lambda *a, **k: "") == "operator"
    for legacy in ("triage", "review-merge", "aftercare"):
        assert multibot.choose_mode(legacy) == legacy


def test_fresh_prs_preempt_every_new_operator_work_unit():
    policy = item("own/repo-policy", "policies")
    first = select_work(snapshot(policy))
    assert first["candidate"] == policy
    prs = item("own/repo#7", "prs")
    assert select_work(snapshot(policy, prs))["candidate"] == prs


def test_blocked_done_and_exact_processed_heads_do_not_starve_lower_categories():
    prs = item("own/repo#7", "prs")
    fix = item("own/repo-fix", "fixes")
    for status in ("blocked", "done"):
        assert select_work(snapshot(dict(prs, status=status), fix))["candidate"] == fix
        assert select_work(snapshot(prs, fix, processed=[completed(prs, status)]))["candidate"] == fix
    changed = dict(prs, revision=B)
    assert select_work(snapshot(changed, fix, processed=[completed(prs)]))["candidate"] == changed


def test_fixes_precede_both_maintenance_categories():
    fix = item("own/fix", "fixes")
    assert select_work(snapshot(item("own/care", "care"), item("own/policy", "policies"), fix))["candidate"] == fix


def test_policies_initial_then_evidenced_alternation_and_urgent_policy():
    policy, care = item("own/policy", "policies"), item("own/care", "care")
    assert select_work(snapshot(care, policy))["candidate"] == policy
    done = completed(policy)
    last = {"id": policy["id"], "revision": A}
    assert select_work(snapshot(care, processed=[done], last=last))["candidate"] == care
    another = item("own/other-policy", "policies")
    assert select_work(snapshot(care, another, processed=[done], last=last))["candidate"] == care
    urgent = dict(another, urgent=True)
    assert select_work(snapshot(care, urgent, processed=[done], last=last))["candidate"] == urgent


def test_two_empty_checks_do_not_advance_maintenance_or_mutate_inputs():
    done = completed(item("own/policy", "policies"))
    data = snapshot(processed=[done], last={"id": done["id"], "revision": A})
    before = deepcopy(data)
    assert select_work(data) == select_work(data)
    assert select_work(data)["candidate"] is None
    assert select_work(data)["idle_cooldown_seconds"] == 60
    assert data == before


def test_scope_narrows_without_adding_authority():
    prs, fix = item("own/repo#7", "prs"), item("own/fix", "fixes")
    assert select_work(snapshot(prs, fix), mode="bot-triage")["candidate"] == prs
    assert select_work(snapshot(fix), mode="bot-triage")["candidate"] is None
    assert select_work(snapshot(prs, fix), mode="github-maintain")["candidate"] == fix


@pytest.mark.parametrize(
    "change",
    [
        {"category": "unknown"},
        {"status": "unknown"},
        {"revision": "short"},
        {"id": "../escape"},
        {"urgent": "yes"},
        {"extra": True},
    ],
)
def test_invalid_candidate_is_rejected(change):
    row = item("own/policy", "policies")
    row.update(change)
    with pytest.raises(ValueError):
        select_work(snapshot(row))


def test_unknown_mode_and_unproven_or_blocked_maintenance_marker_rejected():
    with pytest.raises(ValueError):
        select_work(snapshot(), mode="unknown")
    row = item("own/policy", "policies")
    last = {"id": row["id"], "revision": A}
    for processed in ([], [completed(row, "blocked")], [dict(completed(row), evidence="")]):
        with pytest.raises(ValueError):
            select_work(snapshot(processed=processed, last=last))


def test_input_order_does_not_change_decision():
    a, z = item("own/a", "care"), item("own/z", "care")
    assert select_work(snapshot(z, a)) == select_work(snapshot(a, z))


def test_cli_selection_is_read_only_with_readonly_permission_ceiling(tmp_path, capsys):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot(item("own/fix", "fixes"))), encoding="utf-8")
    config = tmp_path / "readonly.json"
    config.write_text(json.dumps({"permissions": {"ceiling": [], "enabled": [], "api_write_prefixes": []}}))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert cli.main(["--json", "--config", str(config), "multi-bot-select", "--input", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "read" and result["data"]["candidate"]["category"] == "fixes"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
