"""Typed write actions: ceiling + enabled + multi_bot switch, fresh re-read, exact head, readback."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from githubbot_core import actions, cli, typed_actions
from githubbot_core.actions import Action
from githubbot_core.config import default_config, merge_layers
from githubbot_core.credentials import EnvTokenProvider, resolve_token
from githubbot_core.multibot import permission_contract
from githubbot_core.transport import GhCliTransport

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


class TokenTransport(Transport):
    def __init__(self, gets, *, login="example-bot"):
        super().__init__(gets)
        self.login = login
        self.token_reads = []

    def get_for_token(self, token, endpoint):
        self.token_reads.append((token, endpoint))
        if endpoint == "user":
            return {"login": self.login}, None
        return self.get(endpoint)


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
    missing_account = config()
    missing_account["multi_bot"]["merge_account"] = " "
    assert "merge_account" in actions.denial_reason(
        Action("git.push_branch", {**push, "branch": "bot/fix"}), missing_account
    )


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


def _mock_push_token(monkeypatch, token="synthetic-bot-token"):
    seen = []

    def reader(providers):
        providers = list(providers)
        seen.append(providers)
        assert len(providers) == 1
        assert isinstance(providers[0], EnvTokenProvider)
        assert providers[0].names == ("GH_TOKEN",)
        return token, providers[0].name

    monkeypatch.setattr(typed_actions, "resolve_token", reader)
    return token, seen


def _fake_git_output(command, *, origin=None, pushurl=None, readback=None, fail_push=False):
    op_index = next(
        i for i, part in enumerate(command) if part in {"remote", "rev-parse", "check-ref-format", "push", "ls-remote"}
    )
    op = command[op_index]
    if op == "remote":
        if "--push" in command:
            output = pushurl if pushurl is not None else origin
        else:
            output = origin
    elif op == "rev-parse":
        output = SHA
    elif op == "check-ref-format":
        output = "bot/fix"
    elif op == "push":
        if fail_push:
            return SimpleNamespace(
                returncode=128, stdout="remote rejected synthetic-bot-token", stderr="auth synthetic-bot-token"
            )
        output = ""
    else:
        output = readback if readback is not None else f"{SHA}\trefs/heads/bot/fix\n"
    return SimpleNamespace(returncode=0, stdout=f"{output}\n" if output else "", stderr="")


def _push_action(branch="bot/fix"):
    return Action("git.push_branch", {"repo": REPO, "branch": branch, "sha": SHA, "path": "."})


def _push_transport(login="example-bot"):
    return TokenTransport({f"repos/{REPO}": {"default_branch": "main"}}, login=login)


def test_push_branch_uses_same_bot_token_exact_remote_and_exact_readback(monkeypatch):
    token, readers = _mock_push_token(monkeypatch)
    calls = []
    temp_dirs = []

    def git(cmd, **kwargs):
        if "GITHUBBOT_GIT_TOKEN" in kwargs.get("env", {}):
            env = kwargs["env"]
            helper = env["GIT_ASKPASS"]
            temp_dirs.append(os.path.dirname(helper))
            assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
            assert env["GITHUBBOT_GIT_TOKEN"] == token
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            assert all(token not in arg for arg in cmd)
            assert token not in Path(helper).read_text(encoding="utf-8")
            assert "credential.helper=" in cmd
            assert "http.extraHeader=" in cmd
        calls.append((cmd, kwargs))
        return _fake_git_output(
            cmd,
            origin=f"https://github.com/{REPO}.git",
            pushurl=f"https://github.com/{REPO}.git",
        )

    monkeypatch.setattr(typed_actions, "_git", git)
    transport = _push_transport()
    receipt = actions.apply([_push_action()], transport, config())["receipts"][0]
    assert receipt["status"] == "applied"
    # Both API preflights use the token resolved by the one GH_TOKEN-only provider.
    assert transport.token_reads == [(token, "user"), (token, f"repos/{REPO}")]
    assert len(readers[0]) == 1
    push = next(cmd for cmd, _ in calls if "push" in cmd)
    assert push[push.index("push") + 1 :] == ["origin", f"{SHA}:refs/heads/bot/fix"]
    assert not any(arg.startswith("+") or "force" in arg for cmd, _ in calls for arg in cmd)
    assert temp_dirs and all(not os.path.exists(directory) for directory in temp_dirs)


def test_push_branch_fails_closed_for_wrong_identity_or_missing_credential(monkeypatch):
    _mock_push_token(monkeypatch)
    calls = []
    monkeypatch.setattr(typed_actions, "_git", lambda cmd, **kwargs: calls.append(cmd))
    wrong_identity = actions.apply([_push_action()], _push_transport("lukisch"), config())["receipts"][0]
    assert wrong_identity["status"] == "failed"
    assert "identity" in wrong_identity["detail"]
    assert calls == []

    def missing_reader(providers):
        providers = list(providers)
        assert len(providers) == 1 and providers[0].names == ("GH_TOKEN",)
        return resolve_token([EnvTokenProvider(names=("GH_TOKEN",), environ={})])

    monkeypatch.setattr(typed_actions, "resolve_token", missing_reader)
    missing = actions.apply([_push_action()], _push_transport(), config())["receipts"][0]
    assert missing["status"] == "failed"
    assert "credential unavailable" in missing["detail"]
    assert calls == []


def test_push_branch_rejects_bad_prefix_before_git_or_credential_lookup(monkeypatch):
    calls = []
    monkeypatch.setattr(
        typed_actions, "resolve_token", lambda _: (_ for _ in ()).throw(AssertionError("reader called"))
    )
    monkeypatch.setattr(typed_actions, "_git", lambda cmd, **kwargs: calls.append(cmd))
    action = _push_action(branch="feature/x")
    receipt = actions.apply([action], _push_transport(), config())["receipts"][0]
    assert receipt["status"] == "denied"
    assert "prefix" in receipt["detail"]
    assert calls == []


@pytest.mark.parametrize(
    "origin,pushurl",
    [
        ("https://github.com.attacker.invalid/example-org/example-repo.git", None),
        ("https://github.com/other/repo.git", None),
        ("https://token@github.com/example-org/example-repo.git", None),
        (f"https://github.com:443/{REPO}.git", None),
        (f"https://github.com/{REPO}.git?", None),
        (f"git@github.com:{REPO}.git", None),
        (f"https://github.com/{REPO}.git", "https://github.com/other/repo.git"),
    ],
)
def test_push_branch_rejects_unbound_origin_and_pushurl(monkeypatch, origin, pushurl):
    _mock_push_token(monkeypatch)
    calls = []

    def git(cmd, **kwargs):
        calls.append(cmd)
        return _fake_git_output(cmd, origin=origin, pushurl=pushurl)

    monkeypatch.setattr(typed_actions, "_git", git)
    receipt = actions.apply([_push_action()], _push_transport(), config())["receipts"][0]
    assert receipt["status"] == "failed"
    assert not any("push" in cmd for cmd in calls)


def test_push_branch_rejects_empty_port_separator_remote(monkeypatch):
    _mock_push_token(monkeypatch)
    calls = []

    def git(cmd, **kwargs):
        calls.append(cmd)
        return _fake_git_output(cmd, origin=f"https://github.com:/{REPO}.git")

    monkeypatch.setattr(typed_actions, "_git", git)
    receipt = actions.apply([_push_action()], _push_transport(), config())["receipts"][0]
    assert receipt["status"] == "failed"
    assert "origin of the checkout" in receipt["detail"]
    assert not any("push" in cmd for cmd in calls)


@pytest.mark.parametrize(
    "readback",
    [
        f"{SHA}\trefs/heads/bot/other\n",
        f"{OTHER}\trefs/heads/bot/fix\n",
        f"{SHA}\trefs/heads/bot/fix\n{OTHER}\trefs/heads/bot/other\n",
    ],
)
def test_push_branch_rejects_wrong_readback_oid_or_ref(monkeypatch, readback):
    _mock_push_token(monkeypatch)
    calls = []

    def git(cmd, **kwargs):
        calls.append(cmd)
        return _fake_git_output(
            cmd,
            origin=f"https://github.com/{REPO}.git",
            pushurl=f"https://github.com/{REPO}.git",
            readback=readback,
        )

    monkeypatch.setattr(typed_actions, "_git", git)
    receipt = actions.apply([_push_action()], _push_transport(), config())["receipts"][0]
    assert receipt["status"] == "failed"
    assert "read the target before retrying" in receipt["detail"]
    assert any("push" in cmd for cmd in calls)


def test_push_branch_redacts_secret_like_git_errors(monkeypatch, caplog):
    token, _ = _mock_push_token(monkeypatch)
    temp_dirs = []

    def git(cmd, **kwargs):
        if "GITHUBBOT_GIT_TOKEN" in kwargs.get("env", {}):
            temp_dirs.append(os.path.dirname(kwargs["env"]["GIT_ASKPASS"]))
        return _fake_git_output(
            cmd,
            origin=f"https://github.com/{REPO}.git",
            pushurl=f"https://github.com/{REPO}.git",
            fail_push="push" in cmd,
        )

    monkeypatch.setattr(typed_actions, "_git", git)
    receipt = actions.apply([_push_action()], _push_transport(), config())["receipts"][0]
    assert receipt["status"] == "failed"
    assert token not in receipt["detail"] and token not in caplog.text
    assert "git process exited with code 128" in receipt["detail"]
    assert "read the target before retrying" in receipt["detail"]
    assert temp_dirs and all(not os.path.exists(directory) for directory in temp_dirs)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("start", "git process could not start (FileNotFoundError)"),
        ("timeout", "git process timed out"),
        ("exit", "git process exited with code 128"),
    ],
)
def test_push_failure_receipt_reports_only_safe_process_category(monkeypatch, caplog, failure, expected):
    token, _ = _mock_push_token(monkeypatch)
    temp_dirs = []

    def git(cmd, **kwargs):
        if "push" in cmd:
            temp_dirs.append(Path(kwargs["env"]["GIT_ASKPASS"]).parent)
            if failure == "start":
                raise FileNotFoundError(f"synthetic credential-shaped text {token}")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(
                    cmd, timeout=120, output=f"synthetic {token}", stderr=f"rejected {token}"
                )
            return SimpleNamespace(
                returncode=128,
                stdout=f"remote rejected {token}",
                stderr=f"authentication failed {token}",
            )
        return _fake_git_output(
            cmd,
            origin=f"https://github.com/{REPO}.git",
            pushurl=f"https://github.com/{REPO}.git",
        )

    monkeypatch.setattr(typed_actions, "_git", git)
    receipt = actions.apply([_push_action()], _push_transport(), config())["receipts"][0]
    assert receipt["status"] == "failed"
    assert expected in receipt["detail"]
    assert "read the target before retrying" in receipt["detail"]
    assert token not in receipt["detail"] and token not in caplog.text
    assert temp_dirs and all(not directory.exists() for directory in temp_dirs)


def test_git_base_environment_removes_ambient_auth_and_repo_overrides(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "synthetic-ambient-token")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-ambient-token")
    monkeypatch.setenv("GIT_DIR", "wrong-repository")
    monkeypatch.setenv("GIT_WORK_TREE", "wrong-worktree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_TRACE", "2")
    env = typed_actions._git_base_environment()
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert "GIT_CONFIG_COUNT" not in env and "GIT_TRACE" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_git_askpass_helper_uses_child_env_and_cleans_tempfiles(monkeypatch, tmp_path):
    token = "synthetic-askpass-token"
    directory = None
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "invalid-pythonhome"))
    with typed_actions._git_auth_environment(token, "example-bot") as env:
        helper = Path(env["GIT_ASKPASS"])
        directory = helper.parent
        script = helper.with_name("askpass.py")
        source = script.read_text(encoding="utf-8")
        assert token not in source
        commands = []

        def run_askpass(prompt):
            if os.name == "nt":
                args = f'call "{helper}" "{prompt}"'
                command = args
                shell = True
            else:
                args = [str(helper), prompt]
                command = args
                shell = False
            commands.append(args)
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                shell=shell,
                check=True,
            )

        username = run_askpass("Username for https://github.com:")
        password = run_askpass("Password for https://github.com:")
        assert username.stdout == "example-bot"
        assert password.stdout == token
        assert env["PYTHONHOME"].endswith("invalid-pythonhome")
        assert all(token not in str(command) for command in commands)
    assert directory is not None and not directory.exists()


def test_transport_reads_identity_with_exact_token_without_argv_or_raw_error():
    token = "synthetic-api-token"
    seen = []

    def runner(command, **kwargs):
        seen.append((command, kwargs, kwargs["env"].get("GH_TOKEN"), "GITHUB_TOKEN" in kwargs["env"]))
        return SimpleNamespace(returncode=1, stdout=f"{token}", stderr=f"failed {token}")

    transport = GhCliTransport(runner=runner)
    user, error = transport.get_for_token(token, "user")
    assert user is None and error == "GitHub request failed"
    command, kwargs, child_token, has_ambiguous_token = seen[0]
    assert "--hostname" in command and "github.com" in command
    assert all(token not in arg for arg in command)
    assert child_token == token
    assert not has_ambiguous_token
    assert "GH_TOKEN" not in kwargs["env"]


def test_transport_invoke_preserves_legacy_args_and_stdin_call_shapes():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        assert "env" not in kwargs
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    transport = GhCliTransport(runner=runner)
    assert transport._invoke(["user"]) == ('{"ok":true}', None)
    assert transport._invoke(["user"], "request-body") == ('{"ok":true}', None)
    assert calls[0][1]["input"] is None
    assert calls[1][1]["input"] == "request-body"


def test_push_branch_refuses_the_default_branch_even_with_bot_prefix(monkeypatch):
    _mock_push_token(monkeypatch)
    monkeypatch.setattr(typed_actions, "_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git called")))
    cfg = config()
    cfg["typed_actions"] = {"push_branch_prefix": "release"}
    action = Action("git.push_branch", {"repo": REPO, "branch": "release/main", "sha": SHA, "path": "."})
    transport = TokenTransport({f"repos/{REPO}": {"default_branch": "release/main"}})
    receipt = actions.apply([action], transport, cfg)["receipts"][0]
    assert receipt["status"] == "failed" and "never pushes" in receipt["detail"]


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
