"""Formal reviews stay opt-in, commit-bound and fail closed on ambiguous writes."""

from __future__ import annotations

import copy
import json

import pytest

from githubbot_core import actions, cli
from githubbot_core.actions import Action
from githubbot_core.config import default_config, merge_layers
from githubbot_core.multibot import permission_contract

REPO = "example-org/example-repo"
SHA = "a" * 40
OTHER = "b" * 40
ENDPOINT = f"repos/{REPO}/pulls/7"
STATES = {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}


def config(*, enabled=True, publish=True, account="example-bot"):
    return merge_layers(
        [
            ("default", default_config()),
            (
                "instance",
                {
                    "permissions": {"enabled": ["pr.review"] if enabled else []},
                    "multi_bot": {"merge_account": account, "policy": {"publish_reviews": publish}},
                },
            ),
        ]
    )


def action(**overrides):
    return Action("pr.review", {"repo": REPO, "number": 7, "sha": SHA, "event": "APPROVE",
                                "body": "Complete independent review; tests passed.", **overrides})


class Transport:
    def __init__(self):
        self.login = "example-bot"
        self.pull = {"state": "open", "head": {"sha": SHA}, "draft": False, "user": {"login": "author"}}
        self.pull_answers = []
        self.reviews = []
        self.list_error = None
        self.write_error = None
        self.id = 81
        self.created = None
        self.readback_override = {}
        self.readback_error = None
        self.writes = []
        self.reads = []

    def get(self, endpoint):
        self.reads.append(endpoint)
        if endpoint == "user":
            return {"login": self.login}, None
        if endpoint == ENDPOINT:
            return copy.deepcopy(self.pull_answers.pop(0) if self.pull_answers else self.pull), None
        if endpoint == f"{ENDPOINT}/reviews/81":
            return {**self.created, **self.readback_override}, self.readback_error
        raise AssertionError(f"unexpected GET {endpoint}")

    def get_paginated(self, endpoint):
        assert endpoint == f"{ENDPOINT}/reviews?per_page=100"
        return copy.deepcopy(self.reviews), self.list_error

    def request(self, method, endpoint, body):
        assert (method, endpoint) == ("POST", f"{ENDPOINT}/reviews")
        self.writes.append((method, endpoint, body))
        self.created = {"id": self.id, "commit_id": body["commit_id"], "body": body["body"],
                        "state": STATES[body["event"]], "user": {"login": self.login}}
        # A timed-out POST can have created a real review. Subsequent calls must discover it.
        self.reviews.append(copy.deepcopy(self.created))
        return self.created, self.write_error


def apply(transport, review=None, cfg=None):
    return actions.apply([review or action()], transport, cfg or config())["receipts"][0]


def test_review_requires_permission_and_policy_and_never_runs_from_preview():
    review = action()
    assert "pr.review" in default_config()["permissions"]["ceiling"]
    assert "pr.review" not in default_config()["permissions"]["enabled"]
    assert "not enabled" in actions.denial_reason(review, config(enabled=False))
    assert "switched off" in actions.denial_reason(review, config(publish=False))
    contract = permission_contract(config(), [])
    assert contract["permissions"]["effective_enabled"] == ["pr.review"]
    assert actions.preview([review], config())["writes"] == 0
    assert actions.denial_reason(review, {"permissions": {"enabled": ["pr.review"], "ceiling": []}})


@pytest.mark.parametrize("overrides", [
    {"sha": "short"}, {"sha": OTHER.upper()}, {"number": 0}, {"number": -1},
    {"repo": "../wrong/repo"}, {"event": "PENDING"}, {"event": "approve"}, {"event": []},
    {"body": " "}, {"body": None}, {"body": {}}, {"body": "x" * 65537},
])
def test_invalid_review_parameters_are_denied_without_api_calls(overrides):
    transport = Transport()
    receipt = apply(transport, action(**overrides))
    assert receipt["status"] == "denied"
    assert transport.reads == transport.writes == []


@pytest.mark.parametrize("event,state", STATES.items())
def test_formal_review_is_submitted_at_the_exact_sha_and_independently_read_back(event, state):
    transport = Transport()
    review = action(event=event, body="Geprüft: Änderungen vollständig gelesen.")
    receipt = apply(transport, review)
    assert receipt["status"] == "applied"
    assert transport.writes == [("POST", f"{ENDPOINT}/reviews", {
        "commit_id": SHA, "event": event, "body": review.params["body"]})]
    assert receipt["readback"] == {"review_id": 81, "head": SHA, "state": state, "account": "example-bot"}
    assert f"{ENDPOINT}/reviews/81" in transport.reads


@pytest.mark.parametrize("pull", [
    {"state": "closed"}, {"head": {"sha": OTHER}}, {"head": None}, {"draft": True},
    {"user": {"login": "Example-Bot"}}, {"user": None}, {"user": {"login": ""}}, {"draft": None},
])
def test_approval_refuses_unreviewed_closed_draft_or_own_pr(pull):
    transport = Transport()
    transport.pull.update(pull)
    assert apply(transport)["status"] == "failed"
    assert not transport.writes


def test_comment_may_review_a_draft_or_stacked_pull_without_enabling_merge():
    transport = Transport()
    transport.pull.update(draft=True, base={"ref": "feature"})
    assert apply(transport, action(event="COMMENT"))["status"] == "applied"
    assert transport.writes[0][1].endswith("/reviews")


def test_review_requires_the_configured_account():
    transport = Transport()
    assert apply(transport, cfg=config(account=""))["status"] == "denied"
    transport.login = "someone-else"
    assert apply(transport)["status"] == "failed"
    assert not transport.writes


@pytest.mark.parametrize("existing", [
    {"id": 12, "user": {"login": "example-bot"}, "commit_id": SHA, "state": "APPROVED"},
    {"id": 13, "user": {"login": "Example-Bot"}, "commit_id": SHA, "state": "PENDING"},
    {"id": 14, "user": None, "commit_id": SHA},
])
def test_existing_or_unreadable_reviews_prevent_a_second_write(existing):
    transport = Transport()
    transport.reviews = [existing]
    assert apply(transport)["status"] == "failed"
    assert not transport.writes


def test_reviews_on_old_heads_or_from_other_actors_do_not_block():
    transport = Transport()
    transport.reviews = [{"user": {"login": "example-bot"}, "commit_id": OTHER},
                         {"user": {"login": "other-reviewer"}, "commit_id": SHA}]
    assert apply(transport)["status"] == "applied"


def test_review_list_failure_is_not_an_empty_list():
    transport = Transport()
    transport.list_error = "403"
    assert apply(transport)["status"] == "failed"
    assert not transport.writes


@pytest.mark.parametrize("reviews", [None, {}, [None], [{"user": {"login": ""}, "commit_id": SHA}]])
def test_malformed_review_list_is_a_hold(reviews):
    transport = Transport()
    transport.reviews = reviews
    assert apply(transport)["status"] == "failed"
    assert not transport.writes


@pytest.mark.parametrize("review_id", [None, True, 0, "81"])
def test_post_without_a_usable_review_id_is_ambiguous_and_never_retried(review_id):
    transport = Transport()
    transport.id = review_id
    receipt = apply(transport)
    assert receipt["status"] == "failed" and "read existing reviews" in receipt["detail"]
    assert len(transport.writes) == 1


def test_head_moving_during_pagination_prevents_post():
    transport = Transport()
    transport.pull_answers = [transport.pull, {**transport.pull, "head": {"sha": OTHER}}]
    assert apply(transport)["status"] == "failed"
    assert not transport.writes


@pytest.mark.parametrize("override", [
    {"commit_id": OTHER}, {"body": "different"}, {"state": "PENDING"}, {"id": 82},
    {"user": {"login": "another"}}, {"user": None},
])
def test_readback_must_match_created_review_commit_body_state_and_actor(override):
    transport = Transport()
    transport.readback_override = override
    receipt = apply(transport)
    assert receipt["status"] == "failed" and "id=81" in receipt["detail"]
    assert len(transport.writes) == 1


def test_unreadable_readback_reports_the_created_id_and_does_not_repeat_post():
    transport = Transport()
    transport.readback_error = "Timeout"
    receipt = apply(transport)
    assert receipt["status"] == "failed" and "id=81" in receipt["detail"]
    assert len(transport.writes) == 1


def test_head_moving_after_post_never_reports_a_current_head_approval():
    transport = Transport()
    transport.pull_answers = [transport.pull, transport.pull, {**transport.pull, "head": {"sha": OTHER}}]
    receipt = apply(transport)
    assert receipt["status"] == "failed" and "target changed" in receipt["detail"]
    assert len(transport.writes) == 1


def test_timeout_after_creation_is_discovered_on_retry_even_without_receipt_store():
    transport = Transport()
    transport.write_error = "Timeout"
    assert apply(transport)["status"] == "failed"
    transport.write_error = None
    assert apply(transport)["status"] == "failed"
    assert len(transport.writes) == 1


def test_successful_review_is_deduplicated_by_receipt_store(tmp_path):
    transport = Transport()
    store = tmp_path / "receipts.jsonl"
    first = actions.apply([action()], transport, config(), receipt_store=store)
    second = actions.apply([action()], transport, config(), receipt_store=store)
    assert first["receipts"][0]["status"] == "applied"
    assert second["receipts"][0]["status"] == "skipped_duplicate"
    assert len(transport.writes) == 1


def test_cli_preview_plan_mismatch_and_apply_use_the_same_review_contract(tmp_path, capsys):
    layer = tmp_path / "instance.json"
    layer.write_text(json.dumps(config()), encoding="utf-8")
    transport = Transport()
    argv = ["--config", str(layer), "--json", "act", "pr.review", "--params", json.dumps(action().params)]
    assert cli.main(argv, transport=transport) == 0
    plan = json.loads(capsys.readouterr().out)["data"]
    assert plan["actions"][0]["permitted"] and not transport.writes
    cli.main([*argv, "--apply", "--expect-plan", "wrong"], transport=transport)
    assert not transport.writes
    capsys.readouterr()
    assert cli.main([*argv, "--apply", "--expect-plan", plan["plan_sha256"]], transport=transport) == 0
    assert json.loads(capsys.readouterr().out)["data"]["receipts"][0]["status"] == "applied"
