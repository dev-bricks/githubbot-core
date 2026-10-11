"""Tests fuer die Sterne-Rangliste (core/stars.py)."""

import json
import subprocess
import unittest
from unittest.mock import patch


from githubbot_core.stars import (
    RepoStars,
    StarsFetcher,
    generate_ascii_ranking,
    generate_stars_report,
)
from githubbot_core.report_sections import group_repos_by_org, wrap_repo_names


class TestStarsFetcher(unittest.TestCase):
    """Prueft den Stars-Fetcher."""

    @patch("githubbot_core.transport.subprocess.run")
    def test_fetch_reads_stargazers_count(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"stargazers_count": 42}), "")

        fetcher = StarsFetcher()
        result = fetcher.fetch("org/repo")

        self.assertEqual(result.repo, "org/repo")
        self.assertEqual(result.stars, 42)
        self.assertIsNone(result.error)

    @patch("githubbot_core.transport.subprocess.run")
    def test_fetch_records_error_on_api_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 1, "", "HTTP 404")

        fetcher = StarsFetcher()
        result = fetcher.fetch("org/missing-repo")

        self.assertEqual(result.stars, 0)
        self.assertIsNotNone(result.error)
        self.assertIn("HTTP 404", result.error)

    @patch("githubbot_core.transport.subprocess.run")
    def test_fetch_records_json_error_detail(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, "not-json", "")

        result = StarsFetcher().fetch("org/broken-json")

        self.assertIsNotNone(result.error)
        self.assertIn("JSON error", result.error)

    @patch("githubbot_core.transport.subprocess.run")
    def test_fetch_all_preserves_order(self, mock_run):
        payloads = {
            "repos/org/a": {"stargazers_count": 5},
            "repos/org/b": {"stargazers_count": 10},
        }

        def fake_run(cmd, **_kwargs):
            endpoint = cmd[-1]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payloads[endpoint]), "")

        mock_run.side_effect = fake_run

        fetcher = StarsFetcher()
        results = fetcher.fetch_all(["org/a", "org/b"])

        self.assertEqual([r.repo for r in results], ["org/a", "org/b"])
        self.assertEqual([r.stars for r in results], [5, 10])


class TestGenerateAsciiRanking(unittest.TestCase):
    """Prueft die ASCII-Rangliste."""

    def test_ranking_sorted_descending_by_stars(self):
        summaries = [
            RepoStars(repo="org/low", stars=3),
            RepoStars(repo="org/high", stars=99),
            RepoStars(repo="org/mid", stars=20),
        ]

        ranking = generate_ascii_ranking(summaries)

        pos_high = ranking.index("org/high")
        pos_mid = ranking.index("org/mid")
        pos_low = ranking.index("org/low")
        self.assertLess(pos_high, pos_mid)
        self.assertLess(pos_mid, pos_low)
        self.assertIn("3 Repos", ranking)
        self.assertIn("122 Stars", ranking)

    def test_ranking_lists_errors_separately(self):
        summaries = [
            RepoStars(repo="org/ok", stars=1),
            RepoStars(repo="org/broken", error="Kein Zugriff auf Repo-API"),
        ]

        ranking = generate_ascii_ranking(summaries)

        self.assertIn("kein Zugriff", ranking)
        self.assertIn("org/broken", ranking)
        # Fehlerhafte Repos zaehlen nicht in die Rangliste (nur 1 gueltiges Repo).
        self.assertIn("1 Repos", ranking)


class TestGenerateStarsReport(unittest.TestCase):
    """Prueft den Markdown-Report."""

    def test_report_contains_table_and_totals(self):
        summaries = [
            RepoStars(repo="org/high", stars=99),
            RepoStars(repo="org/low", stars=3),
        ]

        report = generate_stars_report(summaries)

        self.assertIn("Sterne-Rangliste", report)
        self.assertIn("org/high", report)
        self.assertIn("org/low", report)
        self.assertIn("102 Stars gesamt", report)

    def test_report_lists_errors(self):
        summaries = [RepoStars(repo="org/broken", error="HTTP 404")]

        report = generate_stars_report(summaries)

        self.assertIn("Fehler (kein Zugriff)", report)
        self.assertIn("org/broken", report)
        self.assertIn("HTTP 404", report)


class TestBackwardCompatibilityWithPrivateArchived(unittest.TestCase):
    """
    Ohne die neuen Parameter (private_repos/archived_repos) bleibt die Ausgabe
    unveraendert -- Pflicht laut Auftrag (bestehende Ein-Argument-Aufrufe
    duerfen sich nicht aendern).
    """

    def test_ascii_ranking_unchanged_without_new_params(self):
        summaries = [RepoStars(repo="org/a", stars=5), RepoStars(repo="org/b", stars=1)]

        self.assertEqual(generate_ascii_ranking(summaries), generate_ascii_ranking(summaries, None, None))
        out = generate_ascii_ranking(summaries)
        self.assertNotIn("PRIVATE MODULE", out)
        self.assertNotIn("ARCHIVIERT", out)
        self.assertNotIn("Privat:", out)

    def test_stars_report_unchanged_without_new_params(self):
        summaries = [RepoStars(repo="org/a", stars=5), RepoStars(repo="org/b", stars=1)]

        self.assertEqual(generate_stars_report(summaries), generate_stars_report(summaries, None, None))
        out = generate_stars_report(summaries)
        self.assertIn("**2 Repos** | **6 Stars gesamt**", out)
        self.assertNotIn("Diese Module sind privat", out)
        self.assertNotIn("Archiviert", out)

    def test_empty_lists_behave_like_none(self):
        summaries = [RepoStars(repo="org/a", stars=5)]

        self.assertEqual(generate_ascii_ranking(summaries, [], []), generate_ascii_ranking(summaries, None, None))
        self.assertEqual(generate_stars_report(summaries, [], []), generate_stars_report(summaries, None, None))


class TestPrivateReposExcludedFromStarsTotals(unittest.TestCase):
    """Private Repos duerfen keine Summe/Rangliste beeinflussen."""

    def test_ascii_totals_unaffected_by_private_repos(self):
        summaries = [RepoStars(repo="org/a", stars=5), RepoStars(repo="org/b", stars=1)]

        without_private = generate_ascii_ranking(summaries)
        with_private = generate_ascii_ranking(
            summaries,
            private_repos=["games-org/AlphaGame", "games-org/BetaGame"],
        )

        self.assertIn("GESAMT  |  2 Repos  |  6 Stars", without_private)
        self.assertIn("GESAMT  |  2 Repos  |  6 Stars  |  Privat: 2 (siehe oben)", with_private)

    def test_markdown_header_reports_public_and_private_counts(self):
        summaries = [RepoStars(repo="org/a", stars=5), RepoStars(repo="org/b", stars=1)]

        report = generate_stars_report(summaries, private_repos=["games-org/AlphaGame"])

        self.assertIn(
            "**2 öffentliche Repos** in den Zahlen | **6 Stars gesamt** | **1 privat** (siehe unten)",
            report,
        )

    def test_private_repo_not_in_ranking_table(self):
        summaries = [RepoStars(repo="org/a", stars=5)]
        private = ["games-org/GhostRepo"]

        ascii_out = generate_ascii_ranking(summaries, private_repos=private)
        table_lines = ascii_out.split("\n")
        ranking_start = next(i for i, l in enumerate(table_lines) if l.strip().startswith("Rang"))
        ranking_end = next(i for i, l in enumerate(table_lines) if "PRIVATE MODULE" in l)
        self.assertFalse(any("GhostRepo" in l for l in table_lines[ranking_start:ranking_end]))
        self.assertIn("GhostRepo", ascii_out)  # aber im Privat-Block weiter unten

        md = generate_stars_report(summaries, private_repos=private)
        table_row_lines = [l for l in md.split("\n") if l.startswith("| ")]
        self.assertFalse(any("GhostRepo" in l for l in table_row_lines))
        self.assertIn("GhostRepo", md)


class TestStarsGroupingAndSorting(unittest.TestCase):
    def test_group_repos_by_org_sorts_orgs_and_names(self):
        repos = ["zeta-org/zebra", "zeta-org/apple", "alpha-org/banana", "alpha-org/apple"]

        groups = group_repos_by_org(repos)

        self.assertEqual(list(groups.keys()), ["alpha-org", "zeta-org"])
        self.assertEqual(groups["alpha-org"], ["apple", "banana"])
        self.assertEqual(groups["zeta-org"], ["apple", "zebra"])

    def test_markdown_private_section_grouped_and_sorted(self):
        summaries = [RepoStars(repo="org/a", stars=5)]
        private = ["zeta-org/ZRepo", "alpha-org/BRepo", "alpha-org/ARepo"]

        report = generate_stars_report(summaries, private_repos=private)

        pos_alpha = report.index("**alpha-org**")
        pos_zeta = report.index("**zeta-org**")
        self.assertLess(pos_alpha, pos_zeta)
        self.assertIn("**alpha-org** (2): ARepo, BRepo", report)
        self.assertIn("**zeta-org** (1): ZRepo", report)


class TestStarsPrivateAndArchivedOverlap(unittest.TestCase):
    def test_repo_private_and_archived_appears_in_both_sections_without_crash(self):
        summaries = [RepoStars(repo="org/a", stars=5)]
        shared = "example-org/dual-status-repo"

        md = generate_stars_report(summaries, private_repos=[shared], archived_repos=[shared])
        self.assertIn("### Diese Module sind privat:", md)
        self.assertIn("### Archiviert:", md)
        self.assertEqual(md.count("dual-status-repo"), 2)

        ascii_out = generate_ascii_ranking(summaries, private_repos=[shared], archived_repos=[shared])
        self.assertIn("PRIVATE MODULE", ascii_out)
        self.assertIn("ARCHIVIERT", ascii_out)
        self.assertEqual(ascii_out.count("dual-status-repo"), 2)


class TestStarsAsciiWidth(unittest.TestCase):
    """
    Die 80-Zeichen-Breite gilt fuer die NEUEN Privat-/Archiviert-Bloecke der
    ASCII-Rangliste (siehe Meldung: bestehende Tabellen-/Balkenspalten in
    generate_ascii_ranking waren bereits vor dieser Aenderung teils >80
    Zeichen -- ausserhalb meines Schreibbereichs, nicht Teil dieser Pruefung).
    """

    def test_wrap_repo_names_never_exceeds_width(self):
        names = [f"Repo-Mit-Langem-Namen-{i:02d}" for i in range(30)]

        lines = wrap_repo_names(names, prefix="  games-org (30): ", width=80)

        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(len(line), 80)

    def test_private_archived_blocks_within_width(self):
        summaries = [RepoStars(repo="org/a", stars=5)]
        private = [f"games-org/Repo-Mit-Ziemlich-Langem-Namen-{i:02d}" for i in range(15)]
        archived = [f"tools-org/Noch-Ein-Langer-Repo-Name-{i:02d}" for i in range(15)]

        ascii_out = generate_ascii_ranking(summaries, private_repos=private, archived_repos=archived)
        lines = ascii_out.split("\n")

        start = next(i for i, l in enumerate(lines) if "PRIVATE MODULE" in l)
        end = next(i for i, l in enumerate(lines) if i > start and l.startswith("=" * 80))

        for line in lines[start - 1 : end]:
            self.assertLessEqual(len(line), 80, msg=f"Zeile zu lang: {line!r}")

    def test_gesamt_line_stays_within_width_with_private_count(self):
        summaries = [RepoStars(repo="org/a", stars=5)]
        private = [
            "games-org/AlphaGame",
            "games-org/BetaGame",
            "example-org/internal-tool",
        ]

        ascii_out = generate_ascii_ranking(summaries, private_repos=private)
        gesamt_line = next(l for l in ascii_out.split("\n") if l.strip().startswith("GESAMT"))

        self.assertLessEqual(len(gesamt_line), 80)


if __name__ == "__main__":
    unittest.main()
