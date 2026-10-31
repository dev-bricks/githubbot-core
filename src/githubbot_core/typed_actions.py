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
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from .credentials import CredentialUnavailable, EnvTokenProvider, resolve_token

SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
WORKFLOW = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml|[0-9]+")
MERGE_METHODS = ("merge", "squash", "rebase")
DEFAULT_PUSH_PREFIX = "bot/"

KINDS = (
    "pr.merge",
    "pr.ready",
    "pr.review",
    "pr.close",
    "actions.dispatch",
    "actions.rerun",
    "pr.create",
    "git.push_branch",
)
REVIEW_STATES = {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}

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
        "pr.review": policy["publish_reviews"],
        "pr.close": policy["close_integrated_prs"],
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
    need_sha = kind in ("pr.merge", "pr.ready", "pr.review", "pr.close", "actions.rerun", "git.push_branch")
    if need_sha and not SHA.fullmatch(str(params.get("sha", ""))):
        return "sha must be the exact reviewed 40-character lowercase commit"
    if kind in ("pr.merge", "pr.ready", "pr.review", "pr.close") and not str(params.get("number", "")).isdecimal():
        return "number must be a pull request number"
    if kind == "pr.close":
        if int(params["number"]) < 1:
            return "number must be a positive pull request number"
        if params.get("reason") != "integrated":
            return "pr.close only supports reason='integrated'"
        if not SHA.fullmatch(str(params.get("integration_sha", ""))):
            return "integration_sha must be the exact current default-branch commit"
        account = (config.get("multi_bot") or {}).get("merge_account")
        if not isinstance(account, str) or not account.strip():
            return "pr.close requires the configured multi_bot.merge_account"
        settings = config.get("typed_actions") or {}
        if not isinstance(settings, dict):
            return "typed_actions must be an object"
        scopes = settings.get("close_repositories")
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(repo, str) or not REPO.fullmatch(repo) for repo in scopes)
        ):
            return "pr.close requires explicit typed_actions.close_repositories"
        if params["repo"] not in scopes:
            return "repository is outside typed_actions.close_repositories"
    if kind == "pr.review":
        if int(params["number"]) < 1:
            return "number must be a positive pull request number"
        if not isinstance(params.get("event"), str) or params["event"] not in REVIEW_STATES:
            return "event must be COMMENT, APPROVE or REQUEST_CHANGES"
        body = params.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 65536:
            return "body must be nonempty review text of at most 65536 characters"
        if not str((config.get("multi_bot") or {}).get("merge_account") or "").strip():
            return "pr.review requires the configured multi_bot.merge_account"
    if kind == "git.push_branch":
        account = (config.get("multi_bot") or {}).get("merge_account")
        if not isinstance(account, str) or not account.strip():
            return "git.push_branch requires the configured multi_bot.merge_account"
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


MERGE_READBACK_QUERY = (
    "query($id: ID!) { node(id: $id) { ... on PullRequest { state headRefOid mergeCommit { oid } } } }"
)


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
    if error or after.get("merged") is not True:
        return None, "merge not confirmed by readback"
    commit_sha = after.get("merge_commit_sha")
    if not commit_sha:
        # REST 2026-03-10 removed merge_commit_sha from pull request objects.
        # Read the actual merged commit; never retry the merge or trust acceptance alone.
        if not pull.get("node_id"):
            return None, "merge not confirmed by readback"
        data, error = transport.request(
            "POST", "graphql", {"query": MERGE_READBACK_QUERY, "variables": {"id": pull["node_id"]}}
        )
        if error or not isinstance(data, dict) or data.get("errors"):
            return None, "merge not confirmed by readback"
        payload = data.get("data")
        node = payload.get("node") if isinstance(payload, dict) else None
        if not isinstance(node, dict) or node.get("state") != "MERGED" or node.get("headRefOid") != params["sha"]:
            return None, "merge not confirmed by readback"
        commit = node.get("mergeCommit")
        commit_sha = commit.get("oid") if isinstance(commit, dict) else None
    if not SHA.fullmatch(str(commit_sha or "")):
        return None, "merge not confirmed by readback"
    return {"readback": {"merged": True, "merge_commit_sha": commit_sha}}, None


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


def exec_review(transport, params: dict, config: dict):
    """Publish one submitted, exact-head review; never an issue comment or pending draft.

    Scope locks and author-instance independence remain the operator's responsibility.
    A failed/ambiguous write is not retried: a subsequent attempt must read reviews first.
    """
    account = str((config.get("multi_bot") or {}).get("merge_account") or "").strip()
    if not account:
        return None, "pr.review requires the configured multi_bot.merge_account"
    user, error = transport.get("user")
    if error or not isinstance(user, dict) or user.get("login") != account:
        return None, f"identity is not the configured review account {account!r}"
    repo, number, sha = params["repo"], int(params["number"]), params["sha"]
    endpoint = f"repos/{repo}/pulls/{number}"

    def current_pull():
        pull, error = _pull(transport, repo, number)
        if error:
            return {}, error
        head = pull.get("head")
        if pull.get("state") != "open" or not isinstance(head, dict) or head.get("sha") != sha:
            return {}, "pull is not open at the reviewed sha; nothing written"
        if params["event"] != "COMMENT":
            if pull.get("draft") is not False:
                return {}, "only COMMENT reviews are allowed on drafts or an unknown draft state"
            author = pull.get("user")
            if not isinstance(author, dict) or not isinstance(author.get("login"), str) or not author["login"].strip():
                return {}, "pull request author unreadable; nothing written"
            if author["login"].casefold() == account.casefold():
                return {}, "cannot approve or request changes on the account's own pull request"
        return pull, None

    _, error = current_pull()
    if error:
        return None, error
    reviews, error = transport.get_paginated(f"{endpoint}/reviews?per_page=100")
    if error or not isinstance(reviews, list):
        return None, f"existing reviews unreadable: {error or 'no list'}"
    for review in reviews:
        if (
            not isinstance(review, dict)
            or not isinstance(review.get("user"), dict)
            or not isinstance(review["user"].get("login"), str)
            or not review["user"]["login"].strip()
            or not isinstance(review.get("commit_id"), str)
            or not SHA.fullmatch(review["commit_id"])
        ):
            return None, "existing review record unreadable; nothing written"
        if review["user"]["login"].casefold() == account.casefold() and review["commit_id"] == sha:
            return None, f"review already exists for this account/head (id={review.get('id')}); nothing written"
    # Pagination may take time; bind the write to a fresh head again afterwards.
    _, error = current_pull()
    if error:
        return None, error
    body = {"commit_id": sha, "event": params["event"], "body": params["body"]}
    created, error = transport.request("POST", f"{endpoint}/reviews", body)
    if error:
        return None, f"review write unconfirmed: {error}; read existing reviews before any retry"
    review_id = created.get("id") if isinstance(created, dict) else None
    if type(review_id) is not int or review_id < 1:
        return None, "review write unconfirmed (no review id); read existing reviews before any retry"
    after, error = transport.get(f"{endpoint}/reviews/{review_id}")
    if (
        error
        or not isinstance(after, dict)
        or type(after.get("id")) is not int
        or after.get("id") != review_id
        or after.get("commit_id") != sha
        or after.get("state") != REVIEW_STATES[params["event"]]
        or after.get("body") != params["body"]
        or not isinstance(after.get("user"), dict)
        or after["user"].get("login") != account
    ):
        return None, f"review id={review_id} created but readback unconfirmed; do not blindly retry"
    latest, error = _pull(transport, repo, number)
    head = latest.get("head")
    if error or latest.get("state") != "open" or not isinstance(head, dict) or head.get("sha") != sha:
        return None, f"review id={review_id} confirmed but target changed; re-review current head, do not retry"
    return {"readback": {"review_id": review_id, "head": sha, "state": after["state"], "account": account}}, None


def exec_close(transport, params: dict, config: dict):
    """Close only heads proven contained in the pinned current default branch.

    GitHub's close endpoint has no atomic expected-head argument. Recheck before
    PATCH and detect races afterwards; ambiguous results must never be retried
    blindly. Locks and independent source review remain operator responsibilities.
    """
    permissions = config.get("permissions") or {}
    if "pr.close" not in (permissions.get("enabled") or []) or "pr.close" not in (permissions.get("ceiling") or []):
        return None, "pr.close is not enabled within the permission ceiling"
    denial = switch_denial("pr.close", config) or param_denial("pr.close", params, config)
    if denial:
        return None, denial
    account = config["multi_bot"]["merge_account"].strip()
    repo, number, sha = params["repo"], int(params["number"]), params["sha"]
    integrated = params["integration_sha"]

    def current_target():
        user, error = transport.get("user")
        if error or not isinstance(user, dict) or user.get("login") != account:
            return None, "identity is not the configured close account"
        branch, error = _default_branch(transport, repo)
        if error or not isinstance(branch, str):
            return None, error or "default branch unreadable"
        ref, error = transport.get(f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}")
        obj = ref.get("object") if isinstance(ref, dict) else None
        if error or not isinstance(obj, dict) or obj.get("type") != "commit" or obj.get("sha") != integrated:
            return None, "default branch moved or unreadable; nothing written"
        pull, error = _pull(transport, repo, number)
        head = pull.get("head")
        base = pull.get("base")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        if (
            error
            or pull.get("number") != number
            or pull.get("state") != "open"
            or pull.get("merged") is not False
            or not isinstance(head, dict)
            or head.get("sha") != sha
            or not isinstance(base_repo, dict)
            or base_repo.get("full_name") != repo
        ):
            return None, "pull is not open and unmerged at the exact head/repository; nothing written"
        return branch, None

    branch, error = current_target()
    if error:
        return None, error
    proof, error = transport.get(f"repos/{repo}/compare/{sha}...{integrated}")
    if (
        error
        or not isinstance(proof, dict)
        or proof.get("status") not in ("ahead", "identical")
        or (proof.get("status") == "identical") != (sha == integrated)
        or not isinstance(proof.get("base_commit"), dict)
        or proof["base_commit"].get("sha") != sha
        or not isinstance(proof.get("merge_base_commit"), dict)
        or proof["merge_base_commit"].get("sha") != sha
    ):
        return None, "head is not proven integrated in the pinned default branch"
    latest_branch, error = current_target()
    if error or latest_branch != branch:
        return None, error or "default branch name changed; nothing written"
    _, error = transport.request("PATCH", f"repos/{repo}/pulls/{number}", {"state": "closed"})
    if error:
        return None, "close write unconfirmed; independently read PR state before any retry: " + str(error)
    after, error = _pull(transport, repo, number)
    head = after.get("head")
    base = after.get("base")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if (
        error
        or after.get("number") != number
        or after.get("state") != "closed"
        or after.get("merged") is not False
        or not isinstance(head, dict)
        or head.get("sha") != sha
        or not isinstance(base_repo, dict)
        or base_repo.get("full_name") != repo
    ):
        return None, "close readback unconfirmed or head changed; do not blindly retry"
    return {
        "readback": {
            "closed": True,
            "merged": False,
            "head": sha,
            "integration_sha": integrated,
            "reason": "integrated",
            "account": account,
        }
    }, None


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


def _git_auth_config(repo: str) -> tuple[str, ...]:
    """Clear every Git credential/header source that could bypass the askpass token."""
    owner = repo.split("/", 1)[0]
    scopes = (
        "https://github.com",
        "https://github.com/",
        f"https://github.com/{owner}",
        f"https://github.com/{owner}/",
        f"https://github.com/{repo}",
        f"https://github.com/{repo}/",
        f"https://github.com/{repo}.git",
    )
    args = [
        "-c",
        "credential.helper=",
        "-c",
        "http.extraHeader=",
        "-c",
        "http.sslVerify=true",
        "-c",
        "http.followRedirects=false",
        "-c",
        "http.sslCert=",
        "-c",
        "http.sslKey=",
        "-c",
        "http.cookieFile=",
    ]
    for scope in scopes:
        for setting in (
            f"credential.{scope}.helper=",
            f"http.{scope}.extraheader=",
            f"http.{scope}.sslverify=true",
            f"http.{scope}.followredirects=false",
            f"http.{scope}.sslcert=",
            f"http.{scope}.sslkey=",
            f"http.{scope}.cookiefile=",
        ):
            args.extend(("-c", setting))
    return tuple(args)


def _git_base_environment() -> dict[str, str]:
    """Isolate Git path/auth overrides while preserving one config view for all checks."""
    env = os.environ.copy()
    for name in tuple(env):
        if (
            name
            in (
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_COMMON_DIR",
                "GIT_INDEX_FILE",
                "GIT_PREFIX",
                "GIT_ASKPASS",
                "SSH_ASKPASS",
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "GH_ENTERPRISE_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN",
                "GIT_CURL_VERBOSE",
                "GCM_INTERACTIVE",
                "GIT_SSL_NO_VERIFY",
                "GIT_SSL_CAINFO",
                "GIT_SSL_CAPATH",
                "GIT_SSL_CERT",
                "GIT_SSL_KEY",
            )
            or name.startswith("GIT_CONFIG")
            or name.startswith("GIT_TRACE")
        ):
            env.pop(name, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


@contextmanager
def _git_auth_environment(token: str, account: str):
    """Prepare a short-lived, secret-free AskPass helper and a process-only env."""
    with tempfile.TemporaryDirectory(prefix="githubbot-git-askpass-") as temp_dir:
        root = Path(temp_dir)
        helper = root / ("askpass.cmd" if os.name == "nt" else "askpass")
        script = root / "askpass.py"
        script.write_text(
            "import os, sys\n"
            "prompt = ' '.join(sys.argv[1:]).casefold()\n"
            "name = 'GITHUBBOT_GIT_USERNAME' if 'username' in prompt else 'GITHUBBOT_GIT_TOKEN'\n"
            "value = os.environ.get(name, '')\n"
            "if not value:\n"
            "    raise SystemExit(1)\n"
            "sys.stdout.write(value)\n",
            encoding="utf-8",
            newline="\n",
        )
        if os.name == "nt":
            helper.write_text(
                '@echo off\r\n"%GITHUBBOT_ASKPASS_PYTHON%" -I "%~dp0askpass.py" %*\r\nexit /b %ERRORLEVEL%\r\n',
                encoding="utf-8",
                newline="",
            )
        else:
            helper.write_text(
                '#!/bin/sh\nexec "$GITHUBBOT_ASKPASS_PYTHON" -I "$(dirname "$0")/askpass.py" "$@"\n',
                encoding="utf-8",
                newline="\n",
            )
            helper.chmod(0o700)

        env = _git_base_environment()
        env.update(
            {
                "GIT_ASKPASS": str(helper),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GITHUBBOT_ASKPASS_PYTHON": sys.executable,
                "GITHUBBOT_GIT_USERNAME": account,
                "GITHUBBOT_GIT_TOKEN": token,
            }
        )
        try:
            yield env
        finally:
            for name in ("GITHUBBOT_GIT_TOKEN", "GITHUBBOT_GIT_USERNAME"):
                env.pop(name, None)


def _run_git(
    path: str,
    *args: str,
    env: Optional[dict[str, str]] = None,
    repo: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    command = ["git", "-C", path]
    if env is not None:
        command.extend(_git_auth_config(repo or ""))
    command.extend(args)
    run_env = env.copy() if env is not None else _git_base_environment()
    try:
        result = _git(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=run_env,
        )
    except subprocess.TimeoutExpired:
        return "", "git process timed out"
    except OSError as exc:
        return "", f"git process could not start ({type(exc).__name__})"
    except Exception as exc:
        return "", f"git process failed ({type(exc).__name__})"
    if result.returncode:
        return "", f"git process exited with code {result.returncode}"
    return (result.stdout or "").strip(), None


def _same_repo(url: str, repo: str) -> bool:
    if not REPO.fullmatch(repo):
        return False
    try:
        parsed = urlsplit(url.strip())
        if (
            parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold() != "github.com"
            or parsed.netloc.casefold() != "github.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in url
            or "#" in url
            or "%" in parsed.path
        ):
            return False
    except ValueError:
        return False
    path = parsed.path.removesuffix(".git")
    return path == f"/{repo}"


def _exact_remote_ref(output: str, sha: str, ref: str) -> bool:
    lines = [line for line in output.splitlines() if line]
    if len(lines) != 1:
        return False
    fields = lines[0].split("\t")
    return len(fields) == 2 and fields == [sha, ref]


def exec_push_branch(transport, params: dict, config: dict):
    repo, branch, sha, path = params["repo"], params["branch"], params["sha"], params["path"]
    account = str((config.get("multi_bot") or {}).get("merge_account") or "").strip()
    if not account:
        return None, "git.push_branch requires the configured multi_bot.merge_account"
    try:
        token, _provider = resolve_token([EnvTokenProvider(names=("GH_TOKEN",))])
    except CredentialUnavailable:
        return None, "authorized bot credential unavailable"
    except Exception:
        return None, "authorized bot credential unavailable"
    get_for_token = getattr(transport, "get_for_token", None)
    if not callable(get_for_token):
        return None, "account-bound GitHub transport unavailable"
    try:
        user, error = get_for_token(token, "user")
    except Exception:
        return None, "configured bot identity unreadable"
    if error or not isinstance(user, dict) or user.get("login") != account:
        return None, "credential identity does not match the configured bot account"
    try:
        repository, error = get_for_token(token, f"repos/{repo}")
    except Exception:
        return None, "target repository unreadable for the configured bot account"
    if error or not isinstance(repository, dict) or not isinstance(repository.get("default_branch"), str):
        return None, "target repository unreadable for the configured bot account"
    default = repository["default_branch"]
    if branch == default:
        return None, "never pushes the default branch"
    fetch_urls, error = _run_git(path, "remote", "get-url", "--all", "origin")
    if error:
        return None, "origin of the checkout is not the target repository"
    push_urls, error = _run_git(path, "remote", "get-url", "--push", "--all", "origin")
    if error:
        return None, "origin of the checkout is not the target repository"
    if len(fetch_urls.splitlines()) != 1 or len(push_urls.splitlines()) != 1:
        return None, "origin of the checkout is not the target repository"
    if not _same_repo(fetch_urls, repo) or not _same_repo(push_urls, repo):
        return None, "origin of the checkout is not the target repository"
    resolved, error = _run_git(path, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if error or resolved != sha:
        return None, "commit not present in the checkout"
    checked_branch, error = _run_git(path, "check-ref-format", "--branch", branch)
    if error or checked_branch != branch:
        return None, "branch ref is invalid"
    ref = f"refs/heads/{branch}"
    try:
        with _git_auth_environment(token, account) as env:
            # No '+' refspec and no --force: a non-fast-forward is rejected by git itself.
            _, error = _run_git(path, "push", "origin", f"{sha}:{ref}", env=env, repo=repo)
            if error:
                return None, f"{error}; push outcome is unknown; read the target before retrying"
            remote, error = _run_git(path, "ls-remote", "--exit-code", "origin", ref, env=env, repo=repo)
            if error:
                return None, f"push readback could not be confirmed ({error}); read the target before retrying"
            if not _exact_remote_ref(remote, sha, ref):
                return None, "push readback is unconfirmed; read the target before retrying"
    except Exception:
        return None, "account-bound Git transport failed; read the target before retrying"
    return {"readback": {"branch": branch, "sha": sha}}, None


EXECUTORS: dict[str, Any] = {
    "pr.merge": exec_merge,
    "pr.ready": exec_ready,
    "pr.review": exec_review,
    "pr.close": exec_close,
    "actions.dispatch": exec_dispatch,
    "actions.rerun": exec_rerun,
    "pr.create": exec_create_pr,
    "git.push_branch": exec_push_branch,
}
