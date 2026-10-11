"""Tests fuer AutoCommentManager."""

import unittest


from githubbot_core.comments import AutoCommentManager, CommentTemplate


class TestCommentTemplate(unittest.TestCase):
    """Testet CommentTemplate."""

    def test_render_with_variable(self):
        tmpl = CommentTemplate(key="test", trigger="test", template="Issue #{original_issue} is duplicate")
        result = tmpl.render(original_issue=42)
        self.assertEqual(result, "Issue #42 is duplicate")

    def test_render_without_variable(self):
        tmpl = CommentTemplate(key="test", trigger="test", template="Static message")
        result = tmpl.render()
        self.assertEqual(result, "Static message")

    def test_render_missing_variable_fallback(self):
        tmpl = CommentTemplate(key="test", trigger="test", template="Issue #{original_issue}")
        result = tmpl.render()  # Missing variable
        self.assertEqual(result, "Issue #{original_issue}")  # Fallback


class TestAutoCommentManager(unittest.TestCase):
    """Testet AutoCommentManager."""

    def setUp(self):
        self.config = {
            "auto_comments": {
                "needs_info_bug": {
                    "trigger": "bug_without_details",
                    "template": "Please provide: version, OS, steps to reproduce",
                },
                "duplicate_reference": {"trigger": "duplicate", "template": "Duplicate of #{original_issue}. Closing."},
            }
        }
        self.manager = AutoCommentManager(self.config)

    def test_get_comment(self):
        result = self.manager.get_comment("needs_info_bug")
        self.assertIn("provide", result.lower())

    def test_get_comment_with_variable(self):
        result = self.manager.get_comment("duplicate_reference", original_issue=42)
        self.assertIn("#42", result)

    def test_get_nonexistent_template(self):
        result = self.manager.get_comment("nonexistent_key")
        self.assertIsNone(result)

    def test_has_template(self):
        self.assertTrue(self.manager.has_template("needs_info_bug"))
        self.assertFalse(self.manager.has_template("nonexistent"))

    def test_list_templates(self):
        keys = self.manager.list_templates()
        self.assertIn("needs_info_bug", keys)
        self.assertIn("duplicate_reference", keys)

    def test_add_marker(self):
        text = "Some comment"
        marked = self.manager.add_marker(text, "needs_info_bug")
        self.assertIn("<!-- githubbot:needs_info_bug -->", marked)

    def test_check_already_commented_true(self):
        """Erkennt vorhandenen Bot-Kommentar."""
        comments = [{"body": "Some comment\n<!-- githubbot:needs_info_bug -->"}]
        result = self.manager.check_already_commented(1, "needs_info_bug", comments)
        self.assertTrue(result)

    def test_check_already_commented_false(self):
        """Erkennt fehlenden Bot-Kommentar."""
        comments = [{"body": "Regular user comment without marker"}]
        result = self.manager.check_already_commented(1, "needs_info_bug", comments)
        self.assertFalse(result)

    def test_check_already_commented_empty(self):
        """Leere Kommentarliste = nicht kommentiert."""
        result = self.manager.check_already_commented(1, "needs_info_bug", [])
        self.assertFalse(result)

    def test_check_different_type(self):
        """Anderer Kommentar-Typ wird nicht verwechselt."""
        comments = [{"body": "Comment\n<!-- githubbot:duplicate_reference -->"}]
        result = self.manager.check_already_commented(1, "needs_info_bug", comments)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
