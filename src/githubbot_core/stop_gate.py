"""Stop-signal gate shared by every approve and merge path.

A reviewing instance may stop a pull request in plain text ("STOPP", "NICHT MERGEN",
"do not merge", ...) or with a CHANGES_REQUESTED review. Whatever account posted it,
the stop holds ``pr.review`` APPROVE and ``pr.merge`` until a *later* event lifts it:

* an explicit release comment ("STOPP aufgehoben", "freigegeben", "stop lifted", ...)
  by the same account or by one of ``multi_bot.policy.stop_release_accounts``, or
* a later APPROVED review by the same account on a different head than the one the
  stop named (reviews carry their commit; a stop comment names it as a short sha).

Dismissed reviews are ignored (dismissal is GitHub's own explicit lift). Unreadable
comments, reviews or timestamps fail closed. Anchors: lock-master#8 (2026-09-22) and
promptboard#8 (2026-11-22), both merged minutes after a STOPP comment.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

_NEGATED = re.compile(r"\b(?:kein(?:e[nr]?)?|no|ohne|without)\s+(?:stopp?|hold)\b", re.IGNORECASE)
_STOP = re.compile(r"\bstopp\b|nicht\s+mergen|do[\s-]+not[\s-]+merge|don'?t\s+merge|\bREQUEST_CHANGES\b", re.IGNORECASE)
_STOP_UPPER = re.compile(r"\bSTOP\b")  # English only in capitals, so prose like "stop the job" passes
_RELEASE = re.compile(
    r"\bstopp?\s+(?:ist\s+)?(?:aufgehoben|zurückgenommen|lifted|released|withdrawn)\b"
    r"|(?<!nicht\s)(?<!not\s)(?<!kein\s)\bfreigegeben\b|\bcleared\s+to\s+merge\b",
    re.IGNORECASE,
)
_SHORT_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


def _is_stop(text: str) -> bool:
    text = _NEGATED.sub(" ", _RELEASE.sub(" ", text))
    return bool(_STOP.search(text) or _STOP_UPPER.search(text))


def _login(item) -> Optional[str]:
    user = item.get("user") if isinstance(item, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return login.casefold() if isinstance(login, str) and login.strip() else None


def evaluate(comments: list, reviews: list, release_accounts: Iterable[str] = ()) -> Optional[str]:
    """Return why approve/merge must hold, or ``None``. Pure: no I/O."""
    owners = {account.casefold() for account in release_accounts}
    events = []
    for item in comments:
        login, at, body = _login(item), (item or {}).get("created_at"), (item or {}).get("body")
        if not login or not isinstance(at, str) or not isinstance(body, str):
            return "pull request comment unreadable; refusing approve/merge"
        events.append((at, "comment", login, body, None))
    for item in reviews:
        login, state = _login(item), (item or {}).get("state")
        if state in ("PENDING", "DISMISSED"):
            continue
        at, body, commit = item.get("submitted_at"), item.get("body") or "", item.get("commit_id")
        if not login or not isinstance(at, str) or not isinstance(body, str) or not isinstance(commit, str):
            return "pull request review unreadable; refusing approve/merge"
        events.append((at, state, login, body, commit))
    events.sort(key=lambda event: event[0])

    stops: dict[str, tuple] = {}  # login -> (at, named head prefixes)
    for at, kind, login, body, commit in events:
        if kind == "APPROVED":
            stop = stops.get(login)
            if stop and not _is_stop(body) and not any(commit.startswith(p) for p in stop[1]):
                del stops[login]
            if not _is_stop(body):
                continue
        if kind == "CHANGES_REQUESTED" or _is_stop(body):
            stops[login] = (at, {commit} if commit else set(_SHORT_SHA.findall(body)))
        elif _RELEASE.search(body):
            if login in owners:
                stops.clear()
            stops.pop(login, None)
    if not stops:
        return None
    login, (at, _) = min(stops.items(), key=lambda item: item[1][0])
    return f"stop signal by {login!r} at {at} is not lifted; approve/merge HOLD"


def hold_reason(transport, repo: str, number: int, config: dict) -> Optional[str]:
    """Read all comments and reviews fresh and evaluate them; read failures are a HOLD."""
    from .multibot import policy_settings

    try:
        release_accounts = policy_settings((config.get("multi_bot") or {}).get("policy"))["stop_release_accounts"]
    except ValueError as exc:
        return f"multi_bot.policy invalid: {exc}"
    comments, error = transport.get_paginated(f"repos/{repo}/issues/{int(number)}/comments?per_page=100")
    if error or not isinstance(comments, list):
        return f"pull request comments unreadable ({error or 'no list'}); approve/merge HOLD"
    reviews, error = transport.get_paginated(f"repos/{repo}/pulls/{int(number)}/reviews?per_page=100")
    if error or not isinstance(reviews, list):
        return f"pull request reviews unreadable ({error or 'no list'}); approve/merge HOLD"
    return evaluate(comments, reviews, release_accounts)
