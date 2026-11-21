"""Explicit authority, including direct CLI/process calls without a wrapper."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from githubbot_core import actions, cli, write_scope
from githubbot_core.actions import Action
from githubbot_core.config import ConfigError, default_config, merge_layers


def grant(owners=("example-org",), account="example-bot"):
    return merge_layers(
        [
            ("default", default_config()),
            ("profile", {"permissions": {"write_owners": list(owners)}, "multi_bot": {"merge_account": account}}),
        ]
    )


def comment(repo="example-org/repo"):
    return Action("issue.comment", {"repo": repo, "number": 1, "body": "synthetic"})


class Transport:
    def __init__(self, login="example-bot", actual="example-org/repo"):
        self.login, self.actual = login, actual
        self.reads, self.writes = [], []

    def get(self, endpoint):
        self.reads.append(endpoint)
        if endpoint == "user":
            return {"login": self.login}, None
        return {"full_name": self.actual}, None

    def request(self, method, endpoint, body=None):
        self.writes.append((method, endpoint, body))
        return {"ok": True}, None


@pytest.mark.parametrize("owners", [None, [], ["other-org"]])
def test_unbound_empty_or_outside_owner_denied_before_transport(owners):
    cfg = default_config() if owners is None else grant(owners)
    t = Transport()
    assert not actions.preview([comment()], cfg)["actions"][0]["permitted"]
    result = actions.apply([comment()], t, cfg)
    assert result["writes"] == 0 and result["receipts"][0]["status"] == "denied"
    assert not t.reads and not t.writes


def test_first_binding_case_normalization_then_only_narrowing():
    layers = [
        ("default", default_config()),
        ("profile", {"permissions": {"write_owners": ["Example-Org", "Other-Org"]}}),
        ("host", {"permissions": {"write_owners": ["EXAMPLE-ORG"]}}),
    ]
    assert merge_layers(layers)["permissions"]["write_owners"] == ["example-org"]
    for reset in (None, ["other-org"], ["example-org", "third-org"]):
        with pytest.raises(ConfigError):
            merge_layers([*layers, ("unsafe", {"permissions": {"write_owners": reset}})])
    with pytest.raises(ConfigError):
        merge_layers(
            [("a", {"permissions": {"write_owners": []}}), ("b", {"permissions": {"write_owners": ["example-org"]}})]
        )


@pytest.mark.parametrize(
    "value",
    [
        "*",
        ["*"],
        ["example-org/repo"],
        ["https://example.org"],
        [" example-org"],
        ["org%2fother"],
        ["öwner"],
        ["Example", "EXAMPLE"],
        [False],
        {},
        1,
    ],
)
def test_invalid_owner_ceiling_never_becomes_global(value):
    with pytest.raises(ConfigError):
        merge_layers([("default", default_config()), ("bad", {"permissions": {"write_owners": value}})])


@pytest.mark.parametrize(
    "repo",
    [
        "example-org.evil/repo",
        "example-org-evil/repo",
        "https://example-org/repo",
        "example-org/repo/other",
        "example-org/%2frepo",
        "example-org/..",
        "öwner/repo",
    ],
)
def test_no_uri_prefix_alias_or_path_authority(repo):
    assert actions.denial_reason(comment(repo), grant())


@pytest.mark.parametrize("owner", [f"synthetic-org-{i}" for i in range(8)])
def test_generic_owner_grants_are_exact_and_instance_independent(owner):
    cfg = grant([owner])
    t = Transport(actual=owner + "/repo")
    assert actions.apply([comment(owner + "/repo")], t, cfg)["writes"] == 1


@pytest.mark.parametrize(
    "kind",
    [
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
    ],
)
def test_all_repository_actions_share_same_owner_gate(kind):
    assert "outside" in write_scope.denial(kind, {"repo": "foreign/repo"}, grant())


@pytest.mark.parametrize(
    "endpoint",
    [
        "graphql",
        "user/starred/example-org/repo",
        "repos/foreign/repo/issues/1/comments",
        "repos/example-org/repo/issues/1/comments?method=GET",
        "repos/example-org/repo/issues/%2f/comments",
        "https://api.example.org/repos/example-org/repo/issues/1/comments",
        "repos/example-org/repo/transfer",
        "repos/example-org/repo/releases",
        "repos/example-org/repo/git/refs/heads/main",
    ],
)
def test_raw_unknown_mutations_fail_closed(endpoint):
    cfg = grant()
    cfg["permissions"].update(enabled=["api.request"], api_write_prefixes=[""])
    action = Action("api.request", {"endpoint": endpoint, "method": "POST"})
    t = Transport()
    assert actions.apply([action], t, cfg)["writes"] == 0
    assert not t.reads and not t.writes


def test_scope_and_actor_changes_invalidate_previous_plan():
    cfg = grant(["example-org", "other-org"])
    digest = actions.preview([comment()], cfg)["plan_sha256"]
    for changed in (grant(["example-org"]), grant(["example-org", "other-org"], "other-user")):
        t = Transport()
        result = actions.apply([comment()], t, changed, expected_plan_sha256=digest)
        assert result["writes"] == 0 and "mismatch" in result["error"] and not t.reads


@pytest.mark.parametrize("login", ["wrong-user", "", None])
def test_every_action_requires_actual_expected_actor(login):
    t = Transport(login=login)
    assert actions.apply([comment()], t, grant())["writes"] == 0
    assert t.reads == ["user"] and not t.writes


def test_missing_actor_denied_without_network_and_explicit_actor_change_keeps_scope():
    cfg = grant()
    del cfg["multi_bot"]["merge_account"]
    t = Transport()
    assert actions.apply([comment()], t, cfg)["writes"] == 0 and not t.reads
    changed = merge_layers([("bound", grant()), ("explicit-account", {"multi_bot": {"merge_account": "other-user"}})])
    assert changed["permissions"]["write_owners"] == ["example-org"]
    assert actions.apply([comment()], Transport(login="other-user"), changed)["writes"] == 1


def test_canonical_redirect_and_mutating_outer_config_do_not_change_authority():
    cfg = grant()
    t = Transport(actual="foreign/repo")
    assert actions.apply([comment()], t, cfg)["writes"] == 0 and not t.writes

    class Changing(Transport):
        def get(self, endpoint):
            cfg["permissions"]["write_owners"] = ["foreign"]
            return super().get(endpoint)

    assert actions.apply([comment()], Changing(), cfg)["writes"] == 1
    assert cfg["permissions"]["write_owners"] == ["foreign"]


def test_direct_cli_with_explicit_grant_and_without_wrapper(tmp_path, capsys):
    profile = tmp_path / "grant.json"
    profile.write_text(json.dumps(grant()), encoding="utf-8")
    t = Transport()
    args = [
        "--config",
        str(profile),
        "--json",
        "act",
        "issue.comment",
        "--params",
        json.dumps(comment().params),
        "--apply",
    ]
    assert cli.main(args, transport=t) == 0
    assert len(t.writes) == 1


def test_direct_core_process_default_is_denied_without_gh_or_credentials(tmp_path):
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": str(tmp_path),
    }
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "githubbot_core",
            "--json",
            "act",
            "issue.comment",
            "--params",
            json.dumps(comment().params),
            "--apply",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)["data"]
    assert data["writes"] == 0 and data["receipts"][0]["status"] == "denied"
    assert "write_owners" in data["receipts"][0]["detail"]


def test_direct_process_cannot_expand_profile_via_external_target(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(grant()), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": str(tmp_path),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "githubbot_core",
            "--config",
            str(profile),
            "--json",
            "act",
            "issue.comment",
            "--params",
            json.dumps(comment("outside-org/repo").params),
            "--apply",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)["data"]
    assert data["writes"] == 0 and data["receipts"][0]["status"] == "denied"
    assert "outside permissions.write_owners" in data["receipts"][0]["detail"]


def test_actor_is_rechecked_for_each_effect_and_scope_case_is_normalized():
    cfg = grant(["EXAMPLE-ORG"], "Example-Bot")

    class Changing(Transport):
        def request(self, *args, **kwargs):
            result = super().request(*args, **kwargs)
            self.login = "another-user"
            return result

    t = Changing()
    result = actions.apply([comment("EXAMPLE-ORG/REPO"), comment()], t, cfg)
    assert result["writes"] == 1 and len(t.writes) == 1
    assert [r["status"] for r in result["receipts"]] == ["applied", "denied"]


def test_unknown_executor_target_cannot_borrow_a_repo_field(monkeypatch):
    monkeypatch.setitem(actions.EXECUTORS, "opaque.write", lambda *a: pytest.fail("unknown executor"))
    cfg = grant()
    cfg["permissions"].update(ceiling=["opaque.write"], enabled=["opaque.write"])
    t = Transport()
    result = actions.apply([Action("opaque.write", {"repo": "example-org/repo"})], t, cfg)
    assert result["writes"] == 0 and not t.reads


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_known_star_spi_retains_explicit_owner_actor_and_prefix_gates(method):
    cfg = grant()
    cfg["permissions"].update(enabled=["api.request"], api_write_prefixes=["user/starred/"])
    action = Action("api.request", {"method": method, "endpoint": "user/starred/example-org/repo"})
    t = Transport()
    assert actions.apply([action], t, cfg)["writes"] == 1
    assert t.reads == ["user", "repos/example-org/repo"]
    assert t.writes == [(method, "user/starred/example-org/repo", None)]
    cfg["permissions"]["api_write_prefixes"] = ["repos/example-org/"]
    blocked = Transport()
    assert actions.apply([action], blocked, cfg)["writes"] == 0 and not blocked.reads


@pytest.mark.parametrize(
    "endpoint,method",
    [
        ("user/starred/outside-org/repo", "PUT"),
        ("user/starred/example-org.evil/repo", "PUT"),
        ("user/starred/example-org%2foutside/repo", "DELETE"),
        ("user/starred/example-org/repo%2fother", "PUT"),
        ("user/starred/example-org/repo", "POST"),
        ("user/starred/example-org/repo", "UNKNOWN"),
        ("user/following/example-org", "PUT"),
    ],
)
def test_star_target_aliases_unknown_methods_and_user_targets_are_denied(endpoint, method):
    cfg = grant()
    cfg["permissions"].update(enabled=["api.request"], api_write_prefixes=["user/"])
    t = Transport()
    result = actions.apply([Action("api.request", {"method": method, "endpoint": endpoint})], t, cfg)
    assert result["writes"] == 0 and not t.reads and not t.writes
