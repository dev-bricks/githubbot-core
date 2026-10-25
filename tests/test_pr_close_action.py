"""Closing is opt-in and limited to heads proven contained in current main."""

import copy

import pytest

from githubbot_core import actions, typed_actions
from githubbot_core.actions import Action
from githubbot_core.config import default_config, merge_layers
from githubbot_core.multibot import permission_contract, policy_settings

REPO = "example-org/example-repo"
SHA, MAIN, OTHER = "a" * 40, "b" * 40, "c" * 40
ENDPOINT = f"repos/{REPO}/pulls/7"


def config(*, enabled=True, policy=True, repos=None):
    return merge_layers(
        [
            ("default", default_config()),
            (
                "instance",
                {
                    "permissions": {"enabled": ["pr.close"] if enabled else []},
                    "multi_bot": {"merge_account": "example-bot", "policy": {"close_integrated_prs": policy}},
                    "typed_actions": {"close_repositories": [REPO] if repos is None else repos},
                },
            ),
        ]
    )


def action(**overrides):
    return Action(
        "pr.close",
        {"repo": REPO, "number": 7, "sha": SHA, "integration_sha": MAIN, "reason": "integrated", **overrides},
    )


class Transport:
    def __init__(self):
        self.login = "example-bot"
        self.branch = "main"
        self.ref = {"object": {"sha": MAIN, "type": "commit"}}
        self.pull = {
            "number": 7,
            "state": "open",
            "merged": False,
            "head": {"sha": SHA},
            "draft": True,
            "base": {"ref": "stacked-worker", "repo": {"full_name": REPO}},
        }
        self.proof = {"status": "ahead", "base_commit": {"sha": SHA}, "merge_base_commit": {"sha": SHA}}
        self.pull_answers = []
        self.ref_answers = []
        self.errors = {}
        self.write_error = None
        self.readback_changes = {}
        self.writes, self.reads = [], []

    def get(self, endpoint):
        self.reads.append(endpoint)
        if endpoint == "user":
            data = {"login": self.login}
        elif endpoint == f"repos/{REPO}":
            data = {"default_branch": self.branch}
        elif endpoint.startswith(f"repos/{REPO}/git/ref/heads/"):
            data = self.ref_answers.pop(0) if self.ref_answers else self.ref
        elif endpoint == ENDPOINT:
            data = self.pull_answers.pop(0) if self.pull_answers else self.pull
        elif endpoint.startswith(f"repos/{REPO}/compare/{SHA}..."):
            data = self.proof
        else:
            raise AssertionError(endpoint)
        return copy.deepcopy(data), self.errors.get(endpoint)

    def request(self, method, endpoint, body):
        assert (method, endpoint, body) == ("PATCH", ENDPOINT, {"state": "closed"})
        self.writes.append((method, endpoint, body))
        self.pull.update(state="closed", **self.readback_changes)
        return copy.deepcopy(self.pull), self.write_error


def apply(transport, item=None, cfg=None, **kwargs):
    return actions.apply([item or action()], transport, cfg or config(), **kwargs)


def test_opt_in_preview_and_permission_contract():
    assert "pr.close" not in default_config()["permissions"]["enabled"]
    assert policy_settings({})["close_integrated_prs"] is False
    assert actions.preview([action()], config())["writes"] == 0
    assert permission_contract(config(), [])["permissions"]["effective_enabled"] == ["pr.close"]
    for cfg in [config(enabled=False), config(policy=False), config(repos=[]), config(repos=["other/repo"])]:
        t = Transport()
        assert apply(t, cfg=cfg)["receipts"][0]["status"] == "denied"
        assert not t.reads and not t.writes
    with pytest.raises(ValueError):
        policy_settings({"close_integrated_prs": "true"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha": "short"},
        {"integration_sha": "short"},
        {"integration_sha": MAIN.upper()},
        {"number": 0},
        {"number": -1},
        {"number": True},
        {"reason": "obsolete"},
        {"reason": None},
        {"repo": "../bad/repo"},
    ],
)
def test_invalid_parameters_never_read_or_write(overrides):
    t = Transport()
    assert apply(t, action(**overrides))["receipts"][0]["status"] == "denied"
    assert not t.reads and not t.writes


@pytest.mark.parametrize("repos", ["*", ["*"], [None], [REPO, "../bad/repo"], {}])
def test_malformed_scopes_are_not_wildcards(repos):
    t = Transport()
    assert apply(t, cfg=config(repos=repos))["receipts"][0]["status"] == "denied"
    assert not t.reads and not t.writes


def test_integrated_draft_or_stack_can_close_with_exact_readback():
    t = Transport()
    receipt = apply(t)["receipts"][0]
    assert receipt["status"] == "applied"
    assert receipt["readback"] == {
        "closed": True,
        "merged": False,
        "head": SHA,
        "integration_sha": MAIN,
        "reason": "integrated",
        "account": "example-bot",
    }
    assert t.writes == [("PATCH", ENDPOINT, {"state": "closed"})]
    assert t.reads.count(ENDPOINT) == 3
    assert t.reads.count("user") == 2


def test_identical_head_and_default_branch_can_close():
    t = Transport()
    t.ref["object"]["sha"] = SHA
    t.proof["status"] = "identical"
    assert apply(t, action(integration_sha=SHA))["receipts"][0]["status"] == "applied"


@pytest.mark.parametrize(
    "change",
    [
        {"status": "diverged"},
        {"status": "behind"},
        {"merge_base_commit": {"sha": OTHER}},
        {"base_commit": {"sha": OTHER}},
        {"merge_base_commit": None},
        {"status": "identical"},
    ],
)
def test_unproven_ancestry_never_closes(change):
    t = Transport()
    t.proof.update(change)
    assert apply(t)["receipts"][0]["status"] == "failed"
    assert not t.writes


@pytest.mark.parametrize(
    "change",
    [
        {"head": {"sha": OTHER}},
        {"head": None},
        {"state": "closed"},
        {"merged": True},
        {"merged": None},
        {"number": 8},
        {"base": {"repo": {"full_name": "foreign/repo"}}},
    ],
)
def test_initial_and_last_moment_head_or_target_changes_stop_before_patch(change):
    for last_moment in [False, True]:
        t = Transport()
        changed = {**t.pull, **change}
        if last_moment:
            t.pull_answers = [copy.deepcopy(t.pull), changed]
        else:
            t.pull = changed
        assert apply(t)["receipts"][0]["status"] == "failed"
        assert not t.writes


def test_identity_default_branch_movement_and_api_errors_fail_closed():
    for last_moment in [False, True]:
        t = Transport()
        changed = {"object": {"sha": OTHER, "type": "commit"}}
        t.ref_answers = [copy.deepcopy(t.ref), changed] if last_moment else [changed]
        assert apply(t)["receipts"][0]["status"] == "failed"
        assert not t.writes
    for endpoint in ["user", f"repos/{REPO}", ENDPOINT, f"repos/{REPO}/compare/{SHA}...{MAIN}"]:
        t = Transport()
        t.errors[endpoint] = "unreadable"
        assert apply(t)["receipts"][0]["status"] == "failed"
        assert not t.writes
    t = Transport()
    t.login = "other-account"
    assert apply(t)["receipts"][0]["status"] == "failed"
    assert not t.writes


def test_write_timeout_or_readback_race_never_claims_success_or_retries():
    t = Transport()
    t.write_error = "timeout"
    assert apply(t)["receipts"][0]["status"] == "failed"
    assert len(t.writes) == 1
    assert apply(t)["receipts"][0]["status"] == "failed"
    assert len(t.writes) == 1
    for changes in [{"head": {"sha": OTHER}}, {"merged": True}, {"number": 8}]:
        t = Transport()
        t.readback_changes = changes
        assert apply(t)["receipts"][0]["status"] == "failed"
        assert len(t.writes) == 1


def test_plan_binding_duplicate_receipt_and_direct_executor_permission(tmp_path):
    t = Transport()
    result = apply(t, expected_plan_sha256="wrong")
    assert result["writes"] == 0 and not t.reads and not t.writes
    store = tmp_path / "receipts.jsonl"
    assert apply(t, receipt_store=store)["receipts"][0]["status"] == "applied"
    assert apply(t, receipt_store=store)["receipts"][0]["status"] == "skipped_duplicate"
    assert len(t.writes) == 1
    t = Transport()
    _, error = typed_actions.exec_close(t, action().params, config(enabled=False))
    assert error and not t.reads and not t.writes
