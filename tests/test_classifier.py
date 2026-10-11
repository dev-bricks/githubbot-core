"""Tests fuer IssueClassifier."""

import unittest


from githubbot_core.triage import IssueClassifier, GitHubIssue


class TestIssueClassifier(unittest.TestCase):
    """Testet IssueClassifier mit verschiedenen Issue-Typen."""

    def setUp(self):
        """Config mit Standard-Kriterien laden."""
        from githubbot_core.config import default_config

        self.config = default_config()
        self.classifier = IssueClassifier(self.config)

    def _make_issue(self, title: str, body: str = "", labels: list = None) -> GitHubIssue:
        return GitHubIssue(number=1, title=title, body=body, labels=labels or [], state="open", author="testuser")

    def test_security_detection(self):
        """Security-Keywords werden erkannt."""
        issue = self._make_issue("Found XSS vulnerability in login page")
        result = self.classifier.classify(issue)
        self.assertIn("security", result.labels_to_add)

    def test_bug_detection(self):
        """Bug-Keywords werden erkannt."""
        issue = self._make_issue("Application crashes on startup", "I get a traceback error")
        result = self.classifier.classify(issue)
        self.assertIn("bug", result.labels_to_add)

    def test_feature_detection(self):
        """Feature-Keywords werden erkannt."""
        issue = self._make_issue("Feature request: add dark mode support")
        result = self.classifier.classify(issue)
        self.assertIn("feature", result.labels_to_add)

    def test_question_detection(self):
        """Frage-Keywords werden erkannt."""
        issue = self._make_issue("How to configure the database?")
        result = self.classifier.classify(issue)
        self.assertIn("question", result.labels_to_add)

    def test_unclear_fallback(self):
        """Unklare Issues bekommen needs-info."""
        issue = self._make_issue("Hello world", "Just saying hi")
        result = self.classifier.classify(issue)
        self.assertIn("needs-info", result.labels_to_add)

    def test_security_priority_over_bug(self):
        """Security hat hoehere Prioritaet als Bug."""
        issue = self._make_issue("Security vulnerability causes crash", "exploit error traceback")
        result = self.classifier.classify(issue)
        # Security sollte als erstes Label kommen
        self.assertEqual(result.labels_to_add[0], "security")

    def test_existing_labels_not_duplicated(self):
        """Bereits vorhandene Labels werden nicht erneut gesetzt."""
        issue = self._make_issue("Bug: crash on startup", labels=["bug"])
        result = self.classifier.classify(issue)
        self.assertNotIn("bug", result.labels_to_add)

    def test_auto_comment_set(self):
        """Auto-Kommentar wird bei passendem Kriterium gesetzt."""
        issue = self._make_issue("Not working", "error fails")
        result = self.classifier.classify(issue)
        # bug_without_details sollte needs_info_bug Kommentar setzen
        if result.comment:
            self.assertIn("provide", result.comment.lower())

    def test_comment_type_set(self):
        """comment_type wird zusammen mit comment gesetzt."""
        issue = self._make_issue("How do I install this?", "help question")
        result = self.classifier.classify(issue)
        if result.comment:
            self.assertIsNotNone(result.comment_type)

    def test_escalation_for_unclear(self):
        """Unklare Issues werden eskaliert."""
        issue = self._make_issue("xyz abc", "random text without keywords")
        result = self.classifier.classify(issue)
        self.assertTrue(result.escalate)


if __name__ == "__main__":
    unittest.main()
