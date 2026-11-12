"""Offline transport, crash and cooperating-process guarantees."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from githubbot_core import coordination
from githubbot_core.coordination import Coordinator, Hold
from githubbot_core.transport import GhCliTransport


def reply(status=200, headers="", body='{"ok": true}', error=""):
    return SimpleNamespace(
        returncode=int(status >= 400), stdout=f"HTTP/2.0 {status} Synthetic\r\n{headers}\r\n{body}", stderr=error
    )


@pytest.mark.parametrize(
    "status,headers,error,policy",
    [
        (429, "Retry-After: 120\r\n", "", False),
        (403, "X-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 1300\r\n", "", False),
        (403, "", "secondary rate limit", False),
        (403, "", "Forbidden", True),
        (429, "Retry-After: invalid\r\n", "", True),
    ],
)
def test_shared_rate_hold_stops_next_process_transport(tmp_path, status, headers, error, policy):
    c = Coordinator(tmp_path / "rate", clock=lambda: 1000)
    calls = []

    def run(*args, **kw):
        calls.append(1)
        return reply(status, headers, error=error)

    t = GhCliTransport(runner=run, coordinator=c)
    assert t.get("user")[1]
    other = GhCliTransport(runner=run, coordinator=Coordinator(c.root, clock=lambda: 1001))
    assert other.get("user")[1] == "GitHub coordination HOLD; no request sent"
    assert len(calls) == 1
    assert bool(c.load("github-com-rate").get("policy_hold")) == policy


def test_cooldown_expiry_and_no_automatic_retry(tmp_path):
    now = [1000]
    c = Coordinator(tmp_path / "rate", clock=lambda: now[0])
    calls = []

    def run(*args, **kw):
        calls.append(1)
        return reply(429, "Retry-After: 60\r\n") if len(calls) == 1 else reply()

    t = GhCliTransport(runner=run, coordinator=c)
    assert t.get("user")[1] and len(calls) == 1
    now[0] = 1059
    assert t.get("user")[1] and len(calls) == 1
    now[0] = 1060
    assert t.get("user") == ({"ok": True}, None)
    assert len(calls) == 2


def test_mutation_spacing_and_body_is_not_rate_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(coordination, "MUTATION_INTERVAL", 1.0)
    c = Coordinator(tmp_path / "rate", clock=lambda: 1000)
    calls = []

    def run(*args, **kw):
        calls.append(1)
        return reply(body='{"body":"rate limit 429 Retry-After: 99999"}')

    t = GhCliTransport(runner=run, coordinator=c)
    assert t.request("POST", "repos/example-org/r/issues/1/comments", {"body": "synthetic"})[1] is None
    assert t.request("POST", "repos/example-org/r/issues/2/comments", {"body": "synthetic"})[1]
    assert len(calls) == 1
    assert t.get("user")[1] is None


def test_invalid_state_and_backwards_clock_fail_closed(tmp_path):
    c = Coordinator(tmp_path / "state", clock=lambda: 1000)
    with c.lock("test"):
        c.check_rate()
        c.clock = lambda: 999
        with pytest.raises(Hold):
            c.check_rate()
        (c.root / "github-com-rate.json").write_text("{}")
        with pytest.raises(Hold):
            c.check_rate()


WORKER = r"""
import json,os,sys,time
from pathlib import Path
from githubbot_core import actions
from githubbot_core.coordination import Coordinator
from githubbot_core.transport import GhCliTransport
from types import SimpleNamespace
root, mode = Path(sys.argv[1]),sys.argv[2]
c=Coordinator(root)
if mode in {'request','barrier'}:
 def runner(*args,**kw):
  with (root/'trace').open('a') as f:f.write(json.dumps(['start',time.time()])+'\n')
  if mode=='barrier':
   deadline=time.monotonic()+20
   while not (root/'release').exists():
    assert time.monotonic()<deadline
    time.sleep(.01)
  else:time.sleep(.2)
  with (root/'trace').open('a') as f:f.write(json.dumps(['end',time.time()])+'\n')
  return SimpleNamespace(returncode=0,stdout='HTTP/2.0 200 OK\n\n{}',stderr='')
 assert GhCliTransport(runner=runner,coordinator=c).get('user')[1] is None
else:
 class T:
  coordinator=c
  def get(self,e):
   return ({'login':'example-bot'} if e=='user' else {'full_name':'example-org/repo'}),None
  def request(self,*args):
   with (root/'effects').open('a') as f:f.write('effect\n');f.flush();os.fsync(f.fileno())
   if mode=='crash':os._exit(23)
   time.sleep(.1)
   return {'ok':True},None
 cfg={'permissions':{'ceiling':['issue.comment'],'enabled':['issue.comment'],'write_owners':['example-org']},'multi_bot':{'merge_account':'example-bot'}}
 result=actions.apply([actions.Action('issue.comment',{'repo':'example-org/repo','number':1,'body':'synthetic'})],T(),cfg)
 print(json.dumps(result))
"""


def spawn(root, mode):
    return subprocess.Popen(
        [sys.executable, "-B", "-X", "utf8", "-c", WORKER, str(root), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )


def finish(child, expected=0):
    out, err = child.communicate(timeout=20)
    assert child.returncode == expected, err
    return out


def test_two_processes_have_no_overlapping_transport_requests(tmp_path):
    root = tmp_path / "process"
    a, b = spawn(root, "request"), spawn(root, "request")
    finish(a)
    finish(b)
    trace = [json.loads(x) for x in (root / "trace").read_text().splitlines()]
    assert [x[0] for x in trace] == ["start", "end", "start", "end"]
    assert trace[2][1] >= trace[1][1]


def test_two_processes_apply_identical_action_once(tmp_path):
    root = tmp_path / "process"
    a, b = spawn(root, "action"), spawn(root, "action")
    results = [json.loads(finish(x))["receipts"][0]["status"] for x in [a, b]]
    assert sorted(results) == ["applied", "skipped_duplicate"]
    assert (root / "effects").read_text().splitlines() == ["effect"]


def test_crash_after_effect_preserves_unknown_and_releases_os_lock(tmp_path):
    root = tmp_path / "crash"
    finish(spawn(root, "crash"), 23)
    result = json.loads(finish(spawn(root, "action")))
    assert result["receipts"][0]["status"] == "failed"
    assert "unknown write HOLD" in result["receipts"][0]["detail"]
    assert (root / "effects").read_text().splitlines() == ["effect"]
    journal = list(root.glob("action-*.json"))
    assert len(journal) == 1 and json.loads(journal[0].read_text())["status"] == "inflight"


@pytest.mark.parametrize("change", ["body", "head", "actor", "duplicate", "number", "malformed"])
def test_unknown_review_needs_unique_exact_readback(change):
    from test_pr_review_action import Transport, action, config, SHA
    from githubbot_core import actions

    t = Transport()
    t.write_error = "Timeout"
    a = action()
    assert actions.apply([a], t, config())["receipts"][0]["status"] == "failed"
    if change == "body":
        t.reviews[0]["body"] = "different"
    elif change == "head":
        t.pull["head"]["sha"] = "b" * 40
    elif change == "actor":
        t.reviews[0]["user"]["login"] = "other-actor"
    elif change == "number":
        t.pull["number"] = 99
    elif change == "malformed":
        t.reviews.append({"user": "not-an-object"})
    else:
        t.reviews.append(dict(t.reviews[0]))
    result = actions.apply([a], t, config())
    assert result["receipts"][0]["status"] == "failed"
    assert len(t.writes) == 1
    assert SHA != "b" * 40


def test_unknown_plan_authority_change_cannot_reuse_intent():
    from test_pr_review_action import Transport, action, config
    from githubbot_core import actions

    t = Transport()
    t.write_error = "Timeout"
    a = action()
    actions.apply([a], t, config())
    cfg = config()
    cfg["permissions"]["write_owners"] += ["another-org"]
    result = actions.apply([a], t, cfg)
    assert result["receipts"][0]["status"] == "failed"
    assert len(t.writes) == 1


def test_pre_send_hold_does_not_invent_unknown_effect(tmp_path, monkeypatch):
    from githubbot_core import actions

    monkeypatch.setattr(coordination, "MUTATION_INTERVAL", 1.0)
    now = [1000.0]
    c = Coordinator(tmp_path / "before", clock=lambda: now[0])
    calls = []

    def run(command, **kw):
        calls.append(command)
        if "--method" in command:
            return reply()
        return reply(
            body=json.dumps({"login": "example-bot"} if command[-1] == "user" else {"full_name": "example-org/repo"})
        )

    t = GhCliTransport(runner=run, coordinator=c)
    cfg = {
        "permissions": {"ceiling": ["issue.comment"], "enabled": ["issue.comment"], "write_owners": ["example-org"]},
        "multi_bot": {"merge_account": "example-bot"},
    }
    a = actions.Action("issue.comment", {"repo": "example-org/repo", "number": 1, "body": "synthetic"})
    with c.lock("github-com-transport"):
        c.check_rate(mutation=True)
    result = actions.apply([a], t, cfg)
    assert result["receipts"][0]["status"] == "failed" and result["writes"] == 0
    assert not list(c.root.glob("action-*.json"))
    assert not any("--method" in x for x in calls)
    now[0] += 1
    assert actions.apply([a], t, cfg)["receipts"][0]["status"] == "applied"


def test_pagination_holds_without_returning_partial_results(tmp_path):
    calls = []

    def run(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            return reply(
                headers='Link: <https://api.github.com/repos/example-org/repo/issues?page=2>; rel="next"\r\n',
                body='[{"number":1}]',
            )
        return reply(429, "Retry-After: 60\r\n")

    t = GhCliTransport(runner=run, coordinator=Coordinator(tmp_path / "pages", clock=lambda: 1000))
    data, error = t.get_paginated("repos/example-org/repo/issues")
    assert data == [] and error and len(calls) == 2


def test_lock_timeout_never_unlinks_guard(tmp_path):
    root = tmp_path / "busy"
    child = spawn(root, "request")
    deadline = __import__("time").monotonic() + 10
    while not (root / "trace").exists():
        assert __import__("time").monotonic() < deadline
        __import__("time").sleep(0.01)
    c = Coordinator(root, timeout=0.01)
    with pytest.raises(Hold):
        with c.lock("github-com-transport"):
            pytest.fail("must not acquire")
    finish(child)
    assert (root / "github-com-transport.lock").exists()


@pytest.mark.parametrize(
    "diagnostic", ["gh: HTTP 403: Forbidden", "gh: Forbidden (HTTP 403)", "gh: Too many requests (HTTP 429)"]
)
def test_cli_status_without_headers_still_persists_hold(tmp_path, diagnostic):
    calls = []

    def run(*args, **kw):
        calls.append(1)
        return SimpleNamespace(returncode=1, stdout="", stderr=diagnostic)

    t = GhCliTransport(runner=run, coordinator=Coordinator(tmp_path / "cli", clock=lambda: 1000))
    assert t.get("user")[1]
    assert t.get("user")[1] == "GitHub coordination HOLD; no request sent"
    assert len(calls) == 1


def test_successful_response_with_exhausted_primary_budget_preserves_result_then_holds(tmp_path):
    calls = []

    def run(*args, **kw):
        calls.append(1)
        return reply(200, "X-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 1300\r\n")

    t = GhCliTransport(runner=run, coordinator=Coordinator(tmp_path / "exhausted", clock=lambda: 1000))
    assert t.get("user") == ({"ok": True}, None)
    assert t.get("user")[1] and len(calls) == 1


def test_fixed_merge_readback_query_is_not_a_second_mutation(tmp_path, monkeypatch):
    from githubbot_core.typed_actions import MERGE_READBACK_QUERY

    monkeypatch.setattr(coordination, "MUTATION_INTERVAL", 1.0)
    c = Coordinator(tmp_path / "readback", clock=lambda: 1000)
    calls = []

    def run(*args, **kw):
        calls.append(1)
        return reply()

    t = GhCliTransport(runner=run, coordinator=c)
    assert t.request("PUT", "repos/example-org/repo/pulls/1/merge", {"sha": "a" * 40})[1] is None
    assert t.request("POST", "graphql", {"query": MERGE_READBACK_QUERY, "variables": {"id": "synthetic"}})[1] is None
    assert t.request("POST", "graphql", {"query": "mutation { synthetic }"})[1]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change,expected", [(None, "skipped_confirmed"), ("head", "failed"), ("number", "failed"), ("commit", "failed")]
)
def test_unknown_merge_reconciles_only_exact_published_state(change, expected):
    from test_typed_actions import Transport, base_gets, config, REPO, SHA
    from githubbot_core.actions import Action, apply

    class T(Transport):
        def request(self, *args, **kw):
            super().request(*args, **kw)
            return None, "Timeout"

    gets = base_gets()
    for pull in gets[f"repos/{REPO}/pulls/7"]:
        pull["number"] = 7
    t = T(gets)
    a = Action("pr.merge", {"repo": REPO, "number": 7, "sha": SHA})
    assert apply([a], t, config())["receipts"][0]["status"] == "failed"
    target = t.gets[f"repos/{REPO}/pulls/7"][-1]
    if change == "head":
        target["head"] = {"sha": "c" * 40}
    if change == "number":
        target["number"] = 99
    if change == "commit":
        target["merge_commit_sha"] = "invalid"
    assert apply([a], t, config())["receipts"][0]["status"] == expected
    assert len(t.writes) == 1


def _interleave_identity_read(monkeypatch, transport, operation):
    """Pause after page body extraction, outside the transport lock."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from githubbot_core import transport as transport_module

    original = transport_module.parse_concatenated_json
    page_ready, other_finished = threading.Event(), threading.Event()
    paused = []

    def parse(text):
        if not paused:
            paused.append(True)
            page_ready.set()
            assert other_finished.wait(5), "parallel identity read did not finish"
        return original(text)

    monkeypatch.setattr(transport_module, "parse_concatenated_json", parse)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operation)
        try:
            assert page_ready.wait(5), "paging did not reach the deterministic boundary"
            assert transport.get("user") == ({"login": "example-bot"}, None)
        finally:
            other_finished.set()
        return future.result(timeout=10)


def test_paginated_headers_are_bound_to_the_response_during_parallel_read(tmp_path, monkeypatch):
    calls = []

    def run(command, **kw):
        endpoint = command[-1]
        calls.append(endpoint)
        if endpoint == "user":
            return reply(body='{"login":"example-bot"}')
        if "page=2" in endpoint:
            return reply(body='[{"number":2}]')
        return reply(
            headers='Link: <https://api.github.com/repos/example-org/repo/issues?page=2>; rel="next"\r\n',
            body='[{"number":1}]',
        )

    t = GhCliTransport(runner=run, coordinator=Coordinator(tmp_path / "thread-pages"))
    result = _interleave_identity_read(monkeypatch, t, lambda: t.get_paginated("repos/example-org/repo/issues"))
    assert result == ([{"number": 1}, {"number": 2}], None)
    assert len(calls) == 3
    assert not hasattr(t, "response_headers")


def test_parallel_read_cannot_hide_existing_review_on_second_page(tmp_path, monkeypatch):
    from test_pr_review_action import action, config, SHA, REPO
    from githubbot_core.actions import apply

    a = action()
    calls = []

    def run(command, **kw):
        endpoint = command[-1]
        calls.append(command)
        if "--method" in command:
            pytest.fail("existing second-page review must prohibit a duplicate POST")
        if endpoint == "user":
            return reply(body='{"login":"example-bot"}')
        if endpoint == f"repos/{REPO}":
            return reply(body=json.dumps({"full_name": REPO, "default_branch": "main"}))
        if "reviews?page=2" in endpoint:
            return reply(
                body=json.dumps(
                    [
                        {
                            "id": 81,
                            "user": {"login": "example-bot"},
                            "commit_id": SHA,
                            "body": a.params["body"],
                            "state": "APPROVED",
                        }
                    ]
                )
            )
        if "/reviews?" in endpoint:
            return reply(
                headers=f'Link: <https://api.github.com/repos/{REPO}/pulls/7/reviews?page=2>; rel="next"\r\n', body="[]"
            )
        return reply(
            body=json.dumps(
                {
                    "number": 7,
                    "head": {"sha": SHA},
                    "state": "open",
                    "draft": False,
                    "user": {"login": "another-author"},
                }
            )
        )

    t = GhCliTransport(runner=run, coordinator=Coordinator(tmp_path / "thread-review"))
    result = _interleave_identity_read(monkeypatch, t, lambda: apply([a], t, config()))
    assert result["receipts"][0]["status"] == "failed"
    assert "already exists" in result["receipts"][0]["detail"]
    assert result["writes"] == 0
    assert not any("--method" in c for c in calls)


def test_response_header_snapshot_is_immutable(tmp_path):
    t = GhCliTransport(
        runner=lambda *a, **kw: reply(headers="X-RateLimit-Remaining: 10\r\n"),
        coordinator=Coordinator(tmp_path / "immutable"),
    )
    response = t._invoke_response(["user"])
    with pytest.raises(TypeError):
        response.headers["link"] = "replacement"
    assert response.body == '{"ok": true}' and response.error is None


def test_same_process_threads_cannot_overlap_via_canonical_lock_path(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    c = Coordinator(tmp_path / "same-thread", timeout=0.05)
    alias = Coordinator(c.root / ".." / c.root.name, timeout=0.05)

    def contender():
        with alias.lock("same"):
            return "incorrectly-acquired"

    with c.lock("same"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contender)
            with pytest.raises(Hold, match="coordination busy; no request sent"):
                future.result(timeout=5)
    with alias.lock("same"):
        pass


def test_threads_and_external_process_cannot_enter_same_transport_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time

    root = tmp_path / "mixed"
    child = spawn(root, "barrier")
    try:
        deadline = time.monotonic() + 10
        while not (root / "trace").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)

        def contend():
            with Coordinator(root, timeout=0.1).lock("github-com-transport"):
                return "incorrectly-acquired"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contend) for _ in range(2)]
            for future in futures:
                with pytest.raises(Hold, match="coordination busy; no request sent"):
                    future.result(timeout=5)
    finally:
        (root / "release").write_text("release", encoding="utf-8")
        finish(child)
    with Coordinator(root).lock("github-com-transport"):
        pass


def test_concurrent_apply_threads_never_duplicate_unknown_or_successful_effect(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from githubbot_core.actions import Action, apply

    entered, release = threading.Event(), threading.Event()
    writes = []
    root = tmp_path / "thread-actions"

    class T:
        def __init__(self):
            self.coordinator = Coordinator(root, timeout=0.1)

        def get(self, e):
            return ({"login": "example-bot"} if e == "user" else {"full_name": "example-org/repo"}), None

        def request(self, *args):
            writes.append(args)
            entered.set()
            assert release.wait(5)
            return {"ok": True}, None

    cfg = {
        "permissions": {"ceiling": ["issue.comment"], "enabled": ["issue.comment"], "write_owners": ["example-org"]},
        "multi_bot": {"merge_account": "example-bot"},
    }
    a = Action("issue.comment", {"repo": "example-org/repo", "number": 1, "body": "synthetic"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(apply, [a], T(), cfg)
        try:
            assert entered.wait(5)
            second = apply([a], T(), cfg)
            assert second["receipts"][0]["status"] == "failed"
            assert len(writes) == 1
        finally:
            release.set()
        assert first.result(timeout=5)["receipts"][0]["status"] == "applied"
    assert apply([a], T(), cfg)["receipts"][0]["status"] == "skipped_duplicate"
    assert len(writes) == 1
