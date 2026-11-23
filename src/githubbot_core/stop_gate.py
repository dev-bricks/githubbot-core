"""Stop-signal gate shared by every approve and merge path.

A reviewing instance may stop a pull request in plain text ("STOPP", "NICHT MERGEN",
"do not merge", "HOLD", ...) in a PR comment, an inline review comment or a review body,
or with a CHANGES_REQUESTED review. Whatever account posted it, the stop holds
``pr.review`` APPROVE, ``pr.ready`` and ``pr.merge`` until a *later* event lifts it:

* a release line by the same account or by one of ``multi_bot.policy.stop_release_accounts``.
  The line must consist of the formula alone (``STOPP aufgehoben``, ``STOP lifted``,
  ``Merge freigegeben``, ``cleared to merge``; markdown emphasis and a final ``.``/``!``
  are tolerated). Questions, negations or extra words on that line never count, nor do
  blockquote lines (``> ...``, as a quote-reply produces) or lines inside code fences; or
* a later APPROVED review by the same account on a head that is provably newer than
  every head the stop named (GitHub compare status ``ahead``). A stop that names no
  head can only be lifted by the release line. The configured merge account never
  lifts a stop by approving: it is the account that writes.

Dismissed reviews are ignored (dismissal is GitHub's own explicit lift). Unreadable
comments, reviews or timestamps fail closed. If auto-merge is already armed on a held
pull request, the hold reason says so, because GitHub would merge without this gate.
Anchors: lock-master#8 (2026-09-22) and promptboard#8 (2026-11-22), both merged
minutes after a STOPP comment.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

_NEGATED = re.compile(r"\b(?:kein(?:e[nr]?)?|no|ohne|without)\s+(?:stopp?|hold)\b", re.IGNORECASE)
_STOP = re.compile(r"\bstopp\b|nicht\s+mergen|do[\s-]+not[\s-]+merge|don'?t\s+merge|\bREQUEST_CHANGES\b", re.IGNORECASE)
_STOP_UPPER = re.compile(
    r"\b(?:STOP|HOLD)\b"
)  # English only in capitals, so prose like "stop the job" / "hold on" passes
_RELEASE_LINE = re.compile(
    r"(?:stopp?\s+(?:ist\s+)?(?:aufgehoben|zurückgenommen)|stop\s+(?:lifted|released|withdrawn)"
    r"|merge\s+freigegeben|cleared\s+to\s+merge)[.!]?",
    re.IGNORECASE,
)
_SHORT_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


def _release_lines(text: str) -> list[tuple[str, bool]]:
    """(line, counts as release). Quoted lines, lines in code fences and indented lines never
    release: a quote-reply repeats someone else's words, a code block shows text instead of
    saying it. Any line indented by a tab or four spaces counts as an indented code block."""
    result, fence = [], None
    for raw in text.splitlines():
        stripped = raw.strip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            result.append((raw, False))
            continue
        if marker:
            fence = marker[1]
            result.append((raw, False))
            continue
        if raw.startswith(("\t", "    ")):  # ponytail: also hides list-nested lines; strict on purpose
            result.append((raw, False))
            continue
        line = re.sub(r"\s+", " ", stripped.strip("*_#").strip())
        # ">" and backticks are deliberately not stripped: a quote line never fullmatches.
        result.append((raw, bool(_RELEASE_LINE.fullmatch(line))))
    return result


def _is_release(text: str) -> bool:
    return any(release for _, release in _release_lines(text))


def write_denial(kind: str, params: dict, config: dict) -> Optional[str]:
    """Under an owner account the bot never posts a STOPP release line.

    A release is the act of the stopping instance or the owner. In the owner fallback
    (``multi_bot.owner_fallback``) or under a ``stop_release_accounts`` account the bot
    writes as that account, so its comment with the formula would lift stops itself.
    """
    from .multibot import policy_settings

    settings = config.get("multi_bot") or {}
    try:
        owners = {a.casefold() for a in policy_settings(settings.get("policy"))["stop_release_accounts"]}
    except ValueError:
        owners = set()  # an invalid policy already denies through switch_denial/hold_reason
    account = str(settings.get("merge_account") or "").strip().casefold()
    if settings.get("owner_fallback") is not True and account not in owners:
        return None
    body = params.get("body")
    if kind == "api.request":
        if not str(params.get("endpoint", "")).rstrip("/").endswith(("/comments", "/reviews")):
            return None
        body = body.get("body") if isinstance(body, dict) else body
    elif kind not in ("issue.comment", "pr.review"):
        return None
    if isinstance(body, str) and _is_release(body):
        return f"{kind} body contains a STOPP release line; under an owner account the bot never lifts a stop"
    return None


def _is_stop(text: str) -> bool:
    rest = "\n".join(line for line, release in _release_lines(text) if not release)
    rest = _NEGATED.sub(" ", rest)
    return bool(_STOP.search(rest) or _STOP_UPPER.search(rest))


def _login(item) -> Optional[str]:
    user = item.get("user") if isinstance(item, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return login.casefold() if isinstance(login, str) and login.strip() else None


def evaluate(
    comments: list,
    reviews: list,
    release_accounts: Iterable[str] = (),
    *,
    merge_account: str = "",
    is_newer: Callable[[str, str], bool] = lambda old, new: False,
) -> Optional[str]:
    """Return why approve/merge must hold, or ``None``. No I/O except ``is_newer``.

    ``comments`` holds issue comments and inline review comments (the latter carry
    ``original_commit_id``). ``is_newer(old, new)`` must be True only when ``new`` is a
    proven descendant of ``old``; the default proves nothing.
    """
    owners = {account.casefold() for account in release_accounts}
    writer = merge_account.casefold().strip()
    events = []
    if any(not isinstance(item, dict) for item in [*comments, *reviews]):
        return "pull request comment or review unreadable; refusing approve/merge"
    for item in comments:
        login, at, body = _login(item), (item or {}).get("created_at"), (item or {}).get("body")
        if not login or not isinstance(at, str) or not isinstance(body, str):
            return "pull request comment unreadable; refusing approve/merge"
        commit = item.get("original_commit_id") or item.get("commit_id")
        events.append((at, "comment", login, body, commit if isinstance(commit, str) else None))
    for item in reviews:
        login, state = _login(item), (item or {}).get("state")
        if state in ("PENDING", "DISMISSED"):
            continue
        at, body, commit = item.get("submitted_at"), item.get("body") or "", item.get("commit_id")
        if not login or not isinstance(at, str) or not isinstance(body, str) or not isinstance(commit, str):
            return "pull request review unreadable; refusing approve/merge"
        events.append((at, state, login, body, commit))
    events.sort(key=lambda event: event[0])

    stops: dict[str, tuple] = {}  # login -> (at, named heads)
    for at, kind, login, body, commit in events:
        stop_text = _is_stop(body)
        if kind == "APPROVED" and not stop_text:
            named = stops.get(login, (None, set()))[1]
            if login != writer and named and all(is_newer(head, commit) for head in named):
                del stops[login]
            continue
        if kind == "CHANGES_REQUESTED" or stop_text:
            named = set(_SHORT_SHA.findall(body))
            if commit:
                named.add(commit)
            stops[login] = (at, named)
        elif _is_release(body):
            if login in owners:
                stops.clear()
            stops.pop(login, None)
    if not stops:
        return None
    login, (at, _) = min(stops.items(), key=lambda item: item[1][0])
    return f"stop signal by {login!r} at {at} is not lifted; approve/merge HOLD"


def hold_reason(transport, repo: str, number: int, config: dict, pull: Optional[dict] = None) -> Optional[str]:
    """Read all comments and reviews fresh and evaluate them; read failures are a HOLD."""
    from .multibot import policy_settings

    settings = config.get("multi_bot") or {}
    try:
        release_accounts = policy_settings(settings.get("policy"))["stop_release_accounts"]
    except ValueError as exc:
        return f"multi_bot.policy invalid: {exc}"
    lists = {}
    for name, endpoint in (
        ("comments", f"repos/{repo}/issues/{int(number)}/comments?per_page=100"),
        ("inline review comments", f"repos/{repo}/pulls/{int(number)}/comments?per_page=100"),
        ("reviews", f"repos/{repo}/pulls/{int(number)}/reviews?per_page=100"),
    ):
        data, error = transport.get_paginated(endpoint)
        if error or not isinstance(data, list):
            return f"pull request {name} unreadable ({error or 'no list'}); approve/merge HOLD"
        lists[name] = data

    def is_newer(old: str, new: str) -> bool:
        if not _SHORT_SHA.fullmatch(old) or not _SHORT_SHA.fullmatch(new):
            return False
        data, error = transport.get(f"repos/{repo}/compare/{old}...{new}")
        return not error and isinstance(data, dict) and data.get("status") == "ahead"

    reason = evaluate(
        lists["comments"] + lists["inline review comments"],
        lists["reviews"],
        release_accounts,
        merge_account=str(settings.get("merge_account") or ""),
        is_newer=is_newer,
    )
    if reason and isinstance(pull, dict) and pull.get("auto_merge") is not None:
        enabler = ((pull.get("auto_merge") or {}).get("enabled_by") or {}).get("login") or "unknown"
        reason += f"; auto-merge is armed (enabled by {enabler!r}) and GitHub may merge without this gate: disable it"
    return reason
