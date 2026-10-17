"""Narrow, typed write actions for pull requests, CI runs and bot branches.

``api.request`` can only be scoped by endpoint prefix, which is far too coarse for
merges (``repos/<owner>/`` would allow every write in every repository). These
actions each do exactly one thing, re-read the target before writing, refuse when
the reviewed head moved, and read the result back. They are in the default ceiling
but never in the default ``enabled`` list, and every kind is additionally gated by
its ``multi_bot`` switch, so a permission alone opens nothing.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Optional

SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
WORKFLOW = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml|[0-9]+")
MERGE_METHODS = ("merge", "squash", "rebase")
DEFAULT_PUSH_PREFIX = "bot/"

KINDS = ("pr.merge", "pr.ready", "actions.dispatch", "actions.rerun", "pr.create", "git.push_branch")

_git = subprocess.run  # replaced in tests


def _policy(config: dict) -> dict:
    from .multibot import policy_settings

    settings = config.get("multi_bot") or {}
    return {"merge_enabled": settings.get("merge_enabled") is True, **policy_settings(settings.get("policy"))}


def switch_denial(kind: str, config: dict) -> Optional[str]:
    """A typed action also needs its multi_bot switch; the permission alone is not enough."""
    if kind not in KINDS:
        return None
    try:
        policy = _policy(config)
    except ValueError as exc:
        return f"multi_bot.policy invalid: {exc}"
    allowed = {
        "pr.merge": policy["merge_enabled"],
        "pr.ready": policy["draft_auto_ready_hours"] is not None or policy["draft_ready_when_mature"],
        "actions.dispatch": policy["trigger_missing_ci"],
        "actions.rerun": policy["trigger_missing_ci"],
        "pr.create": policy["open_fix_prs"],
        "git.push_branch": policy["open_fix_prs"],
    }[kind]
    return None if allowed else f"{kind} is switched off in multi_bot (merge_enabled/policy)"


def push_prefix(config: dict) -> str:
    prefix = str((config.get("typed_actions") or {}).get("push_branch_prefix") or DEFAULT_PUSH_PREFIX)
    return prefix if prefix.endswith("/") else prefix + "/"


def param_denial(kind: str, params: dict, config: dict) -> Optional[str]:
    """Static parameter checks, shown already in preview."""
    if kind not in KINDS:
        return None
    if not REPO.fullmatch(str(params.get("repo", ""))):
        return "repo must be owner/name"
    need_sha = kind in ("pr.merge", "pr.ready", "actions.rerun", "git.push_branch")
    if need_sha and not SHA.fullmatch(str(params.get("sha", ""))):
        return "sha must be the exact reviewed 40-character lowercase commit"
    if kind in ("pr.merge", "pr.ready") and not str(params.get("number", "")).isdecimal():
        return "number must be a pull request number"
    if kind == "pr.merge" and params.get("method", "merge") not in MERGE_METHODS:
        return f"method must be one of {list(MERGE_METHODS)}"
    if kind == "actions.dispatch":
        if not WORKFLOW.fullmatch(str(params.get("workflow", ""))):
            return "workflow must be a workflow file name or id"
        if not BRANCH.fullmatch(str(params.get("ref", ""))) or ".." in str(params.get("ref")):
            return "ref must be a branch name"
    if kind == "actions.rerun" and not str(params.get("run_id", "")).isdecimal():
        return "run_id must be numeric"
    if kind in ("pr.create", "git.push_branch"):
        branch = str(params.get("branch", ""))
        if not BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".lock", "/")):
            return "branch name is invalid"
        if not branch.startswith(push_prefix(config)):
            return f"branch must start with the bot prefix {push_prefix(config)!r}"
    if kind == "pr.create" and (not str(params.get("title", "")).strip() or not str(params.get("body", "")).strip()):
        return "title and body are required"
    if kind == "git.push_branch" and not str(params.get("path", "")).strip():
        return "path of the local checkout is required"
    return None


def _default_branch(transport, repo: str) -> tuple[Optional[str], Optional[str]]:
    data, error = transport.get(f"repos/{repo}")
    if error or not isinstance(data, dict) or not data.get("default_branch"):
        return None, f"default branch unreadable: {error or 'no data'}"
    return data["default_branch"], None


def _pull(transport, repo: str, number) -> tuple[dict, Optional[str]]:
    data, error = transport.get(f"repos/{repo}/pulls/{int(number)}")
    if error or not isinstance(data, dict):
        return {}, f"pull request unreadable: {error or 'no data'}"
    return data, None


def _reviewed_pull(transport, params: dict, *, draft: bool) -> tuple[dict, Optional[str]]:
    """Fresh read: open, unstacked, exact reviewed head, expected draft state."""
    repo, sha = params["repo"], params["sha"]
    default, error = _default_branch(transport, repo)
    if error:
        return {}, error
    pull, error = _pull(transport, repo, params["number"])
    if error:
        return {}, error
    if pull.get("state") != "open":
        return {}, "pull request is not open"
    if (pull.get("head") or {}).get("sha") != sha:
        return {}, "head moved since review (sha mismatch); nothing written"
    if (pull.get("base") or {}).get("ref") != default:
        return {}, "stacked pull request (base is not the default branch)"
    if bool(pull.get("draft")) is not draft:
        return {}, "pull request is a draft" if pull.get("draft") else "pull request is not a draft"
    return pull, None


def exec_merge(transport, params: dict, config: dict):
    account = str((config.get("multi_bot") or {}).get("merge_account") or "")
    if account:
        user, error = transport.get("user")
        if error or (user or {}).get("login") != account:
            return None, f"identity is not the configured merge account {account!r}"
    pull, error = _reviewed_pull(transport, params, draft=False)
    if error:
        return None, error
    # "clean" means GitHub sees required checks and reviews satisfied. Anything else
    # (blocked, unstable, dirty, unknown) is a HOLD, so an admin token cannot bypass.
    if pull.get("mergeable_state") != "clean":
        return None, f"mergeable_state is {pull.get('mergeable_state')!r}, not 'clean'"
    repo, number = params["repo"], int(params["number"])
    body = {"sha": params["sha"], "merge_method": params.get("method", "merge")}
    _, error = transport.request("PUT", f"repos/{repo}/pulls/{number}/merge", body)
    if error:
        return None, error
    after, error = _pull(transport, repo, number)
    if error or not after.get("merged") or not after.get("merge_commit_sha"):
        return None, "merge not confirmed by readback"
    return {"readback": {"merged": True, "merge_commit_sha": after["merge_commit_sha"]}}, None


READY_MUTATION = (
    "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
)


def exec_ready(transport, params: dict, config: dict):
    pull, error = _reviewed_pull(transport, params, draft=True)
    if error:
        return None, error
    data, error = transport.request("POST", "graphql", {"query": READY_MUTATION, "variables": {"id": pull["node_id"]}})
    if error or (data or {}).get("errors"):
        return None, error or "GraphQL error"
    after, error = _pull(transport, params["repo"], params["number"])
    if error or after.get("draft") or (after.get("head") or {}).get("sha") != params["sha"]:
        return None, "ready state not confirmed by readback"
    return {"readback": {"draft": False, "head": params["sha"]}}, None


def exec_dispatch(transport, params: dict, config: dict):
    repo, workflow = params["repo"], params["workflow"]
    data, error = transport.get(f"repos/{repo}/actions/workflows/{workflow}")
    if error or (data or {}).get("state") != "active":
        return None, f"workflow not active or unreadable: {error or (data or {}).get('state')}"
    _, error = transport.request(
        "POST", f"repos/{repo}/actions/workflows/{workflow}/dispatches", {"ref": params["ref"]}
    )
    if error:
        return None, error
    # The run appears asynchronously; queued or still running is never a green result.
    return {"readback": {"dispatched": True, "note": "evidence only once completed and green at the same head"}}, None


def exec_rerun(transport, params: dict, config: dict):
    repo, run_id = params["repo"], int(params["run_id"])
    run, error = transport.get(f"repos/{repo}/actions/runs/{run_id}")
    if error or not isinstance(run, dict):
        return None, f"run unreadable: {error or 'no data'}"
    if run.get("head_sha") != params["sha"]:
        return None, "run belongs to another head"
    if (run.get("head_repository") or {}).get("full_name") != repo:
        return None, "run comes from a fork; fork runs are never re-run or approved by the bot"
    if run.get("status") != "completed":
        return None, f"run is {run.get('status')!r}, not completed"
    _, error = transport.request("POST", f"repos/{repo}/actions/runs/{run_id}/rerun", None)
    return (None, error) if error else ({"readback": {"rerun": run_id}}, None)


def exec_create_pr(transport, params: dict, config: dict):
    repo, branch = params["repo"], params["branch"]
    default, error = _default_branch(transport, repo)
    if error:
        return None, error
    if branch == default:
        return None, "head must not be the default branch"
    body = {
        "title": str(params["title"]),
        "body": str(params["body"]),
        "head": branch,
        "base": default,
        "draft": bool(params.get("draft", False)),
    }
    data, error = transport.request("POST", f"repos/{repo}/pulls", body)
    if error or not isinstance(data, dict) or not data.get("number"):
        return None, error or "pull request not confirmed"
    return {"readback": {"number": data["number"], "url": data.get("html_url")}}, None


def _run_git(path: str, *args: str) -> tuple[str, Optional[str]]:
    result = _git(["git", "-C", path, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        return "", " ".join((result.stderr or result.stdout or "").split())[:500] or f"git exit {result.returncode}"
    return result.stdout.strip(), None


def _same_repo(url: str, repo: str) -> bool:
    url = url.strip().removesuffix(".git").removesuffix("/").casefold()
    return url.endswith("github.com/" + repo.casefold()) or url.endswith("github.com:" + repo.casefold())


def exec_push_branch(transport, params: dict, config: dict):
    repo, branch, sha, path = params["repo"], params["branch"], params["sha"], params["path"]
    default, error = _default_branch(transport, repo)
    if error:
        return None, error
    if branch == default:
        return None, "never pushes the default branch"
    url, error = _run_git(path, "remote", "get-url", "origin")
    if error or not _same_repo(url, repo):
        return None, "origin of the checkout is not the target repository"
    resolved, error = _run_git(path, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if error or resolved != sha:
        return None, "commit not present in the checkout"
    # No '+' refspec and no --force: a non-fast-forward is rejected by git itself.
    _, error = _run_git(path, "push", "origin", f"{sha}:refs/heads/{branch}")
    if error:
        return None, error
    remote, error = _run_git(path, "ls-remote", "origin", f"refs/heads/{branch}")
    if error or not remote.startswith(sha):
        return None, "push not confirmed by ls-remote"
    return {"readback": {"branch": branch, "sha": sha}}, None


EXECUTORS: dict[str, Any] = {
    "pr.merge": exec_merge,
    "pr.ready": exec_ready,
    "actions.dispatch": exec_dispatch,
    "actions.rerun": exec_rerun,
    "pr.create": exec_create_pr,
    "git.push_branch": exec_push_branch,
}
