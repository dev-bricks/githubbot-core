"""Tests fuer DuplicateDetector."""
import unittest


from githubbot_core.duplicates import (
    DuplicateDetector,
    tokenize, jaccard_similarity,
)


class TestTokenize(unittest.TestCase):
    """Testet die Tokenisierung."""

    def test_basic_tokenize(self):
        tokens = tokenize("Hello World Test")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)
        self.assertIn("test", tokens)

    def test_stopwords_removed(self):
        tokens = tokenize("the quick fox is not a dog")
        self.assertNotIn("the", tokens)
        self.assertNotIn("is", tokens)
        self.assertNotIn("not", tokens)
        self.assertNotIn("a", tokens)

    def test_special_chars_removed(self):
        tokens = tokenize("error! crash? bug#123")
        self.assertIn("error", tokens)
        self.assertIn("crash", tokens)
        self.assertIn("bug", tokens)
        self.assertIn("123", tokens)

    def test_empty_string(self):
        tokens = tokenize("")
        self.assertEqual(len(tokens), 0)


class TestJaccardSimilarity(unittest.TestCase):
    """Testet Jaccard-Aehnlichkeit."""

    def test_identical_sets(self):
        self.assertAlmostEqual(jaccard_similarity({"a", "b"}, {"a", "b"}), 1.0)

    def test_disjoint_sets(self):
        self.assertAlmostEqual(jaccard_similarity({"a", "b"}, {"c", "d"}), 0.0)

    def test_partial_overlap(self):
        sim = jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"})
        self.assertAlmostEqual(sim, 2/4)  # 0.5

    def test_empty_sets(self):
        self.assertAlmostEqual(jaccard_similarity(set(), set()), 1.0)

    def test_one_empty(self):
        self.assertAlmostEqual(jaccard_similarity({"a"}, set()), 0.0)


class TestDuplicateDetector(unittest.TestCase):
    """Testet DuplicateDetector."""

    def setUp(self):
        self.detector = DuplicateDetector(threshold=0.80)

    def test_identical_issues(self):
        """Identische Issues werden als Duplikat erkannt."""
        issues = [
            {"number": 1, "title": "Application crashes on startup", "body": "Error traceback shown"},
            {"number": 2, "title": "Application crashes on startup", "body": "Error traceback shown"},
        ]
        candidates = self.detector.find_duplicates(issues)
        self.assertEqual(len(candidates), 1)
        self.assertGreaterEqual(candidates[0].similarity, 0.80)

    def test_different_issues(self):
        """Verschiedene Issues werden nicht als Duplikat erkannt."""
        issues = [
            {"number": 1, "title": "Add dark mode", "body": "Please implement dark mode"},
            {"number": 2, "title": "Fix login error", "body": "Cannot login with email"},
        ]
        candidates = self.detector.find_duplicates(issues)
        self.assertEqual(len(candidates), 0)

    def test_single_issue(self):
        """Ein einzelnes Issue ergibt keine Duplikate."""
        issues = [{"number": 1, "title": "Bug report", "body": "Something broken"}]
        candidates = self.detector.find_duplicates(issues)
        self.assertEqual(len(candidates), 0)

    def test_empty_list(self):
        """Leere Liste ergibt keine Duplikate."""
        candidates = self.detector.find_duplicates([])
        self.assertEqual(len(candidates), 0)

    def test_sorted_by_similarity(self):
        """Ergebnisse sind nach Aehnlichkeit sortiert."""
        issues = [
            {"number": 1, "title": "crash error bug", "body": "crash error bug details"},
            {"number": 2, "title": "crash error bug", "body": "crash error bug details"},
            {"number": 3, "title": "crash error bug report", "body": "crash error different"},
        ]
        candidates = self.detector.find_duplicates(issues)
        if len(candidates) >= 2:
            self.assertGreaterEqual(candidates[0].similarity, candidates[1].similarity)

    def test_find_for_new_issue(self):
        """find_for_new_issue findet Duplikate fuer ein neues Issue."""
        existing = [
            {"number": 1, "title": "Application crashes on startup", "body": "Error shown"},
            {"number": 2, "title": "Feature request dark mode", "body": "Add dark mode"},
        ]
        candidates = self.detector.find_for_new_issue(
            3, "Application crashes on startup", "Error shown", existing
        )
        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0].original_number, 1)

    def test_threshold_configurable(self):
        """Schwellwert ist konfigurierbar."""
        strict = DuplicateDetector(threshold=0.95)
        loose = DuplicateDetector(threshold=0.30)
        issues = [
            {"number": 1, "title": "crash error report", "body": "details here"},
            {"number": 2, "title": "crash error found", "body": "different details"},
        ]
        strict_results = strict.find_duplicates(issues)
        loose_results = loose.find_duplicates(issues)
        self.assertLessEqual(len(strict_results), len(loose_results))


if __name__ == "__main__":
    unittest.main()
