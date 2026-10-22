"""Typed write actions: ceiling + enabled + multi_bot switch, fresh re-read, exact head, readback."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from githubbot_core import actions, cli, typed_actions
from githubbot_core.actions import Action
from githubbot_core.config import default_config, merge_layers
from githubbot_core.multibot import permission_contract

REPO = "example-org/example-repo"
SHA = "a" * 40
OTHER = "b" * 40
ALL = list(typed_actions.KINDS)


def config(enabled=ALL, merge=True, **policy):
    switches = {"trigger_missing_ci": True, "open_fix_prs": True, "draft_ready_when_mature": True, **policy}
    layer = {
        "permissions": {"enabled": ["issue.comment", *enabled]},
        "multi_bot": {"merge_enabled": merge, "merge_account": "example-bot", "policy": switches},
    }
    return merge_layers([("default", default_config()), ("instance", layer)])


def pull(**overrides):
    data = {
        "state": "open",
        "draft": False,
        "node_id": "PR_node",
        "head": {"sha": SHA},
        "base": {"ref": "main"},
        "mergeable_state": "clean",
        "merged": False,
    }
    data.update(overrides)
    return data


class Transport:
    """GET answers may be lists (one entry per call, the last repeats); writes are recorded."""

    def __init__(self, gets, write_result=None):
        self.gets = {key: value if isinstance(value, list) else [value] for key, value in gets.items()}
        self.write_result = write_result if write_result is not None else {"ok": True}
        self.writes = []

    def get(self, endpoint):
        answers = self.gets.get(endpoint)
        if not answers:
            return None, f"404 {endpoint}"
        return (answers.pop(0) if len(answers) > 1 else answers[0]), None

    def request(self, method, endpoint, body=None):
        self.writes.append((method, endpoint, body))
        return self.write_result, None


def base_gets(**pull_overrides):
    return {
        f"repos/{REPO}": {"default_branch": "main"},
        "user": {"login": "example-bot"},
        f"repos/{REPO}/pulls/7": [pull(**pull_overrides), pull(merged=True, merge_commit_sha=OTHER)],
    }


def merge_action(sha=SHA):
    return Action("pr.merge", {"repo": REPO, "number": 7, "sha": sha})


def test_default_ceiling_lists_typed_actions_but_enables_none():
    cfg = default_config()
    assert set(ALL) <= set(cfg["permissions"]["ceiling"])
    assert not set(ALL) & set(cfg["permissions"]["enabled"])
    assert "not enabled" in actions.denial_reason(merge_action(), merge_layers([("default", cfg)]))


def test_permission_alone_is_not_enough_without_the_switch():
    assert "switched off" in actions.denial_reason(merge_action(), config(merge=False))
    off = config(trigger_missing_ci=False, open_fix_prs=False, draft_ready_when_mature=False)
    assert "switched off" in actions.denial_reason(Action("actions.rerun", {}), off)
    assert "switched off" in actions.denial_reason(Action("pr.create", {}), off)
    assert "switched off" in actions.denial_reason(Action("pr.ready", {}), off)
    assert actions.denial_reason(merge_action(), config()) is None


def test_static_parameters_are_checked_in_preview():
    cfg = config()
    assert "sha" in actions.denial_reason(merge_action(sha="abc"), cfg)
    assert "method" in actions.denial_reason(
        Action("pr.merge", {"repo": REPO, "number": 7, "sha": SHA, "method": "x"}), cfg
    )
    push = {"repo": REPO, "branch": "feature/x", "sha": SHA, "path": "."}
    assert "prefix" in actions.denial_reason(Action("git.push_branch", push), cfg)
    assert "invalid" in actions.denial_reason(Action("git.push_branch", {**push, "branch": "bot/../main"}), cfg)


def test_merge_is_sha_bound_and_read_back():
    transport = Transport(base_gets())
    result = actions.apply([merge_action()], transport, config())
    receipt = result["receipts"][0]
    assert receipt["status"] == "applied" and receipt["readback"]["merge_commit_sha"] == OTHER
    assert transport.writes == [("PUT", f"repos/{REPO}/pulls/7/merge", {"sha": SHA, "merge_method": "merge"})]


def test_merge_refuses_moved_head_unclean_stacked_draft_and_foreign_identity():
    cases = [
        {"head": {"sha": OTHER}},
        {"mergeable_state": "blocked"},
        {"mergeable_state": "unknown"},
        {"base": {"ref": "feature"}},
        {"draft": True},
        {"state": "closed"},
    ]
    for overrides in cases:
        transport = Transport(base_gets(**overrides))
        receipt = actions.apply([merge_action()], transport, config())["receipts"][0]
        assert receipt["status"] == "failed", overrides
        assert transport.writes == [], overrides
    gets = base_gets()
    gets["user"] = {"login": "someone-else"}
    transport = Transport(gets)
    assert actions.apply([merge_action()], transport, config())["receipts"][0]["status"] == "failed"
    assert transport.writes == []


def test_merge_without_readback_is_failed():
    gets = base_gets()
    gets[f"repos/{REPO}/pulls/7"] = pull()  # never shows merged
    receipt = actions.apply([merge_action()], Transport(gets), config())["receipts"][0]
    assert receipt["status"] == "failed" and "readback" in receipt["detail"]


class MergeReadbackTransport(Transport):
    """The versioned REST readback omits merge_commit_sha; GraphQL still exposes it."""

    def __init__(self, node=None, *, graphql_error=None, graphql_errors=None, after=None):
        gets = base_gets()
        gets[f"repos/{REPO}/pulls/7"] = [pull(), pull(merged=True) if after is None else after]
        super().__init__(gets)
        self.graphql = {"data": {"node": node}, "errors": graphql_errors}
        self.graphql_error = graphql_error

    def request(self, method, endpoint, body=None):
        if endpoint == "graphql":
            self.writes.append((method, endpoint, body))
            return self.graphql, self.graphql_error
        return super().request(method, endpoint, body)


def merged_node(**overrides):
    return {"state": "MERGED", "headRefOid": SHA, "mergeCommit": {"oid": OTHER}, **overrides}


def test_merge_api_2026_readback_confirms_the_commit_and_reviewed_head():
    transport = MergeReadbackTransport(merged_node())
    receipt = actions.apply([merge_action()], transport, config())["receipts"][0]
    assert receipt["status"] == "applied"
    assert receipt["readback"] == {"merged": True, "merge_commit_sha": OTHER}
    assert transport.writes[0] == ("PUT", f"repos/{REPO}/pulls/7/merge", {"sha": SHA, "merge_method": "merge"})
    method, endpoint, body = transport.writes[1]
    assert (method, endpoint, body["variables"]) == ("POST", "graphql", {"id": "PR_node"})
    assert body["query"].lstrip().startswith("query")
    assert len(transport.writes) == 2  # one merge attempt and one read-only query


@pytest.mark.parametrize(
    "node",
    [
        None,
        {},
        merged_node(state="OPEN"),
        merged_node(state="CLOSED"),
        merged_node(headRefOid=OTHER),
        merged_node(mergeCommit=None),
        merged_node(mergeCommit={}),
        merged_node(mergeCommit={"oid": "not-a-commit"}),
    ],
)
def test_merge_api_2026_needs_a_confirmed_commit_at_the_reviewed_head(node):
    transport = MergeReadbackTransport(node)
    receipt = actions.apply([merge_action()], transport, config())["receipts"][0]
    assert receipt["status"] == "failed" and "readback" in receipt["detail"]
    assert sum(endpoint.endswith("/merge") for _, endpoint, _ in transport.writes) == 1


@pytest.mark.parametrize("kwargs", [{"graphql_error": "Timeout"}, {"graphql_errors": [{"message": "unavailable"}]}])
def test_merge_api_2026_does_not_accept_a_graphql_error(kwargs):
    transport = MergeReadbackTransport(merged_node(), **kwargs)
    receipt = actions.apply([merge_action()], transport, config())["receipts"][0]
    assert receipt["status"] == "failed" and "readback" in receipt["detail"]


def test_merge_api_2026_does_not_query_when_rest_has_not_confirmed_the_merge():
    transport = MergeReadbackTransport(merged_node(), after=pull())
    receipt = actions.apply([merge_action()], transport, config())["receipts"][0]
    assert receipt["status"] == "failed"
    assert len(transport.writes) == 1


def test_ready_uses_graphql_for_the_exact_draft_head():
    gets = base_gets()
    gets[f"repos/{REPO}/pulls/7"] = [pull(draft=True), pull(draft=False)]
    transport = Transport(gets, write_result={"data": {"markPullRequestReadyForReview": {}}})
    action = Action("pr.ready", {"repo": REPO, "number": 7, "sha": SHA})
    receipt = actions.apply([action], transport, config())["receipts"][0]
    assert receipt["status"] == "applied"
    method, endpoint, body = transport.writes[0]
    assert (method, endpoint, body["variables"]) == ("POST", "graphql", {"id": "PR_node"})
    gets[f"repos/{REPO}/pulls/7"] = pull(draft=True, base={"ref": "feature"})
    transport = Transport(gets)
    assert actions.apply([action], transport, config())["receipts"][0]["status"] == "failed"
    assert transport.writes == []


def test_rerun_refuses_fork_runs_other_heads_and_running_runs():
    run = {"head_sha": SHA, "status": "completed", "head_repository": {"full_name": REPO}}
    action = Action("actions.rerun", {"repo": REPO, "run_id": 5, "sha": SHA})
    for overrides in (
        {"head_repository": {"full_name": "fork/example-repo"}},
        {"head_sha": OTHER},
        {"status": "queued"},
    ):
        transport = Transport({f"repos/{REPO}/actions/runs/5": {**run, **overrides}})
        assert actions.apply([action], transport, config())["receipts"][0]["status"] == "failed"
        assert transport.writes == []
    transport = Transport({f"repos/{REPO}/actions/runs/5": run})
    assert actions.apply([action], transport, config())["receipts"][0]["status"] == "applied"
    assert transport.writes == [("POST", f"repos/{REPO}/actions/runs/5/rerun", None)]


def test_dispatch_needs_an_active_workflow():
    action = Action("actions.dispatch", {"repo": REPO, "workflow": "ci.yml", "ref": "bot/fix"})
    transport = Transport({f"repos/{REPO}/actions/workflows/ci.yml": {"state": "disabled_manually"}})
    assert actions.apply([action], transport, config())["receipts"][0]["status"] == "failed"
    transport = Transport({f"repos/{REPO}/actions/workflows/ci.yml": {"state": "active"}})
    assert actions.apply([action], transport, config())["receipts"][0]["status"] == "applied"
    assert transport.writes == [("POST", f"repos/{REPO}/actions/workflows/ci.yml/dispatches", {"ref": "bot/fix"})]


def test_create_pr_targets_the_default_branch_from_a_bot_branch():
    action = Action("pr.create", {"repo": REPO, "branch": "bot/fix", "title": "fix: x", "body": "Problem, tests"})
    transport = Transport({f"repos/{REPO}": {"default_branch": "main"}}, write_result={"number": 9, "html_url": "u"})
    receipt = actions.apply([action], transport, config())["receipts"][0]
    assert receipt["status"] == "applied" and receipt["readback"]["number"] == 9
    assert transport.writes[0][2]["base"] == "main" and transport.writes[0][2]["head"] == "bot/fix"


def test_push_branch_never_forces_and_checks_origin(monkeypatch):
    calls = []

    def git(cmd, **kwargs):
        calls.append(cmd[3:])
        out = {
            "remote": f"https://github.com/{REPO}.git\n",
            "rev-parse": SHA + "\n",
            "push": "",
            "ls-remote": f"{SHA}\trefs/heads/bot/fix\n",
        }[cmd[3]]
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(typed_actions, "_git", git)
    action = Action("git.push_branch", {"repo": REPO, "branch": "bot/fix", "sha": SHA, "path": "."})
    transport = Transport({f"repos/{REPO}": {"default_branch": "main"}})
    receipt = actions.apply([action], transport, config())["receipts"][0]
    assert receipt["status"] == "applied"
    push = next(c for c in calls if c[0] == "push")
    assert push == ["push", "origin", f"{SHA}:refs/heads/bot/fix"]
    assert not any(arg.startswith("+") or "force" in arg for c in calls for arg in c)

    calls.clear()
    monkeypatch.setattr(
        typed_actions,
        "_git",
        lambda cmd, **k: SimpleNamespace(returncode=0, stdout="https://github.com/other/repo\n", stderr=""),
    )
    assert actions.apply([action], transport, config())["receipts"][0]["status"] == "failed"


def test_push_branch_refuses_the_default_branch_even_with_bot_prefix(monkeypatch):
    monkeypatch.setattr(typed_actions, "_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git called")))
    cfg = config()
    cfg["typed_actions"] = {"push_branch_prefix": "release"}
    action = Action("git.push_branch", {"repo": REPO, "branch": "release/main", "sha": SHA, "path": "."})
    transport = Transport({f"repos/{REPO}": {"default_branch": "release/main"}})
    assert actions.apply([action], transport, cfg)["receipts"][0]["status"] == "failed"


def test_run_contract_reports_effective_enabled(tmp_path):
    layer = tmp_path / "instance.json"
    layer.write_text(
        json.dumps(
            {
                "permissions": {"enabled": ["issue.comment", "pr.merge", "pr.ready"]},
                "multi_bot": {"merge_enabled": True, "policy": {"draft_auto_ready_hours": None}},
            }
        )
    )
    from githubbot_core.config import load_config

    contract = permission_contract(load_config([layer]), [layer])
    assert contract["permissions"]["effective_enabled"] == ["issue.comment", "pr.merge"]


def test_cli_act_previews_then_applies_with_expected_plan(tmp_path, capsys):
    layer = tmp_path / "instance.json"
    layer.write_text(
        json.dumps({"permissions": {"enabled": ["pr.merge"]}, "multi_bot": {"merge_enabled": True}}), encoding="utf-8"
    )
    params = json.dumps({"repo": REPO, "number": 7, "sha": SHA})
    transport = Transport(base_gets())
    argv = ["--config", str(layer), "--json", "act", "pr.merge", "--params", params]
    assert cli.main(argv, transport=transport) == 0
    preview = json.loads(capsys.readouterr().out)["data"]
    assert preview["actions"][0]["permitted"] and transport.writes == []
    assert cli.main([*argv, "--apply", "--expect-plan", preview["plan_sha256"]], transport=transport) == 0
    assert json.loads(capsys.readouterr().out)["data"]["receipts"][0]["status"] == "applied"
