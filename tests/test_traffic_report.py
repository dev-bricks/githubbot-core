"""
Tests fuer den Traffic-Report (core/traffic.py) -- inkl. der neuen
Privat-/Archiviert-Sektionen (Sichtbarkeits-Audit fuer private Repos).
"""

import unittest


from githubbot_core.traffic import (
    TrafficSummary,
    generate_ascii_chart,
    generate_traffic_report,
)
from githubbot_core.report_sections import group_repos_by_org, wrap_repo_names


def _make_summaries():
    """Zwei oeffentliche Repos: eines aktiv, eines ohne Traffic (14d)."""
    return [
        TrafficSummary(
            repo="example-org/alpha",
            views_total=50, views_unique=20,
            clones_total=10, clones_unique=5,
        ),
        TrafficSummary(
            repo="example-org/beta",
            views_total=0, views_unique=0,
            clones_total=0, clones_unique=0,
        ),
    ]


class TestBackwardCompatibility(unittest.TestCase):
    """
    Ohne die neuen Parameter (private_repos/archived_repos) bleibt die
    Ausgabe unveraendert -- Pflicht laut Auftrag (bestehende
    Ein-Argument-Aufrufe duerfen sich nicht aendern).
    """

    def test_ascii_chart_unchanged_without_new_params(self):
        summaries = _make_summaries()

        self.assertEqual(
            generate_ascii_chart(summaries), generate_ascii_chart(summaries, None, None)
        )
        out = generate_ascii_chart(summaries)
        self.assertNotIn("PRIVATE MODULE", out)
        self.assertNotIn("ARCHIVIERT", out)
        self.assertNotIn("Privat:", out)

    def test_traffic_report_unchanged_without_new_params(self):
        summaries = _make_summaries()

        self.assertEqual(
            generate_traffic_report(summaries), generate_traffic_report(summaries, None, None)
        )
        out = generate_traffic_report(summaries)
        self.assertIn("**2 Repos** | 14-Tage-Fenster", out)
        self.assertNotIn("Diese Module sind privat", out)
        self.assertNotIn("Archiviert", out)

    def test_empty_lists_behave_like_none(self):
        summaries = _make_summaries()

        self.assertEqual(
            generate_ascii_chart(summaries, [], []), generate_ascii_chart(summaries, None, None)
        )
        self.assertEqual(
            generate_traffic_report(summaries, [], []), generate_traffic_report(summaries, None, None)
        )


class TestPrivateReposExcludedFromTotals(unittest.TestCase):
    """Private Repos duerfen keine Summe/Balken/Aktivitaets-Liste beeinflussen."""

    def test_ascii_totals_unaffected_by_private_repos(self):
        summaries = _make_summaries()

        without_private = generate_ascii_chart(summaries)
        with_private = generate_ascii_chart(
            summaries,
            private_repos=["games-org/AlphaGame", "games-org/BetaGame"],
        )

        self.assertIn(
            "GESAMT  |  Clones: 10 (5 unique)  |  Views: 50 (20 unique)", without_private
        )
        self.assertIn(
            "GESAMT  |  Clones: 10 (5 unique)  |  Views: 50 (20 unique)  |  Privat: 2 (siehe oben)",
            with_private,
        )

    def test_markdown_header_reports_public_and_private_counts(self):
        summaries = _make_summaries()

        report = generate_traffic_report(
            summaries,
            private_repos=["games-org/AlphaGame", "games-org/BetaGame"],
        )

        self.assertIn(
            "**2 öffentliche Repos** in den Zahlen | **2 privat** (siehe unten) | 14-Tage-Fenster",
            report,
        )
        # Die "Keine Aktivitaet"-Zeile zaehlt weiterhin nur das oeffentliche Repo.
        self.assertIn("Keine Aktivität (14d): beta", report)

    def test_private_repo_not_in_no_views_or_inactive_lists(self):
        summaries = _make_summaries()
        private = ["games-org/GhostRepo"]

        ascii_out = generate_ascii_chart(summaries, private_repos=private)
        no_views_line = next(
            l for l in ascii_out.split("\n") if l.strip().startswith("(keine Views):")
        )
        self.assertNotIn("GhostRepo", no_views_line)
        self.assertIn("GhostRepo", ascii_out)  # aber im Privat-Block weiter unten

        md = generate_traffic_report(summaries, private_repos=private)
        inactive_line = next(l for l in md.split("\n") if l.startswith("### Keine Aktivität"))
        self.assertNotIn("GhostRepo", inactive_line)
        self.assertIn("GhostRepo", md)


class TestGroupingAndSorting(unittest.TestCase):
    def test_group_repos_by_org_sorts_orgs_and_names(self):
        repos = ["zeta-org/zebra", "zeta-org/apple", "alpha-org/banana", "alpha-org/apple"]

        groups = group_repos_by_org(repos)

        self.assertEqual(list(groups.keys()), ["alpha-org", "zeta-org"])
        self.assertEqual(groups["alpha-org"], ["apple", "banana"])
        self.assertEqual(groups["zeta-org"], ["apple", "zebra"])

    def test_markdown_private_section_grouped_and_sorted(self):
        summaries = _make_summaries()
        private = ["zeta-org/ZRepo", "alpha-org/BRepo", "alpha-org/ARepo"]

        report = generate_traffic_report(summaries, private_repos=private)

        pos_alpha = report.index("**alpha-org**")
        pos_zeta = report.index("**zeta-org**")
        self.assertLess(pos_alpha, pos_zeta)
        self.assertIn("**alpha-org** (2): ARepo, BRepo", report)
        self.assertIn("**zeta-org** (1): ZRepo", report)


class TestPrivateAndArchivedOverlap(unittest.TestCase):
    def test_repo_private_and_archived_appears_in_both_sections_without_crash(self):
        summaries = _make_summaries()
        shared = "example-org/dual-status-repo"

        md = generate_traffic_report(summaries, private_repos=[shared], archived_repos=[shared])
        self.assertIn("### Diese Module sind privat:", md)
        self.assertIn("### Archiviert:", md)
        self.assertEqual(md.count("dual-status-repo"), 2)

        ascii_out = generate_ascii_chart(summaries, private_repos=[shared], archived_repos=[shared])
        self.assertIn("PRIVATE MODULE", ascii_out)
        self.assertIn("ARCHIVIERT", ascii_out)
        self.assertEqual(ascii_out.count("dual-status-repo"), 2)


class TestAsciiWidth(unittest.TestCase):
    """
    Die 80-Zeichen-Breite gilt fuer die NEUEN Privat-/Archiviert-Bloecke der
    ASCII-Grafik (siehe Meldung: bestehende Tabellen-/Balkenspalten in
    generate_ascii_chart waren bereits vor dieser Aenderung teils >80 Zeichen
    -- ausserhalb meines Schreibbereichs, nicht Teil dieser Pruefung).
    """

    def test_wrap_repo_names_never_exceeds_width(self):
        names = [f"Repo-Mit-Langem-Namen-{i:02d}" for i in range(30)]

        lines = wrap_repo_names(names, prefix="  games-org (30): ", width=80)

        self.assertGreater(len(lines), 1)  # Umbruch muss tatsaechlich stattfinden
        for line in lines:
            self.assertLessEqual(len(line), 80)

    def test_private_archived_blocks_within_width(self):
        summaries = _make_summaries()
        private = [f"games-org/Repo-Mit-Ziemlich-Langem-Namen-{i:02d}" for i in range(15)]
        archived = [f"tools-org/Noch-Ein-Langer-Repo-Name-{i:02d}" for i in range(15)]

        ascii_out = generate_ascii_chart(summaries, private_repos=private, archived_repos=archived)
        lines = ascii_out.split("\n")

        start = next(i for i, l in enumerate(lines) if "PRIVATE MODULE" in l)
        end = next(i for i, l in enumerate(lines) if i > start and l.startswith("=" * 80))

        for line in lines[start - 1:end]:
            self.assertLessEqual(len(line), 80, msg=f"Zeile zu lang: {line!r}")


if __name__ == "__main__":
    unittest.main()
