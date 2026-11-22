"""A stop signal from any reviewing instance holds approve and merge until it is lifted.

Fixtures mirror lock-master#8 (2026-09-22, "NICHT MERGEN" review) and promptboard#8
(2026-11-22, STOPP comment under the owner account, bot approve + merge six minutes
later). The probe table includes every case of the independent review of this gate,
among them the seven texts and approves that wrongly lifted a stop. Accounts and
repositories are synthetic.
"""

from __future__ import annotations

import pytest

from githubbot_core import actions, stop_gate
from githubbot_core.actions import Action
from githubbot_core.config import default_config, merge_layers

REPO = "example-org/example-repo"
SHA = "a" * 40
NEW = "c" * 40  # descendant of SHA in every fake history below
COMMENTS = f"repos/{REPO}/issues/7/comments?per_page=100"
INLINE = f"repos/{REPO}/pulls/7/comments?per_page=100"
REVIEWS = f"repos/{REPO}/pulls/7/reviews?per_page=100"


def comment(login, at, body):
    return {"user": {"login": login}, "created_at": at, "body": body}


def review(login, at, state, body="", commit=SHA):
    return {"user": {"login": login}, "submitted_at": at, "state": state, "body": body, "commit_id": commit}


def newer(old, new):
    return SHA.startswith(old) and new == NEW


STOP = comment("instance", "2026-01-01T10:00:00Z", f"Head `{SHA[:7]}`: **STOPP**, Tests rot.")
STOP_NO_SHA = comment("instance", "2026-01-01T10:00:00Z", "STOPP, Tests rot.")


def later(body, login="instance"):
    return comment(login, "2026-01-01T11:00:00Z", body)


def approved(login="instance", commit=SHA):
    return review(login, "2026-01-01T12:00:00Z", "APPROVED", "Retest passed.", commit)


PROBES = {
    # wrongly lifted before (review finding): negated or conditional texts, a question
    "negated_far": ([STOP, later("weiterhin nicht fuer den Merge freigegeben, Tests rot.")], [], True),
    "nicht_mehr": ([STOP, later("Das ist nicht mehr freigegeben.")], [], True),
    "double_space": ([STOP, later("noch nicht  freigegeben")], [], True),
    "not_yet": ([STOP, later("not yet freigegeben")], [], True),
    "conditional": ([STOP, later("freigegeben wird erst nach Retest")], [], True),
    "question": ([STOP, later("Ist der STOPP aufgehoben?")], [], True),
    "no_sha_same_head_approve": ([STOP_NO_SHA], [approved()], True),
    # further holds
    "same_head_approve": ([STOP], [approved()], True),
    "no_sha_newer_head_approve": ([STOP_NO_SHA], [approved(commit=NEW)], True),
    "merge_account_approve": ([{**STOP, "user": {"login": "example-bot"}}], [approved("example-bot", NEW)], True),
    "foreign_release": ([STOP, later("STOPP aufgehoben", login="someone")], [], True),
    "foreign_approve_new_head": ([STOP], [approved("someone", NEW)], True),
    "owner_release_then_new_stop": (
        [STOP, later("STOPP aufgehoben", login="owner"), comment("instance", "2026-01-01T13:00:00Z", "NICHT MERGEN")],
        [],
        True,
    ),
    "changes_requested_same_head": (
        [],
        [review("instance", "2026-01-01T10:00:00Z", "CHANGES_REQUESTED"), approved()],
        True,
    ),
    "release_line_plus_stop_line": ([STOP, later("STOPP aufgehoben\nAber: nicht mergen, Retest offen.")], [], True),
    "deleted_user": ([STOP, {**later("STOPP aufgehoben"), "user": None}], [], True),
    "review_without_submitted_at": ([], [{**review("x", "", "APPROVED"), "submitted_at": None}], True),
    "body_null": ([{**later("x"), "body": None}], [], True),
    "non_dict_item": ([STOP, "STOPP aufgehoben"], [], True),
    # round 2: quoted or fenced formula lines are no release
    "quote_reply_contradiction": ([STOP, later("> Merge freigegeben\n\nNein, noch nicht.")], [], True),
    "quote_only": ([STOP, later("> STOPP aufgehoben")], [], True),
    "fenced_formula": ([STOP, later("```\nSTOPP aufgehoben\n```")], [], True),
    "tilde_fenced_formula": ([STOP, later("~~~text\nSTOPP aufgehoben\n~~~")], [], True),
    # 0.2.2: an indented code block shows the formula instead of saying it
    "indented_code_formula": ([STOP, later("Beispiel:\n\n    STOPP aufgehoben")], [], True),
    "tab_indented_formula": ([STOP, later("\tMerge freigegeben")], [], True),
    # lifts
    "release_after_closed_fence": ([STOP, later("```\nlog\n```\nSTOPP aufgehoben")], [], False),
    "release_own_line": ([STOP, later("Nachmessung grün.\nSTOPP aufgehoben")], [], False),
    "release_markdown": ([STOP, later("**STOP lifted.**")], [], False),
    "approve_on_newer_head": ([STOP], [approved(commit=NEW)], False),
    "owner_release": ([STOP, later("Merge freigegeben", login="owner")], [], False),
    "no_stop_at_all": ([comment("bot", "2026-01-01T10:00:00Z", "Kein STOPP-Befund; stop the job later.")], [], False),
}


@pytest.mark.parametrize("name", PROBES)
def test_probe(name):
    comments, reviews, holds = PROBES[name]
    result = stop_gate.evaluate(comments, reviews, ["owner"], merge_account="example-bot", is_newer=newer)
    assert bool(result) is holds, result


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
    def __init__(self, comments=(), reviews=(), inline=(), **pull_overrides):
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
            "auto_merge": None,
            **pull_overrides,
        }
        self.pulls = [pull, {**pull, "merged": True, "merge_commit_sha": "b" * 40}]
        self.lists = {COMMENTS: list(comments), INLINE: list(inline), REVIEWS: list(reviews)}
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
        if endpoint.startswith(f"repos/{REPO}/compare/"):
            old, new = endpoint.rsplit("/", 1)[1].split("...")
            return {"status": "ahead" if newer(old, new) else "behind"}, None
        return None, f"404 {endpoint}"

    def get_paginated(self, endpoint):
        return (list(self.lists[endpoint]), None) if endpoint in self.lists else ([], f"404 {endpoint}")

    def request(self, method, endpoint, body=None):
        self.writes.append((method, endpoint, body))
        return {"id": 81}, None


PROMPTBOARD_8 = [
    comment("github-actions[bot]", "2026-11-22T19:40:09Z", "Welcome! A maintainer will review it soon."),
    comment(
        "owner-account", "2026-11-22T19:57:09Z", f"## Zweitprüfung Head `{SHA[:7]}`\n**Urteil: STOPP, nicht gemergt.**"
    ),
]
LOCK_MASTER_8 = [review("review-instance", "2026-09-22T10:00:00Z", "COMMENTED", "Befund: NICHT MERGEN, Tests rot.")]


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


def test_inline_review_comment_stop_holds_merge():
    inline = [{**comment("instance", "2026-01-01T10:00:00Z", "NICHT MERGEN"), "original_commit_id": SHA}]
    transport = Transport(reviews=[review("instance", "2026-01-01T10:00:01Z", "COMMENTED")], inline=inline)
    assert "stop signal by 'instance'" in merge(transport)["receipts"][0]["detail"]
    assert transport.writes == []


def test_later_release_line_by_the_same_account_lifts_the_stop():
    lifted = [*PROMPTBOARD_8, comment("owner-account", "2026-11-22T20:30:00Z", "Nachmessung grün.\nSTOPP aufgehoben")]
    transport = Transport(lifted)
    assert merge(transport)["receipts"][0]["status"] == "applied"
    assert transport.writes[0][:2] == ("PUT", f"repos/{REPO}/pulls/7/merge")


def test_approve_on_a_newer_head_is_proven_through_compare():
    stop = comment("instance", "2026-01-01T10:00:00Z", f"STOPP at {SHA[:7]}")
    lifted = Transport([stop], reviews=[approved(commit=NEW)])
    assert stop_gate.hold_reason(lifted, REPO, 7, config()) is None
    held = Transport([stop], reviews=[approved(commit="d" * 40)])
    assert stop_gate.hold_reason(held, REPO, 7, config())


def test_armed_auto_merge_is_reported_on_hold():
    transport = Transport(PROMPTBOARD_8, auto_merge={"enabled_by": {"login": "someone"}})
    detail = merge(transport)["receipts"][0]["detail"]
    assert "auto-merge is armed" in detail and "'someone'" in detail


def test_without_stop_signal_merge_and_approve_proceed():
    plain = [comment("github-actions[bot]", "2026-11-22T19:40:09Z", "Welcome! Kein STOPP-Befund, stop the job later.")]
    assert merge(Transport(plain))["receipts"][0]["status"] == "applied"
    assert approve(Transport(plain))["receipts"][0]["status"] == "applied"


FORMULA = "Nachmessung grün.\nSTOPP aufgehoben"
WRITES = [
    ("issue.comment", {"repo": REPO, "number": 7, "body": FORMULA}),
    ("pr.review", {"repo": REPO, "number": 7, "sha": SHA, "event": "COMMENT", "body": FORMULA}),
    ("api.request", {"method": "POST", "endpoint": f"repos/{REPO}/issues/7/comments", "body": {"body": FORMULA}}),
    ("api.request", {"method": "POST", "endpoint": f"/repos/{REPO}/pulls/7/reviews/", "body": {"body": FORMULA}}),
]


def fallback_config(**multi_bot):
    layer = {
        "permissions": {"enabled": ["issue.comment", "pr.review", "api.request"], "write_owners": ["example-org"]},
        "multi_bot": {"merge_account": "owner-account", "policy": {"publish_reviews": True}, **multi_bot},
    }
    return merge_layers([("default", default_config()), ("instance", layer)])


@pytest.mark.parametrize("kind,params", WRITES)
def test_bot_under_owner_account_never_posts_a_release_line(kind, params):
    for cfg in (
        fallback_config(owner_fallback=True),
        fallback_config(policy={"publish_reviews": True, "stop_release_accounts": ["Owner-Account"]}),
    ):
        assert "release line" in (stop_gate.write_denial(kind, params, cfg) or "")
        assert "release line" in (actions.denial_reason(Action(kind, params), cfg) or "")


def test_release_lines_stay_allowed_outside_the_owner_fallback():
    for kind, params in WRITES:
        assert stop_gate.write_denial(kind, params, fallback_config()) is None
    quoted = {"repo": REPO, "number": 7, "body": "> STOPP aufgehoben\n\nNoch offen."}
    assert stop_gate.write_denial("issue.comment", quoted, fallback_config(owner_fallback=True)) is None
    labels = {"repo": REPO, "number": 7, "labels": ["STOPP aufgehoben"]}
    assert stop_gate.write_denial("issue.add_labels", labels, fallback_config(owner_fallback=True)) is None


@pytest.mark.parametrize("endpoint", [COMMENTS, INLINE, REVIEWS])
def test_unreadable_lists_fail_closed(endpoint):
    transport = Transport(PROMPTBOARD_8)
    del transport.lists[endpoint]
    assert "unreadable" in merge(transport)["receipts"][0]["detail"]
    assert transport.writes == []
