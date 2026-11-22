"""A stop signal from any reviewing instance holds approve and merge until it is lifted.

Fixtures mirror lock-master#8 (2026-09-22, "NICHT MERGEN" review) and promptboard#8
(2026-11-22, STOPP comment under the owner account, bot approve + merge six minutes
later). Accounts and repositories are synthetic.
"""

from __future__ import annotations

from githubbot_core import actions, stop_gate
from githubbot_core.actions import Action
from githubbot_core.config import default_config, merge_layers

REPO = "example-org/example-repo"
SHA = "a" * 40
NEW = "c" * 40
COMMENTS = f"repos/{REPO}/issues/7/comments?per_page=100"
REVIEWS = f"repos/{REPO}/pulls/7/reviews?per_page=100"


def comment(login, at, body):
    return {"user": {"login": login}, "created_at": at, "body": body}


def review(login, at, state, body="", commit=SHA):
    return {"user": {"login": login}, "submitted_at": at, "state": state, "body": body, "commit_id": commit}


PROMPTBOARD_8 = [
    comment("github-actions[bot]", "2026-11-22T19:40:09Z", "Welcome! A maintainer will review it soon."),
    comment(
        "owner-account", "2026-11-22T19:57:09Z", f"## Zweitprüfung Head `{SHA[:7]}`\n**Urteil: STOPP – nicht gemergt.**"
    ),
]
LOCK_MASTER_8 = [review("review-instance", "2026-09-22T10:00:00Z", "COMMENTED", "Befund: NICHT MERGEN, Tests rot.")]


def config(**policy):
    layer = {
        "permissions": {"enabled": ["pr.merge", "pr.review"], "write_owners": ["example-org"]},
        "multi_bot": {
            "merge_enabled": True,
            "merge_account": "example-bot",
            "policy": {"publish_reviews": True, **policy},
        },
    }
    return merge_layers([("default", default_config()), ("instance", layer)])


class Transport:
    def __init__(self, comments=(), reviews=()):
        pull = {
            "number": 7,
            "state": "open",
            "draft": False,
            "node_id": "PR_node",
            "head": {"sha": SHA},
            "base": {"ref": "main"},
            "mergeable_state": "clean",
            "merged": False,
            "user": {"login": "author"},
        }
        self.pulls = [pull, {**pull, "merged": True, "merge_commit_sha": "b" * 40}]
        self.lists = {COMMENTS: list(comments), REVIEWS: list(reviews)}
        self.writes = []

    def get(self, endpoint):
        if endpoint == "user":
            return {"login": "example-bot"}, None
        if endpoint == f"repos/{REPO}":
            return {"default_branch": "main", "full_name": REPO}, None
        if endpoint == f"repos/{REPO}/pulls/7":
            return self.pulls[-1 if self.writes else 0], None
        if endpoint.startswith(f"repos/{REPO}/pulls/7/reviews/"):
            return {**self.writes[-1][2], "id": 81, "user": {"login": "example-bot"}, "state": "APPROVED"}, None
        return None, f"404 {endpoint}"

    def get_paginated(self, endpoint):
        return (list(self.lists[endpoint]), None) if endpoint in self.lists else ([], f"404 {endpoint}")

    def request(self, method, endpoint, body=None):
        self.writes.append((method, endpoint, body))
        return {"id": 81}, None


def merge(transport, cfg=None):
    return actions.apply([Action("pr.merge", {"repo": REPO, "number": 7, "sha": SHA})], transport, cfg or config())


def approve(transport):
    params = {"repo": REPO, "number": 7, "sha": SHA, "event": "APPROVE", "body": "Independent review passed."}
    return actions.apply([Action("pr.review", params)], transport, config())


def test_promptboard_8_stop_comment_under_foreign_account_holds_approve_and_merge():
    for run in (approve, merge):
        transport = Transport(PROMPTBOARD_8)
        receipt = run(transport)["receipts"][0]
        assert receipt["status"] == "failed" and "stop signal by 'owner-account'" in receipt["detail"]
        assert not [w for w in transport.writes if w[0] in ("PUT", "POST")]


def test_lock_master_8_nicht_mergen_review_holds_merge_even_after_bot_approve():
    reviews = [*LOCK_MASTER_8, review("example-bot", "2026-09-22T10:05:00Z", "APPROVED", "CI green.")]
    transport = Transport(reviews=reviews)
    assert merge(transport)["receipts"][0]["status"] == "failed"
    assert transport.writes == []


def test_later_release_comment_by_the_same_account_lifts_the_stop():
    lifted = [*PROMPTBOARD_8, comment("owner-account", "2026-11-22T20:30:00Z", "STOPP aufgehoben, Nachmessung grün.")]
    transport = Transport(lifted)
    assert merge(transport)["receipts"][0]["status"] == "applied"
    assert transport.writes[0][:2] == ("PUT", f"repos/{REPO}/pulls/7/merge")


def test_release_rules():
    stop = comment("instance", "2026-01-01T10:00:00Z", f"STOPP for head {SHA[:7]}")
    # A foreign account cannot lift; a configured owner account can.
    other = comment("someone", "2026-01-01T11:00:00Z", "freigegeben")
    assert stop_gate.evaluate([stop, other], [])
    assert stop_gate.evaluate([stop, {**other, "user": {"login": "Owner"}}], [], ["owner"]) is None
    # Later approve of the same instance lifts only on another head than the one the stop named.
    assert stop_gate.evaluate([stop], [review("instance", "2026-01-01T12:00:00Z", "APPROVED")])
    assert stop_gate.evaluate([stop], [review("instance", "2026-01-01T12:00:00Z", "APPROVED", commit=NEW)]) is None
    # Order matters: a release before the stop does not count.
    early = comment("instance", "2026-01-01T09:00:00Z", "STOPP aufgehoben")
    assert stop_gate.evaluate([early, stop], [])
    # "nicht freigegeben" is no release; dismissed CHANGES_REQUESTED is no stop.
    assert stop_gate.evaluate([stop, comment("instance", "2026-01-01T13:00:00Z", "nicht freigegeben")], [])
    assert stop_gate.evaluate([], [review("x", "2026-01-01T10:00:00Z", "DISMISSED", "do not merge")]) is None
    assert stop_gate.evaluate([], [review("x", "2026-01-01T10:00:00Z", "CHANGES_REQUESTED")])


def test_without_stop_signal_merge_and_approve_proceed():
    plain = [comment("github-actions[bot]", "2026-11-22T19:40:09Z", "Welcome! Kein STOPP-Befund, stop the job later.")]
    assert merge(Transport(plain))["receipts"][0]["status"] == "applied"
    assert approve(Transport(plain))["receipts"][0]["status"] == "applied"


def test_unreadable_comments_fail_closed():
    transport = Transport(PROMPTBOARD_8)
    del transport.lists[COMMENTS]
    assert "unreadable" in merge(transport)["receipts"][0]["detail"]
    assert stop_gate.evaluate([{"user": {"login": "x"}, "body": "ok"}], [])  # missing timestamp
