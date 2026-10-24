"""Formal review writes obey the portable MULTI-BOT organization policy."""

from __future__ import annotations

import json

import pytest

from githubbot_core import actions, typed_actions
from githubbot_core.actions import Action
from githubbot_core.config import default_config, merge_layers

SHA = "a" * 40
OTHER_SHA = "b" * 40
BOT = "example-bot"


def orgs_document():
    return {
        "organizations": [
            {
                "name": "research-line",
                "type": "organization",
                "enabled": True,
                "features": ["pr-review"],
                "exclude_writes": ["fst-nash"],
            },
            {
                "name": "dev-bricks",
                "type": "organization",
                "enabled": True,
                "features": ["pr-review"],
                "exclude_writes": ["automizer-for-claude-desktop"],
            },
            {"name": "file-bricks", "type": "organization", "enabled": True, "features": ["pr-review"]},
            {
                "name": "lukisch",
                "type": "personal",
                "enabled": True,
                "features": ["pr-review"],
            },
        ],
        "global_exclude_writes": [".github"],
    }


def write_orgs(root, document=None):
    path = root / "config" / "orgs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document if document is not None else orgs_document()), encoding="utf-8")
    return path


def review_config(root, *, enabled=True, publish_reviews=True):
    layer = {
        "permissions": {"enabled": ["pr.review"] if enabled else []},
        "multi_bot": {
            "root": str(root),
            "merge_account": BOT,
            "policy": {"publish_reviews": publish_reviews},
        },
    }
    return merge_layers([("default", default_config()), ("instance", layer)])


def review_action(repo):
    return Action(
        "pr.review",
        {"repo": repo, "number": 7, "sha": SHA, "event": "COMMENT", "body": "Independent review receipt."},
    )


class FakeTransport:
    """Offline transport that records every call and never opens a socket."""

    def __init__(self, *, identity=BOT, head_sha=SHA):
        self.identity = identity
        self.head_sha = head_sha
        self.calls = []
        self.repo = "dev-bricks/githubbot-core"
        self.pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": head_sha},
            "user": {"login": "different-author"},
        }
        self.review = {
            "id": 23,
            "commit_id": SHA,
            "state": "COMMENTED",
            "body": "Independent review receipt.",
            "user": {"login": BOT},
            "html_url": "https://example.invalid/review/23",
        }

    def get(self, endpoint):
        self.calls.append(("GET", endpoint))
        if endpoint == "user":
            return {"login": self.identity}, None
        if endpoint.endswith("/pulls/7"):
            return self.pull, None
        if endpoint.endswith("/pulls/7/reviews/23"):
            return self.review, None
        return None, f"unexpected fake GET {endpoint}"

    def get_paginated(self, endpoint):
        self.calls.append(("GET_PAGINATED", endpoint))
        return [], None

    def request(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, body))
        if method == "POST" and endpoint.endswith("/pulls/7/reviews"):
            return {"id": "23"}, None
        return None, f"unexpected fake write {method} {endpoint}"


class PolicyChangingTransport(FakeTransport):
    def __init__(self, root):
        super().__init__()
        self.root = root

    def get_paginated(self, endpoint):
        self.calls.append(("GET_PAGINATED", endpoint))
        document = orgs_document()
        document["organizations"][1]["exclude_writes"].append("dev-bricks/githubbot-core")
        write_orgs(self.root, document)
        return [], None


@pytest.mark.parametrize(
    ("repo", "message"),
    [
        ("RESEARCH-LINE/FST-NASH", "organization-excluded"),
        ("DEV-BRICKS/.GITHUB", "globally excluded"),
        ("dev-bricks/automizer-for-claude-desktop", "organization-excluded"),
    ],
)
def test_configured_exclusions_block_preview_apply_direct_execution_and_replay(tmp_path, repo, message):
    write_orgs(tmp_path)
    config = review_config(tmp_path)
    action = review_action(repo)
    transport = FakeTransport()

    preview = actions.preview([action], config)
    assert preview["actions"][0]["permitted"] is False
    assert message in preview["actions"][0]["denied_reason"]

    receipt_store = tmp_path / "receipts.jsonl"
    receipt_store.write_text(json.dumps({"id": action.action_id, "status": "applied"}) + "\n", encoding="utf-8")
    applied = actions.apply([action], transport, config, receipt_store=receipt_store)
    assert applied["receipts"][0]["status"] == "denied"
    assert message in applied["receipts"][0]["detail"]
    assert transport.calls == []

    _, direct_error = typed_actions.exec_review(transport, action.params, config)
    assert direct_error and message in direct_error
    assert transport.calls == []


def test_allowed_control_repo_submits_exact_head_review_via_fake_transport(tmp_path):
    write_orgs(tmp_path)
    config = review_config(tmp_path)
    action = review_action("dev-bricks/githubbot-core")
    transport = FakeTransport()

    assert actions.preview([action], config)["actions"][0]["permitted"] is True
    result = actions.apply([action], transport, config)

    assert result["receipts"][0]["status"] == "applied"
    assert result["receipts"][0]["readback"]["commit_id"] == SHA
    assert [call for call in transport.calls if call[0] == "POST"] == [
        (
            "POST",
            "repos/dev-bricks/githubbot-core/pulls/7/reviews",
            {"commit_id": SHA, "event": "COMMENT", "body": "Independent review receipt."},
        )
    ]


def test_full_owner_repo_exclusion_is_enforced_and_absent_org_exclusions_are_valid(tmp_path):
    document = orgs_document()
    document["organizations"][1]["exclude_writes"].append("dev-bricks/githubbot-core")
    write_orgs(tmp_path, document)
    config = review_config(tmp_path)

    assert "organization-excluded" in actions.denial_reason(review_action("dev-bricks/githubbot-core"), config)
    assert actions.denial_reason(review_action("file-bricks/unexcluded-control"), config) is None


def test_scope_is_reloaded_immediately_before_review_post(tmp_path):
    write_orgs(tmp_path)
    config = review_config(tmp_path)
    transport = PolicyChangingTransport(tmp_path)

    result = actions.apply([review_action("dev-bricks/githubbot-core")], transport, config)

    assert result["receipts"][0]["status"] == "failed"
    assert "organization-excluded" in result["receipts"][0]["detail"]
    assert not [call for call in transport.calls if call[0] == "POST"]


@pytest.mark.parametrize(
    ("repo", "mutate", "message"),
    [
        ("unknown-org/repo", None, "unknown namespace"),
        ("lukisch/repo", None, "non-organization"),
        (
            "research-line/other",
            lambda doc: doc["organizations"][0]["features"].remove("pr-review"),
            "without the pr-review",
        ),
        ("dev-bricks/repo", lambda doc: doc["organizations"][1].update(enabled=False), "disabled organization"),
    ],
)
def test_noneligible_namespace_denies_without_transport_calls(tmp_path, repo, mutate, message):
    document = orgs_document()
    if mutate:
        mutate(document)
    write_orgs(tmp_path, document)
    config = review_config(tmp_path)
    transport = FakeTransport()

    denial = actions.denial_reason(review_action(repo), config)

    assert denial and message in denial
    assert actions.apply([review_action(repo)], transport, config)["receipts"][0]["status"] == "denied"
    assert transport.calls == []


@pytest.mark.parametrize(
    "document_text",
    [
        None,
        "{not-json",
        json.dumps({"organizations": orgs_document()["organizations"]}),
        json.dumps({"organizations": "dev-bricks", "global_exclude_writes": []}),
        json.dumps({"organizations": [], "global_exclude_writes": []}),
        json.dumps({"organizations": orgs_document()["organizations"], "global_exclude_writes": ".github"}),
    ],
)
def test_missing_or_invalid_required_policy_fails_closed(tmp_path, document_text):
    if document_text is not None:
        path = tmp_path / "config" / "orgs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document_text, encoding="utf-8")
    config = review_config(tmp_path)
    transport = FakeTransport()

    denial = actions.denial_reason(review_action("dev-bricks/githubbot-core"), config)

    assert denial and "review write scope denied" in denial
    assert (
        actions.apply([review_action("dev-bricks/githubbot-core")], transport, config)["receipts"][0]["status"]
        == "denied"
    )
    assert transport.calls == []


def test_duplicate_json_keys_fail_closed(tmp_path):
    path = tmp_path / "config" / "orgs.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"global_exclude_writes": [], "global_exclude_writes": [".github"]}', encoding="utf-8")
    config = review_config(tmp_path)

    denial = actions.denial_reason(review_action("dev-bricks/githubbot-core"), config)

    assert denial and "duplicate keys" in denial


def test_runtime_root_defaults_to_current_directory_when_not_configured(tmp_path, monkeypatch):
    write_orgs(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = review_config(tmp_path)
    config["multi_bot"].pop("root")

    assert "organization-excluded" in actions.denial_reason(review_action("research-line/fst-nash"), config)


def test_permission_and_publish_switch_remain_independent_of_scope(tmp_path):
    write_orgs(tmp_path)
    action = review_action("dev-bricks/githubbot-core")
    no_permission = review_config(tmp_path, enabled=False, publish_reviews=True)
    switch_off = review_config(tmp_path, enabled=True, publish_reviews=False)
    transport = FakeTransport()

    assert "not enabled" in actions.denial_reason(action, no_permission)
    assert "switched off" in actions.denial_reason(action, switch_off)
    assert transport.calls == []

    # Publishing reviews does not turn on the distinct merge switch.
    assert "switched off" in typed_actions.switch_denial("pr.merge", review_config(tmp_path))


@pytest.mark.parametrize(
    ("identity", "head_sha", "expected"),
    [("someone-else", SHA, "identity"), (BOT, OTHER_SHA, "head moved")],
)
def test_identity_and_exact_head_guards_still_precede_review_post(tmp_path, identity, head_sha, expected):
    write_orgs(tmp_path)
    config = review_config(tmp_path)
    transport = FakeTransport(identity=identity, head_sha=head_sha)

    result = actions.apply([review_action("dev-bricks/githubbot-core")], transport, config)

    assert result["receipts"][0]["status"] == "failed"
    assert expected in result["receipts"][0]["detail"]
    assert not [call for call in transport.calls if call[0] == "POST"]
