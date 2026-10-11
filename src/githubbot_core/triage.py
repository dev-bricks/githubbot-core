"""
githubbot-core -- Issue-Triage
==============================
Klassifiziert GitHub-Issues nach konfigurierbaren Kriterien und plant
Label- und Kommentaraktionen.

Ablauf:
  1. Offene Issues ueber den Transport laden (REST, Pull Requests ausgefiltert)
  2. Jedes Issue nach ``triage_criteria`` klassifizieren
  3. Labels und Auto-Kommentare als Aktionen planen (``actions.Action``)
  4. Vorschau ist der Standard; geschrieben wird nur ueber ``actions.apply``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .actions import Action
from .comments import AutoCommentManager

logger = logging.getLogger(__name__)


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class GitHubIssue:
    """Repraesentiert ein GitHub Issue."""
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    state: str = "open"
    author: str = ""
    created_at: str = ""

    @classmethod
    def from_gh_json(cls, data: dict) -> "GitHubIssue":
        """Erstellt Issue aus `gh issue list --json` Output."""
        return cls(
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            labels=[l["name"] for l in data.get("labels", [])],
            state=data.get("state", "open"),
            author=data.get("author", {}).get("login", "") if data.get("author") else "",
            created_at=data.get("createdAt", ""),
        )

    @classmethod
    def from_rest_json(cls, data: dict) -> "GitHubIssue":
        """Erstellt Issue aus der REST-Antwort `GET /repos/{repo}/issues`."""
        return cls(
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            labels=[l.get("name", "") for l in data.get("labels", []) if isinstance(l, dict)],
            state=data.get("state", "open"),
            author=(data.get("user") or {}).get("login", ""),
            created_at=data.get("created_at", ""),
        )


@dataclass
class TriageResult:
    """Ergebnis der Triage fuer ein Issue."""
    issue: GitHubIssue
    labels_to_add: list[str] = field(default_factory=list)
    comment: Optional[str] = None
    comment_type: Optional[str] = None
    escalate: bool = False
    escalate_reason: str = ""


# ============================================================
# CLASSIFIER
# ============================================================

class IssueClassifier:
    """Klassifiziert Issues nach den Triage-Kriterien aus der Config."""

    PRIORITY_ORDER = [
        "security_public", "core_change", "bug_with_steps",
        "bug_without_details", "feature_request", "question",
        "documentation", "unclear",
    ]

    def __init__(self, config: dict):
        self.criteria = config.get("triage_criteria", {})
        self.comments = config.get("auto_comments", {})

    def classify(self, issue: GitHubIssue) -> TriageResult:
        """
        Klassifiziert ein Issue und gibt ein TriageResult zurueck.

        Reihenfolge (Prioritaet absteigend):
        1. Security (sofortige Aktion noetig)
        2. Core-Aenderung (eskalieren)
        3. Bug mit / ohne Steps
        4. Feature Request
        5. Frage
        6. Unklar
        """
        result = TriageResult(issue=issue)

        text = f"{issue.title} {issue.body}".lower()
        matched_criteria = []

        for crit_name, crit in self.criteria.items():
            if self._matches(text, crit):
                matched_criteria.append(crit_name)

        # Sortiere nach Prioritaet
        matched_criteria.sort(
            key=lambda c: self.PRIORITY_ORDER.index(c) if c in self.PRIORITY_ORDER else len(self.PRIORITY_ORDER)
        )

        if not matched_criteria:
            crit = self.criteria.get("unclear", {})
            result.labels_to_add = [l for l in crit.get("labels", ["needs-info"]) if l not in issue.labels]
            result.escalate = crit.get("escalate", False)
            result.escalate_reason = "Keine Kategorie erkannt"
            return result

        primary = matched_criteria[0]
        crit = self.criteria[primary]

        # Labels aus Primary + sekundaere Labels zusammensammeln
        all_labels = list(crit.get("labels", []))
        for secondary in matched_criteria[1:]:
            for label in self.criteria[secondary].get("labels", []):
                if label not in all_labels:
                    all_labels.append(label)

        result.labels_to_add = [l for l in all_labels if l not in issue.labels]

        if crit.get("escalate"):
            result.escalate = True
            result.escalate_reason = crit["description"]

        auto_comment_key = crit.get("auto_comment")
        if auto_comment_key and auto_comment_key in self.comments:
            result.comment = self.comments[auto_comment_key]["template"]
            result.comment_type = auto_comment_key

        return result

    def _matches(self, text: str, criterion: dict) -> bool:
        """Prueft ob Text die Detektions-Keywords des Kriteriums enthaelt."""
        detection = criterion.get("detection", {})
        keywords = detection.get("keywords", [])
        if not keywords:
            return False
        return any(kw.lower() in text for kw in keywords)


# ============================================================
# TRIAGE
# ============================================================

class IssueTriage:
    """Laedt Issues, klassifiziert sie und plant Aktionen. Schreibt selbst nie."""

    def __init__(self, config: dict, repo: str, transport):
        if not repo or "/" not in repo:
            raise ValueError("repo must be given as owner/name")
        self.config = config
        self.repo = repo
        self.transport = transport
        self.classifier = IssueClassifier(config)
        self.comments = AutoCommentManager(config)

    def fetch_issues(self, limit: int = 50) -> tuple[list[GitHubIssue], Optional[str]]:
        data, error = self.transport.get_paginated(f"repos/{self.repo}/issues?state=open&per_page=100")
        if error:
            return [], error
        issues = [GitHubIssue.from_rest_json(item) for item in data if "pull_request" not in item]
        return issues[:limit], None

    def classify_all(self, issues: list[GitHubIssue]) -> list[TriageResult]:
        return [self.classifier.classify(issue) for issue in issues]

    def plan(self, results: list[TriageResult]) -> list[Action]:
        """Uebersetzt Triage-Ergebnisse in Aktionen. Kommentare nur, wenn der Marker fehlt."""
        actions: list[Action] = []
        for result in results:
            number = result.issue.number
            if result.labels_to_add:
                actions.append(Action("issue.add_labels",
                                      {"repo": self.repo, "number": number, "labels": result.labels_to_add},
                                      reason=f"triage #{number}"))
            if result.comment and result.comment_type:
                existing, error = self.transport.get_paginated(
                    f"repos/{self.repo}/issues/{number}/comments?per_page=100")
                if error:
                    logger.warning("#%d: Kommentare nicht lesbar, Kommentar wird nicht geplant: %s", number, error)
                    continue
                if self.comments.check_already_commented(number, result.comment_type, existing):
                    continue
                body = self.comments.add_marker(result.comment, result.comment_type)
                actions.append(Action("issue.comment", {"repo": self.repo, "number": number, "body": body},
                                      reason=f"auto comment {result.comment_type} #{number}"))
        return actions

    @staticmethod
    def get_escalations(results: list[TriageResult]) -> list[TriageResult]:
        return [r for r in results if r.escalate]

    def generate_report(self, results: list[TriageResult]) -> str:
        today = date.today().strftime("%Y-%m-%d")
        lines = [
            f"## Issue-Triage -- {today}",
            f"**Repo:** {self.repo} | **Issues verarbeitet:** {len(results)}",
            "",
        ]
        auto_done = [r for r in results if not r.escalate]
        escalations = [r for r in results if r.escalate]
        if auto_done:
            lines.append("### Automatisch einordenbar:")
            for r in auto_done:
                labels = ", ".join(r.labels_to_add) if r.labels_to_add else "(keine neuen Labels)"
                lines.append(f"- #{r.issue.number}: **{r.issue.title[:60]}** -> {labels}")
            lines.append("")
        if escalations:
            lines.append("### Eskalation (Handlungsbedarf):")
            for r in escalations:
                lines.append(f"- #{r.issue.number}: **{r.issue.title[:60]}**")
                lines.append(f"  Grund: {r.escalate_reason}")
            lines.append("")
        if not results:
            lines.append("*Keine neuen Issues zu verarbeiten.*")
        return "\n".join(lines)
