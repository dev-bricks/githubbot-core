"""
githubbot-core -- Repo-Health
========================
Sammelt repo-uebergreifende Gesundheitsdaten:

- offene Dependabot-Warnungen
- fehlgeschlagene GitHub-Actions-Laeufe im Zeitfenster
- offene Issues und Pull Requests
- offene Code-Scanning- und Secret-Scanning-Alerts
- optionale Copilot-Autofix-Signale zu Code-Scanning-Alerts
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote

from .transport import GhCliTransport

logger = logging.getLogger(__name__)


def _parse_timestamp(value: str) -> Optional[datetime]:
    """Parst GitHub-UTC-Timestamps."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class DependabotAlert:
    """Zusammenfassung einer offenen Dependabot-Warnung."""

    number: int
    package_name: str = ""
    ecosystem: str = ""
    severity: str = "unknown"
    summary: str = ""
    manifest_path: str = ""
    created_at: str = ""
    html_url: str = ""


@dataclass
class WorkflowFailure:
    """Zusammenfassung eines fehlgeschlagenen Actions-Laufs."""

    run_id: int
    workflow_name: str = ""
    display_title: str = ""
    branch: str = ""
    event: str = ""
    actor: str = ""
    status: str = ""
    conclusion: str = ""
    created_at: str = ""
    updated_at: str = ""
    html_url: str = ""


@dataclass
class OpenIssue:
    """Zusammenfassung eines offenen Issues."""

    number: int
    title: str = ""
    author: str = ""
    labels: list[str] = field(default_factory=list)
    updated_at: str = ""
    html_url: str = ""


@dataclass
class OpenPullRequest:
    """Zusammenfassung eines offenen Pull Requests."""

    number: int
    title: str = ""
    author: str = ""
    labels: list[str] = field(default_factory=list)
    updated_at: str = ""
    html_url: str = ""


@dataclass
class CodeScanningAlert:
    """Zusammenfassung eines offenen Code-Scanning-Alerts."""

    number: int
    rule_id: str = ""
    rule_name: str = ""
    severity: str = ""
    tool_name: str = ""
    path: str = ""
    start_line: int = 0
    created_at: str = ""
    html_url: str = ""
    copilot_autofix_status: str = ""
    copilot_autofix_url: str = ""

    def has_copilot_autofix(self) -> bool:
        return bool(self.copilot_autofix_status or self.copilot_autofix_url)


@dataclass
class SecretScanningAlert:
    """Zusammenfassung eines offenen Secret-Scanning-Alerts."""

    number: int
    secret_type: str = ""
    created_at: str = ""
    html_url: str = ""


@dataclass
class RepoHealthSummary:
    """Health-Zusammenfassung fuer ein Repo."""

    repo: str
    dependabot_alerts: list[DependabotAlert] = field(default_factory=list)
    failed_runs: list[WorkflowFailure] = field(default_factory=list)
    open_issues: list[OpenIssue] = field(default_factory=list)
    open_pull_requests: list[OpenPullRequest] = field(default_factory=list)
    code_scanning_alerts: list[CodeScanningAlert] = field(default_factory=list)
    secret_scanning_alerts: list[SecretScanningAlert] = field(default_factory=list)
    resolved_failures: int = 0  # Fehllaeufe im Fenster, die seither wieder gruen sind (unterdrueckt)
    closed_pr_failures: int = 0  # PR-Fehllaeufe ohne offenen PR (geschlossen/gemerged)
    open_issues_total: int = 0
    open_pull_requests_total: int = 0
    code_scanning_total: int = 0
    secret_scanning_total: int = 0
    missing_description: bool = False
    missing_topics: bool = False
    is_locked: bool = False
    lock_reason: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def dependabot_count(self) -> int:
        return len(self.dependabot_alerts)

    @property
    def failed_runs_count(self) -> int:
        return len(self.failed_runs)

    @property
    def open_issues_count(self) -> int:
        return self.open_issues_total or len(self.open_issues)

    @property
    def open_pull_requests_count(self) -> int:
        return self.open_pull_requests_total or len(self.open_pull_requests)

    @property
    def code_scanning_count(self) -> int:
        return self.code_scanning_total or len(self.code_scanning_alerts)

    @property
    def secret_scanning_count(self) -> int:
        return self.secret_scanning_total or len(self.secret_scanning_alerts)

    @property
    def copilot_autofix_count(self) -> int:
        return sum(1 for alert in self.code_scanning_alerts if alert.has_copilot_autofix())

    def has_findings(self) -> bool:
        return bool(
            self.dependabot_alerts
            or self.failed_runs
            or self.open_issues
            or self.open_pull_requests
            or self.code_scanning_alerts
            or self.secret_scanning_alerts
            or (self.missing_description and not self.is_locked)
            or (self.missing_topics and not self.is_locked)
        )


class RepoHealthCollector:
    """Laedt Repo-Health ueber den Transport (Standard: `gh api`). Nur lesend."""

    def __init__(
        self,
        config: dict | None = None,
        transport=None,
        lock_check: Optional[Callable[[str], tuple[bool, str]]] = None,
    ):
        config = config or {}
        self.transport = transport or GhCliTransport()
        # Optionaler Adapter: (gesperrt?, Grund) je Repo -- z. B. aus einem lokalen Register.
        self.lock_check = lock_check
        self.window_days = int(config.get("failed_runs_window_days", 14))
        self.max_failed_runs_per_repo = int(config.get("max_failed_runs_per_repo", 5))
        self.max_dependabot_alerts_per_repo = int(config.get("max_dependabot_alerts_per_repo", 10))
        self.max_open_issues_per_repo = int(config.get("max_open_issues_per_repo", 10))
        self.max_open_prs_per_repo = int(config.get("max_open_prs_per_repo", 10))
        self.max_code_scanning_alerts_per_repo = int(config.get("max_code_scanning_alerts_per_repo", 20))
        self.max_secret_scanning_alerts_per_repo = int(config.get("max_secret_scanning_alerts_per_repo", 20))
        self.check_copilot_autofix = bool(config.get("check_copilot_autofix", True))
        self.max_copilot_autofix_checks_per_repo = int(config.get("max_copilot_autofix_checks_per_repo", 10))
        self.check_metadata = bool(config.get("check_metadata", True))

    def fetch(self, repo: str) -> RepoHealthSummary:
        """Laedt Dependabot- und Actions-Health fuer ein Repo."""
        summary = RepoHealthSummary(repo=repo)

        alerts, alert_error = self._fetch_dependabot_alerts(repo)
        summary.dependabot_alerts = alerts
        if alert_error:
            summary.errors.append(alert_error)

        issues, issue_total, prs, pr_total, issue_error = self._fetch_open_issues_and_prs(repo)
        summary.open_issues = issues
        summary.open_issues_total = issue_total
        summary.open_pull_requests = prs
        summary.open_pull_requests_total = pr_total
        if issue_error:
            summary.errors.append(issue_error)

        code_alerts, code_total, code_error = self._fetch_code_scanning_alerts(repo)
        summary.code_scanning_alerts = code_alerts
        summary.code_scanning_total = code_total
        if code_error:
            summary.errors.append(code_error)

        secret_alerts, secret_total, secret_error = self._fetch_secret_scanning_alerts(repo)
        summary.secret_scanning_alerts = secret_alerts
        summary.secret_scanning_total = secret_total
        if secret_error:
            summary.errors.append(secret_error)

        failed_runs, resolved_count, closed_pr_count, runs_error = self._fetch_failed_runs(repo)
        summary.failed_runs = failed_runs
        summary.resolved_failures = resolved_count
        summary.closed_pr_failures = closed_pr_count
        if runs_error:
            summary.errors.append(runs_error)

        if self.check_metadata:
            desc, topics, metadata_error = self._get_repo_metadata(repo)
            if metadata_error:
                summary.errors.append(metadata_error)
            else:
                summary.missing_description = not bool(str(desc or "").strip())
                summary.missing_topics = len(topics or []) == 0

                if self.lock_check is not None:
                    summary.is_locked, summary.lock_reason = self.lock_check(repo)

                if not summary.is_locked:
                    if summary.missing_description:
                        logger.warning("[METADATA] %s: Description fehlt!", repo)
                    if summary.missing_topics:
                        logger.warning("[METADATA] %s: Topics fehlen!", repo)

        return summary

    def _get_repo_metadata(self, repo: str) -> tuple[str, list[str], str | None]:
        """Holt Description und Topics via API."""
        data, err = self._call(f"repos/{repo}")
        if not err and isinstance(data, dict):
            desc = data.get("description") or ""
            topics = data.get("topics") or []
            return desc, topics, None
        return "", [], err or f"Metadaten-Antwort für {repo} ist ungültig"

    def fetch_all(self, repos: list[str]) -> list[RepoHealthSummary]:
        """Laedt Repo-Health fuer mehrere Repos."""
        return [self.fetch(repo) for repo in repos]

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def _fetch_dependabot_alerts(self, repo: str) -> tuple[list[DependabotAlert], Optional[str]]:
        data, error = self._call_paginated(f"repos/{repo}/dependabot/alerts?state=open&per_page=100")
        if error:
            return [], f"{repo}: Dependabot-Warnungen konnten nicht geladen werden ({error})"

        alerts = []
        for item in (data or [])[: self.max_dependabot_alerts_per_repo]:
            dependency = item.get("dependency") or {}
            package = dependency.get("package") or {}
            vulnerability = item.get("security_vulnerability") or {}
            advisory = item.get("security_advisory") or {}

            alerts.append(
                DependabotAlert(
                    number=item.get("number", 0),
                    package_name=package.get("name", ""),
                    ecosystem=package.get("ecosystem", ""),
                    severity=vulnerability.get("severity") or advisory.get("severity") or "unknown",
                    summary=advisory.get("summary", ""),
                    manifest_path=item.get("manifest_path") or dependency.get("manifest_path", ""),
                    created_at=item.get("created_at", ""),
                    html_url=item.get("html_url", ""),
                )
            )

        return alerts, None

    def _fetch_open_issues_and_prs(
        self, repo: str
    ) -> tuple[list[OpenIssue], int, list[OpenPullRequest], int, Optional[str]]:
        data, error = self._call_paginated(f"repos/{repo}/issues?state=open&per_page=100")
        if error:
            return [], 0, [], 0, f"{repo}: offene Issues/PRs konnten nicht geladen werden ({error})"

        issues: list[OpenIssue] = []
        pull_requests: list[OpenPullRequest] = []

        for item in data or []:
            labels = [label.get("name", "") for label in item.get("labels", []) if label.get("name")]
            author = (item.get("user") or {}).get("login", "")
            if item.get("pull_request"):
                pull_requests.append(
                    OpenPullRequest(
                        number=item.get("number", 0),
                        title=item.get("title", ""),
                        author=author,
                        labels=labels,
                        updated_at=item.get("updated_at", ""),
                        html_url=item.get("html_url", ""),
                    )
                )
            else:
                issues.append(
                    OpenIssue(
                        number=item.get("number", 0),
                        title=item.get("title", ""),
                        author=author,
                        labels=labels,
                        updated_at=item.get("updated_at", ""),
                        html_url=item.get("html_url", ""),
                    )
                )

        return (
            issues[: self.max_open_issues_per_repo],
            len(issues),
            pull_requests[: self.max_open_prs_per_repo],
            len(pull_requests),
            None,
        )

    def _fetch_code_scanning_alerts(self, repo: str) -> tuple[list[CodeScanningAlert], int, Optional[str]]:
        data, error = self._call_paginated(f"repos/{repo}/code-scanning/alerts?state=open&per_page=100")
        if error:
            if self._is_optional_security_feature_unavailable(error):
                return [], 0, None
            return [], 0, f"{repo}: Code-Scanning-Alerts konnten nicht geladen werden ({error})"

        alerts: list[CodeScanningAlert] = []
        for item in data or []:
            rule = item.get("rule") or {}
            tool = item.get("tool") or {}
            instance = item.get("most_recent_instance") or {}
            location = instance.get("location") or {}
            alert = CodeScanningAlert(
                number=item.get("number", 0),
                rule_id=rule.get("id", ""),
                rule_name=rule.get("name", ""),
                severity=rule.get("security_severity_level") or rule.get("severity") or "",
                tool_name=tool.get("name", ""),
                path=location.get("path", ""),
                start_line=location.get("start_line") or 0,
                created_at=item.get("created_at", ""),
                html_url=item.get("html_url", ""),
            )
            alerts.append(alert)

        visible_alerts = alerts[: self.max_code_scanning_alerts_per_repo]
        if self.check_copilot_autofix and visible_alerts:
            for alert in visible_alerts[: self.max_copilot_autofix_checks_per_repo]:
                status, url = self._fetch_copilot_autofix(repo, alert.number)
                alert.copilot_autofix_status = status
                alert.copilot_autofix_url = url

        return visible_alerts, len(alerts), None

    def _fetch_secret_scanning_alerts(self, repo: str) -> tuple[list[SecretScanningAlert], int, Optional[str]]:
        data, error = self._call_paginated(f"repos/{repo}/secret-scanning/alerts?state=open&per_page=100")
        if error:
            if self._is_optional_security_feature_unavailable(error):
                return [], 0, None
            return [], 0, f"{repo}: Secret-Scanning-Alerts konnten nicht geladen werden ({error})"

        alerts = [
            SecretScanningAlert(
                number=item.get("number", 0),
                secret_type=item.get("secret_type", ""),
                created_at=item.get("created_at", ""),
                html_url=item.get("html_url", ""),
            )
            for item in data or []
        ]
        return alerts[: self.max_secret_scanning_alerts_per_repo], len(alerts), None

    def _fetch_copilot_autofix(self, repo: str, alert_number: int) -> tuple[str, str]:
        data, error = self._call(f"repos/{repo}/code-scanning/alerts/{alert_number}/autofix")
        if error or not isinstance(data, dict):
            return "", ""
        return data.get("status") or data.get("state") or "available", data.get("html_url", "")

    @staticmethod
    def _is_optional_security_feature_unavailable(error: str) -> bool:
        text = error.lower()
        return any(
            marker in text
            for marker in (
                "not found",
                "no analysis found",
                "not enabled",
                "disabled",
                "advanced security must be enabled",
            )
        )

    # Conclusions, die als echte Bruchstelle zaehlen (cancelled = bewusst abgebrochen, kein Defekt)
    FAILURE_CONCLUSIONS = ("failure", "timed_out", "startup_failure")

    def _fetch_failed_runs(self, repo: str) -> tuple[list[WorkflowFailure], int, int, Optional[str]]:
        """Liefert nur OFFENE Fehllaeufe (Workflow+Branch aktuell rot) + Anzahl seither behobener.

        „since-resolved": Ein Fehllauf wird unterdrueckt, wenn fuer denselben Workflow
        (`workflow_id`) auf demselben Branch (`head_branch`) ein NEUERER abgeschlossener Lauf
        existiert. Es zaehlt nur der juengste abgeschlossene Lauf je (Workflow, Branch):
        ist dessen Conclusion ein Fehler -> offen; sonst (gruen) bzw. aeltere Laeufe -> behoben.
        """
        # ALLE Laeufe (jede Conclusion) laden, API liefert newest-first nach created_at.
        data, error = self._call(f"repos/{repo}/actions/runs?per_page=100")
        if error:
            return [], 0, 0, f"{repo}: fehlgeschlagene Actions-Läufe konnten nicht geladen werden ({error})"

        runs = (data or {}).get("workflow_runs", [])
        cutoff = self._utcnow() - timedelta(days=self.window_days)

        def _key(item: dict) -> tuple:
            return (item.get("workflow_id"), item.get("head_branch", ""))

        # Juengster ABGESCHLOSSENER Lauf je (Workflow, Branch) -- erste Sichtung = neuester (desc-Order)
        latest_completed: dict[tuple, dict] = {}
        for item in runs:
            if item.get("status") != "completed":
                continue
            key = _key(item)
            if key not in latest_completed:
                latest_completed[key] = item

        latest_open_ids = {
            item.get("id") for item in latest_completed.values() if item.get("conclusion") in self.FAILURE_CONCLUSIONS
        }

        open_failures: list[WorkflowFailure] = []
        resolved_count = 0
        closed_pr_count = 0
        for item in runs:
            if item.get("conclusion") not in self.FAILURE_CONCLUSIONS:
                continue
            created_at = _parse_timestamp(item.get("created_at", ""))
            if created_at and created_at < cutoff:
                continue
            # Fehllauf im Fenster: offen nur, wenn er der juengste abgeschlossene Lauf seiner
            # (Workflow, Branch)-Gruppe ist; sonst seither behoben/ueberholt -> unterdruecken.
            if item.get("id") in latest_open_ids:
                if item.get("event") in {
                    "pull_request",
                    "pull_request_target",
                } and not self._has_open_pull_request_for_run(repo, item):
                    closed_pr_count += 1
                    continue
                open_failures.append(
                    WorkflowFailure(
                        run_id=item.get("id", 0),
                        workflow_name=item.get("name", ""),
                        display_title=item.get("display_title", ""),
                        branch=item.get("head_branch", ""),
                        event=item.get("event", ""),
                        actor=(item.get("actor") or {}).get("login", ""),
                        status=item.get("status", ""),
                        conclusion=item.get("conclusion", ""),
                        created_at=item.get("created_at", ""),
                        updated_at=item.get("updated_at", ""),
                        html_url=item.get("html_url", ""),
                    )
                )
            else:
                resolved_count += 1

        open_failures = open_failures[: self.max_failed_runs_per_repo]
        return open_failures, resolved_count, closed_pr_count, None

    def _has_open_pull_request_for_run(self, repo: str, run: dict) -> bool:
        """Prueft, ob ein PR-Run noch zu einem offenen PR gehoert.

        GitHub markiert alte `pull_request_target`-Runs auch nach Merge weiter als
        rot. Fuer Health-Zwecke sind sie keine offene Bruchstelle mehr.
        Bei API-Unsicherheit wird konservativ nicht unterdrueckt.
        """
        branch = run.get("head_branch", "")
        if not branch:
            return True
        head_repo = (run.get("head_repository") or {}).get("full_name") or repo
        head_owner = head_repo.split("/", 1)[0]
        head = quote(f"{head_owner}:{branch}", safe="")
        data, error = self._call(f"repos/{repo}/pulls?state=open&head={head}&per_page=1")
        if error:
            return True
        return bool(data)

    def _call(self, endpoint: str) -> tuple[dict | list | None, Optional[str]]:
        return self.transport.get(endpoint)

    def _call_paginated(self, endpoint: str) -> tuple[list, Optional[str]]:
        return self.transport.get_paginated(endpoint)


def generate_health_report(
    summaries: list[RepoHealthSummary],
    *,
    window_days: int,
    max_dependabot_alerts_per_repo: int,
    max_failed_runs_per_repo: int,
    max_open_issues_per_repo: int = 10,
    max_open_prs_per_repo: int = 10,
    max_code_scanning_alerts_per_repo: int = 20,
    max_secret_scanning_alerts_per_repo: int = 20,
) -> str:
    """Erstellt einen Markdown-Report ueber Repo-Health."""
    today = date.today().strftime("%Y-%m-%d")

    with_dependabot = [s for s in summaries if s.dependabot_alerts]
    with_failures = [s for s in summaries if s.failed_runs]
    with_issues = [s for s in summaries if s.open_issues]
    with_prs = [s for s in summaries if s.open_pull_requests]
    with_code_scanning = [s for s in summaries if s.code_scanning_alerts]
    with_secret_scanning = [s for s in summaries if s.secret_scanning_alerts]
    with_copilot_autofix = [s for s in summaries if s.copilot_autofix_count]
    with_missing_metadata = [s for s in summaries if not s.is_locked and (s.missing_description or s.missing_topics)]
    with_errors = [s for s in summaries if s.errors]
    healthy = [s for s in summaries if not s.errors and not s.has_findings()]

    total_alerts = sum(s.dependabot_count for s in summaries)
    total_failures = sum(s.failed_runs_count for s in summaries)
    total_resolved = sum(s.resolved_failures for s in summaries)
    total_closed_pr_failures = sum(s.closed_pr_failures for s in summaries)
    total_issues = sum(s.open_issues_count for s in summaries)
    total_prs = sum(s.open_pull_requests_count for s in summaries)
    total_code_scanning = sum(s.code_scanning_count for s in summaries)
    total_secret_scanning = sum(s.secret_scanning_count for s in summaries)
    total_copilot_autofix = sum(s.copilot_autofix_count for s in summaries)

    lines = [
        f"## GitHub Health Report -- {today}",
        f"**Repos geprüft:** {len(summaries)}",
        f"**Actions-Fenster:** letzte {window_days} Tage",
        f"**Dependabot-Warnungen:** {total_alerts} in {len(with_dependabot)} Repos",
        f"**Offene fehlgeschlagene Actions-Läufe:** {total_failures} in {len(with_failures)} Repos "
        f"(aktuell rot; {total_resolved} seither behobene Fehlläufe, "
        f"{total_closed_pr_failures} geschlossene/gemergte PR-Fehlläufe unterdrückt)",
        f"**Offene Issues:** {total_issues} in {len(with_issues)} Repos",
        f"**Offene Pull Requests:** {total_prs} in {len(with_prs)} Repos",
        f"**Offene Code-Scanning-Alerts:** {total_code_scanning} in {len(with_code_scanning)} Repos",
        f"**Offene Secret-Scanning-Alerts:** {total_secret_scanning} in {len(with_secret_scanning)} Repos",
        f"**Copilot-Autofix-Signale:** {total_copilot_autofix} in {len(with_copilot_autofix)} Repos",
        f"**Repos mit fehlenden Metadaten:** {len(with_missing_metadata)} in {len(summaries)} Repos (ohne gelockte Repos)",
        "",
        f'*„Offen" = der jüngste abgeschlossene Lauf je Workflow+Branch ist rot. Fehlläufe, die ein '
        f"späterer Lauf wieder grün gemacht hat, werden nur gezählt, nicht gelistet. "
        f"Pro Repo max. {max_dependabot_alerts_per_repo} Dependabot-Warnungen / "
        f"{max_failed_runs_per_repo} offene Läufe / "
        f"{max_open_issues_per_repo} Issues / {max_open_prs_per_repo} PRs / "
        f"{max_code_scanning_alerts_per_repo} Code-Scanning-Alerts / "
        f"{max_secret_scanning_alerts_per_repo} Secret-Scanning-Alerts gelistet.*",
        "",
    ]

    if with_dependabot:
        lines.append("### Repos mit offenen Dependabot-Warnungen")
        lines.append("")
        for summary in sorted(with_dependabot, key=lambda item: (-item.dependabot_count, item.repo)):
            lines.append(f"**{summary.repo}** -- {summary.dependabot_count} Warnung(en)")
            for alert in summary.dependabot_alerts:
                package = alert.package_name or "unbekanntes Paket"
                ecosystem = f" ({alert.ecosystem})" if alert.ecosystem else ""
                severity = alert.severity or "unknown"
                text = alert.summary or "Keine Kurzbeschreibung vorhanden."
                lines.append(f"- `{severity}` -- `{package}`{ecosystem}: {text}")
                if alert.manifest_path:
                    lines.append(f"  Manifest: `{alert.manifest_path}`")
                if alert.html_url:
                    lines.append(f"  Link: {alert.html_url}")
            lines.append("")

    if with_failures:
        lines.append("### Repos mit OFFENEN fehlgeschlagenen Actions-Läufen (aktuell rot)")
        lines.append("")
        for summary in sorted(with_failures, key=lambda item: (-item.failed_runs_count, item.repo)):
            resolved_note = f" (+{summary.resolved_failures} seither behoben)" if summary.resolved_failures else ""
            lines.append(f"**{summary.repo}** -- {summary.failed_runs_count} offen{resolved_note}")
            for run in summary.failed_runs:
                workflow_name = run.workflow_name or "Unbenannter Workflow"
                display_title = run.display_title or workflow_name
                created = run.created_at[:10] if run.created_at else "unbekannt"
                details = [created, workflow_name]
                if run.branch:
                    details.append(f"Branch `{run.branch}`")
                if run.event:
                    details.append(f"Event `{run.event}`")
                if run.actor:
                    details.append(f"Actor `{run.actor}`")
                if run.conclusion:
                    details.append(f"Conclusion `{run.conclusion}`")
                lines.append(f"- {' -- '.join(details)}")
                lines.append(f"  Titel: {display_title}")
                if run.html_url:
                    lines.append(f"  Link: {run.html_url}")
            lines.append("")

    if with_issues:
        lines.append("### Repos mit offenen Issues")
        lines.append("")
        for summary in sorted(with_issues, key=lambda item: (-item.open_issues_count, item.repo)):
            shown = len(summary.open_issues)
            suffix = f" (zeige {shown})" if shown < summary.open_issues_count else ""
            lines.append(f"**{summary.repo}** -- {summary.open_issues_count} offen{suffix}")
            for issue in summary.open_issues:
                labels = f" -- Labels: `{', '.join(issue.labels)}`" if issue.labels else ""
                author = f" -- Autor `{issue.author}`" if issue.author else ""
                lines.append(f"- #{issue.number} -- {issue.title}{author}{labels}")
                if issue.html_url:
                    lines.append(f"  Link: {issue.html_url}")
            lines.append("")

    if with_prs:
        lines.append("### Repos mit offenen Pull Requests")
        lines.append("")
        for summary in sorted(with_prs, key=lambda item: (-item.open_pull_requests_count, item.repo)):
            shown = len(summary.open_pull_requests)
            suffix = f" (zeige {shown})" if shown < summary.open_pull_requests_count else ""
            lines.append(f"**{summary.repo}** -- {summary.open_pull_requests_count} offen{suffix}")
            for pr in summary.open_pull_requests:
                labels = f" -- Labels: `{', '.join(pr.labels)}`" if pr.labels else ""
                author = f" -- Autor `{pr.author}`" if pr.author else ""
                lines.append(f"- #{pr.number} -- {pr.title}{author}{labels}")
                if pr.html_url:
                    lines.append(f"  Link: {pr.html_url}")
            lines.append("")

    if with_code_scanning:
        lines.append("### Repos mit offenen Code-Scanning-Alerts")
        lines.append("")
        for summary in sorted(with_code_scanning, key=lambda item: (-item.code_scanning_count, item.repo)):
            shown = len(summary.code_scanning_alerts)
            suffix = f" (zeige {shown})" if shown < summary.code_scanning_count else ""
            lines.append(f"**{summary.repo}** -- {summary.code_scanning_count} offen{suffix}")
            for alert in summary.code_scanning_alerts:
                rule = alert.rule_id or alert.rule_name or "unbekannte Regel"
                severity = alert.severity or "unknown"
                location = f" -- `{alert.path}:{alert.start_line}`" if alert.path else ""
                tool = f" -- Tool `{alert.tool_name}`" if alert.tool_name else ""
                autofix = ""
                if alert.has_copilot_autofix():
                    status = alert.copilot_autofix_status or "available"
                    autofix = f" -- Copilot Autofix `{status}`"
                lines.append(f"- #{alert.number} -- `{severity}` -- `{rule}`{tool}{location}{autofix}")
                if alert.html_url:
                    lines.append(f"  Link: {alert.html_url}")
            lines.append("")

    if with_secret_scanning:
        lines.append("### Repos mit offenen Secret-Scanning-Alerts")
        lines.append("")
        for summary in sorted(with_secret_scanning, key=lambda item: (-item.secret_scanning_count, item.repo)):
            shown = len(summary.secret_scanning_alerts)
            suffix = f" (zeige {shown})" if shown < summary.secret_scanning_count else ""
            lines.append(f"**{summary.repo}** -- {summary.secret_scanning_count} offen{suffix}")
            for alert in summary.secret_scanning_alerts:
                secret_type = alert.secret_type or "unbekannter Secret-Typ"
                lines.append(f"- #{alert.number} -- `{secret_type}`")
                if alert.html_url:
                    lines.append(f"  Link: {alert.html_url}")
            lines.append("")

    if with_missing_metadata:
        lines.append("### Repos mit fehlenden Metadaten (Description / Topics)")
        lines.append("")
        for summary in sorted(with_missing_metadata, key=lambda item: item.repo):
            details = []
            if summary.missing_description:
                details.append("Description fehlt")
            if summary.missing_topics:
                details.append("Topics fehlen")
            lines.append(f"- **{summary.repo}**: {', '.join(details)}")
        lines.append("")

    if healthy:
        names = ", ".join(summary.repo for summary in sorted(healthy, key=lambda item: item.repo))
        lines.append(f"### Unauffällige Repos ({len(healthy)})")
        lines.append(names)
        lines.append("")

    if with_errors:
        lines.append("### Fehler beim Abruf")
        lines.append("")
        for summary in sorted(with_errors, key=lambda item: item.repo):
            for error in summary.errors:
                lines.append(f"- {error}")
        lines.append("")

    if (
        not with_dependabot
        and not with_failures
        and not with_issues
        and not with_prs
        and not with_code_scanning
        and not with_secret_scanning
        and not with_missing_metadata
        and not with_errors
        and not healthy
    ):
        lines.append("*Keine offenen Health-Befunde im gewählten Zeitfenster.*")

    return "\n".join(lines)
