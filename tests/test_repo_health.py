"""Tests fuer Repo-Health."""

import json
import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


from githubbot_core.health import (
    CodeScanningAlert,
    DependabotAlert,
    OpenIssue,
    RepoHealthCollector,
    RepoHealthSummary,
    WorkflowFailure,
    generate_health_report,
)


class TestRepoHealthCollector(unittest.TestCase):
    """Prueft den Health-Collector."""

    @patch("githubbot_core.transport.subprocess.run")
    def test_fetch_collects_dependabot_and_recent_failures(self, mock_run):
        alerts_payload = [
            {
                "number": 17,
                "html_url": "https://github.com/org/repo/security/dependabot/17",
                "created_at": "2026-06-18T10:00:00Z",
                "manifest_path": "requirements.txt",
                "dependency": {
                    "package": {
                        "name": "requests",
                        "ecosystem": "pip",
                    }
                },
                "security_vulnerability": {"severity": "high"},
                "security_advisory": {"summary": "Requests vulnerability"},
            }
        ]
        runs_payload = {
            "workflow_runs": [
                {
                    "id": 101,
                    "name": "CI",
                    "display_title": "Fix tests",
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-06-18T09:00:00Z",
                    "updated_at": "2026-06-18T09:05:00Z",
                    "html_url": "https://github.com/org/repo/actions/runs/101",
                    "actor": {"login": "octocat"},
                },
                {
                    "id": 99,
                    "name": "CI",
                    "display_title": "Old failure",
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-05-01T09:00:00Z",
                    "updated_at": "2026-05-01T09:05:00Z",
                    "html_url": "https://github.com/org/repo/actions/runs/99",
                    "actor": {"login": "octocat"},
                },
            ]
        }

        def fake_run(cmd, **_kwargs):
            endpoint = cmd[-1]
            if "dependabot/alerts" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(alerts_payload), "")
            if "/issues" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps([]), "")
            if "code-scanning/alerts" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps([]), "")
            if "secret-scanning/alerts" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps([]), "")
            if "actions/runs" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(runs_payload), "")
            if endpoint == "repos/org/repo":
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    json.dumps({"description": "Test repo", "topics": ["test"]}),
                    "",
                )
            raise AssertionError(f"Unerwarteter Endpoint: {endpoint}")

        mock_run.side_effect = fake_run

        collector = RepoHealthCollector(
            {
                "failed_runs_window_days": 14,
                "max_failed_runs_per_repo": 5,
                "max_dependabot_alerts_per_repo": 10,
            }
        )

        with patch.object(
            collector,
            "_utcnow",
            return_value=datetime(2026, 6, 19, tzinfo=timezone.utc),
        ):
            summary = collector.fetch("org/repo")

        self.assertEqual(summary.dependabot_count, 1)
        self.assertEqual(summary.failed_runs_count, 1)
        self.assertEqual(summary.dependabot_alerts[0].package_name, "requests")
        self.assertEqual(summary.failed_runs[0].run_id, 101)
        self.assertEqual(summary.failed_runs[0].actor, "octocat")
        self.assertEqual(summary.errors, [])

    @patch("githubbot_core.transport.subprocess.run")
    def test_fetch_records_endpoint_errors(self, mock_run):
        def fake_run(cmd, **_kwargs):
            endpoint = cmd[-1]
            if "dependabot/alerts" in endpoint:
                return subprocess.CompletedProcess(cmd, 1, "", "HTTP 403")
            if "/issues" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps([]), "")
            if "code-scanning/alerts" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps([]), "")
            if "secret-scanning/alerts" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps([]), "")
            if "actions/runs" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"workflow_runs": []}), "")
            raise AssertionError(f"Unerwarteter Endpoint: {endpoint}")

        mock_run.side_effect = fake_run

        collector = RepoHealthCollector()
        summary = collector.fetch("org/repo")

        self.assertEqual(summary.dependabot_count, 0)
        self.assertEqual(summary.failed_runs_count, 0)
        self.assertTrue(summary.errors)
        self.assertIn("Dependabot-Warnungen", summary.errors[0])


class TestGenerateHealthReport(unittest.TestCase):
    """Prueft den Markdown-Report."""

    def test_report_contains_findings_and_summary(self):
        summary = RepoHealthSummary(
            repo="org/repo",
            dependabot_alerts=[
                DependabotAlert(
                    number=1,
                    package_name="requests",
                    ecosystem="pip",
                    severity="high",
                    summary="Requests vulnerability",
                    manifest_path="requirements.txt",
                    html_url="https://github.com/org/repo/security/dependabot/1",
                )
            ],
            failed_runs=[
                WorkflowFailure(
                    run_id=101,
                    workflow_name="CI",
                    display_title="Fix tests",
                    branch="main",
                    event="push",
                    actor="octocat",
                    conclusion="failure",
                    created_at="2026-06-18T09:00:00Z",
                    html_url="https://github.com/org/repo/actions/runs/101",
                )
            ],
            open_issues=[
                OpenIssue(
                    number=7,
                    title="Rename provider-specific API",
                    author="octocat",
                    labels=["documentation"],
                    html_url="https://github.com/org/repo/issues/7",
                )
            ],
            open_issues_total=1,
            code_scanning_alerts=[
                CodeScanningAlert(
                    number=3,
                    rule_id="py/path-injection",
                    severity="high",
                    tool_name="CodeQL",
                    path="server.py",
                    start_line=42,
                    html_url="https://github.com/org/repo/security/code-scanning/3",
                )
            ],
            code_scanning_total=1,
        )

        report = generate_health_report(
            [summary],
            window_days=14,
            max_dependabot_alerts_per_repo=10,
            max_failed_runs_per_repo=5,
        )

        self.assertIn("Dependabot-Warnungen", report)
        self.assertIn("fehlgeschlagenen Actions-Läufen", report)
        self.assertIn("org/repo", report)
        self.assertIn("requests", report)
        self.assertIn("Fix tests", report)
        self.assertIn("Rename provider-specific API", report)
        self.assertIn("py/path-injection", report)

    def test_report_contains_missing_metadata_section(self):
        summary_unlocked = RepoHealthSummary(
            repo="org/unlocked-repo",
            missing_description=True,
            missing_topics=True,
            is_locked=False,
        )
        summary_locked = RepoHealthSummary(
            repo="org/locked-repo",
            missing_description=True,
            missing_topics=True,
            is_locked=True,
            lock_reason="exclude_writes",
        )
        report = generate_health_report(
            [summary_unlocked, summary_locked],
            window_days=14,
            max_dependabot_alerts_per_repo=10,
            max_failed_runs_per_repo=5,
        )
        self.assertIn("Repos mit fehlenden Metadaten", report)
        self.assertIn("- **org/unlocked-repo**", report)
        self.assertNotIn("- **org/locked-repo**", report)

    def test_has_findings_ignores_locked_metadata(self):
        summary_locked = RepoHealthSummary(
            repo="org/locked-repo",
            missing_description=True,
            missing_topics=True,
            is_locked=True,
        )
        self.assertFalse(summary_locked.has_findings())

        summary_unlocked = RepoHealthSummary(
            repo="org/unlocked-repo",
            missing_description=True,
            is_locked=False,
        )
        self.assertTrue(summary_unlocked.has_findings())

    @patch("githubbot_core.transport.subprocess.run")
    def test_metadata_api_error_is_unknown_not_missing(self, mock_run):
        def fake_run(cmd, **_kwargs):
            endpoint = cmd[-1]
            if endpoint == "repos/org/repo":
                return subprocess.CompletedProcess(cmd, 1, "", "API unavailable")
            if "actions/runs" in endpoint:
                return subprocess.CompletedProcess(cmd, 0, '{"workflow_runs": []}', "")
            return subprocess.CompletedProcess(cmd, 0, "[]", "")

        mock_run.side_effect = fake_run

        summary = RepoHealthCollector().fetch("org/repo")

        self.assertFalse(summary.missing_description)
        self.assertFalse(summary.missing_topics)
        self.assertTrue(any("API unavailable" in error for error in summary.errors))


if __name__ == "__main__":
    unittest.main()
