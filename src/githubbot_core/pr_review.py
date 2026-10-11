"""
githubbot-core -- Pull-Request-Zusammenfassung
==============================================
Fasst Pull Requests zusammen und markiert grosse Aenderungen zur manuellen
Pruefung. Projektspezifische Policy-Pruefungen kommen ausschliesslich ueber
Plugins (Hook ``pr_policy_checks``); der Kern bringt keine eigene Policy mit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass
class PullRequest:
    """Repraesentiert einen GitHub Pull Request."""

    number: int
    title: str
    body: str
    author: str
    labels: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    created_at: str = ""
    head_sha: str = ""
    threshold_lines: int = 200

    @property
    def total_lines(self) -> int:
        return self.additions + self.deletions

    @property
    def is_large(self) -> bool:
        return self.total_lines > self.threshold_lines

    @classmethod
    def from_gh_json(cls, data: dict) -> "PullRequest":
        """Erstellt PR aus `gh pr list --json` Output."""
        return cls(
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            author=data.get("author", {}).get("login", "") if data.get("author") else "",
            labels=[l["name"] for l in data.get("labels", [])],
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            changed_files=data.get("changedFiles", 0),
            created_at=data.get("createdAt", ""),
        )

    @classmethod
    def from_rest_json(cls, data: dict) -> "PullRequest":
        """Erstellt PR aus der REST-Antwort `GET /repos/{repo}/pulls/{n}`."""
        return cls(
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            author=(data.get("user") or {}).get("login", ""),
            labels=[l.get("name", "") for l in data.get("labels", []) if isinstance(l, dict)],
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            changed_files=data.get("changed_files", 0),
            created_at=data.get("created_at", ""),
            head_sha=(data.get("head") or {}).get("sha", ""),
        )


@dataclass
class PRReviewResult:
    """Ergebnis der PR-Pruefung."""

    pr: PullRequest
    summary: str = ""
    policy_check_results: list[tuple[str, bool, str]] = field(default_factory=list)
    escalate: bool = False
    escalate_reason: str = ""
    issues_found: list[str] = field(default_factory=list)


class PRReviewer:
    """Groessenpruefung + Zusammenfassung; Policy-Checks liefern Plugins."""

    def __init__(self, config: dict, repo: str = "", transport=None, policy_plugins: Iterable = ()):
        self.config = config
        self.repo = repo
        self.transport = transport
        self.policy_plugins = [p for p in policy_plugins if callable(getattr(p, "pr_policy_checks", None))]
        self.threshold_lines = int(config.get("pr_review", {}).get("summary_threshold_lines", 200))

    def fetch(self, number: int) -> tuple[PullRequest | None, str | None]:
        data, error = self.transport.get(f"repos/{self.repo}/pulls/{int(number)}")
        if error or not isinstance(data, dict):
            return None, error or "invalid pull request response"
        pr = PullRequest.from_rest_json(data)
        pr.threshold_lines = self.threshold_lines
        return pr, None

    def review(self, pr: PullRequest) -> PRReviewResult:
        pr.threshold_lines = self.threshold_lines
        result = PRReviewResult(pr=pr)
        if pr.is_large:
            result.escalate = True
            result.escalate_reason = f"Grosser PR: {pr.total_lines} Zeilen in {pr.changed_files} Dateien"
        result.summary = self._generate_summary(pr)

        if self.policy_plugins:
            files, diff = self._files_and_diff(pr.number)
            for plugin in self.policy_plugins:
                for name, passed, detail in plugin.pr_policy_checks(pr, files, diff):
                    check = (f"{plugin.name}:{name}", bool(passed), str(detail))
                    result.policy_check_results.append(check)
                    if not passed:
                        result.issues_found.append(f"{check[0]}: {check[2]}")
                        result.escalate = True
        return result

    def _files_and_diff(self, number: int) -> tuple[list[str], str]:
        if self.transport is None or not self.repo:
            return [], ""
        items, error = self.transport.get_paginated(f"repos/{self.repo}/pulls/{number}/files?per_page=100")
        files = [] if error else [item.get("filename", "") for item in items]
        diff, diff_error = self.transport.get_text(f"repos/{self.repo}/pulls/{number}", "application/vnd.github.diff")
        return files, "" if diff_error else diff[:80000]

    def _generate_summary(self, pr: PullRequest) -> str:
        lines = [
            f"**PR #{pr.number}: {pr.title}**",
            f"Autor: {pr.author} | +{pr.additions} -{pr.deletions} Zeilen / {pr.changed_files} Dateien",
        ]
        if pr.body:
            preview = pr.body[:200].replace("\n", " ")
            if len(pr.body) > 200:
                preview += "..."
            lines.append(f"Beschreibung: {preview}")
        if pr.is_large:
            lines.append(f"HINWEIS: Grosser PR (>{self.threshold_lines} Zeilen) — manuelle Review empfohlen")
        return "\n".join(lines)
