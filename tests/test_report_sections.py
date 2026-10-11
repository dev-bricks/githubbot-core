# -*- coding: utf-8 -*-
"""
Tests fuer core/report_sections.py -- die Bausteine, die Traffic- und
Sterne-Bericht gemeinsam nutzen.

Die Helfer lagen bis 2026-08-02 doppelt in core/traffic.py und core/stars.py.
Sie liegen jetzt einmal hier, damit die Formulierungen der Privat-Sektion in
beiden Berichten nicht auseinanderdriften -- ein Textunterschied liest sich
sonst wie ein inhaltlicher.
"""

import pytest


from githubbot_core.report_sections import (  # noqa: E402
    ARCHIVED_HEADING_MD,
    PRIVATE_HEADING_MD,
    REASON_STARS,
    REASON_TRAFFIC,
    ascii_group_block,
    group_repos_by_org,
    markdown_archived_section,
    markdown_private_section,
    wrap_repo_names,
)


class TestGruppierung:
    def test_sortiert_orgs_und_namen(self):
        result = group_repos_by_org(
            [
                "zeta/b",
                "alpha/z",
                "alpha/a",
                "zeta/a",
            ]
        )
        assert list(result) == ["alpha", "zeta"]
        assert result["alpha"] == ["a", "z"]
        assert result["zeta"] == ["a", "b"]

    def test_name_ohne_org_landet_unter_leerem_schluessel(self):
        assert group_repos_by_org(["ohne-org"]) == {"": ["ohne-org"]}

    def test_leere_liste(self):
        assert group_repos_by_org([]) == {}


class TestZeilenumbruch:
    def test_kurze_liste_bleibt_einzeilig(self):
        lines = wrap_repo_names(["a", "b"], "  org (2): ", width=80)
        assert lines == ["  org (2): a, b"]

    def test_umgebrochene_zeile_endet_auf_komma(self):
        """
        Ohne Komma am Zeilenende liest sich die Fortsetzung wie eine neue
        Aufzaehlung -- genau das war im ersten Bericht vom 2026-08-02 zu sehen.
        """
        names = [f"repo-nummer-{i:02d}" for i in range(10)]
        lines = wrap_repo_names(names, "  org (10): ", width=60)
        assert len(lines) > 1, "Testdaten muessen einen Umbruch erzwingen"
        for line in lines[:-1]:
            assert line.endswith(","), f"Zeile ohne Trennkomma: {line!r}"
        assert not lines[-1].endswith(","), "letzte Zeile braucht kein Komma"

    def test_haelt_die_breite_ein_inklusive_komma(self):
        """Bei realistischen Breiten passt jede Zeile samt Trennkomma."""
        names = [f"ein-ziemlich-langer-repo-name-{i:02d}" for i in range(12)]
        for width in (60, 80, 100):
            for line in wrap_repo_names(names, "  org (12): ", width=width):
                assert len(line) <= width, f"{len(line)} > {width}: {line!r}"

    def test_breite_wird_nur_von_einzelelementen_gesprengt(self):
        """
        Die eigentliche Invariante: eine zu breite Zeile darf hoechstens EIN
        Element tragen. Passt schon `prefix` + ein Name nicht in die Breite,
        ist die Ueberschreitung unvermeidbar -- aber sie darf sich nie
        dadurch ergeben, dass ein zweites Element zu viel angehaengt wurde.
        """
        names = [f"ein-ziemlich-langer-repo-name-{i:02d}" for i in range(12)]
        for width in (30, 40, 50, 60, 80):
            for line in wrap_repo_names(names, "  org (12): ", width=width):
                if len(line) > width:
                    assert line.rstrip(",").count(", ") == 0, (
                        f"Zeile ueberschreitet {width} mit mehreren Elementen: {line!r}"
                    )

    def test_alle_namen_bleiben_erhalten(self):
        names = [f"repo{i}" for i in range(25)]
        text = " ".join(wrap_repo_names(names, "  org (25): ", width=50))
        for name in names:
            assert name in text

    def test_ueberlanger_einzelname_steht_allein(self):
        lang = "x" * 200
        lines = wrap_repo_names(["kurz", lang], "  org (2): ", width=40)
        assert any(lang in line for line in lines)

    def test_folgezeilen_sind_eingerueckt(self):
        names = [f"repo-nummer-{i:02d}" for i in range(10)]
        lines = wrap_repo_names(names, "  org (10): ", width=50, indent="    ")
        assert all(line.startswith("    ") for line in lines[1:])


class TestMarkdownSektionen:
    def test_leere_liste_erzeugt_keine_sektion(self):
        assert markdown_private_section([], REASON_TRAFFIC) == []
        assert markdown_private_section(None, REASON_TRAFFIC) == []
        assert markdown_archived_section([]) == []
        assert markdown_archived_section(None) == []

    def test_ueberschrift_ist_der_wortlaut_den_der_nutzer_sucht(self):
        lines = markdown_private_section(["o/r"], REASON_TRAFFIC)
        assert lines[0] == "### Diese Module sind privat:"
        assert PRIVATE_HEADING_MD == lines[0]

    def test_gruppiert_und_zaehlt_je_org(self):
        lines = markdown_private_section(["a/z", "a/y", "b/x"], REASON_TRAFFIC)
        assert "- **a** (2): y, z" in lines
        assert "- **b** (1): x" in lines

    def test_begruendung_unterscheidet_sich_je_bericht(self):
        traffic = "\n".join(markdown_private_section(["o/r"], REASON_TRAFFIC))
        stars = "\n".join(markdown_private_section(["o/r"], REASON_STARS))
        assert "0 Views/0 Clones" in traffic
        assert "Sternenzahl" in stars
        # Der Audit-Zweck steht in beiden -- das ist die gemeinsame Aussage.
        assert "Sichtbarkeits-Audit" in traffic and "Sichtbarkeits-Audit" in stars

    def test_archiviert_hat_eigene_ueberschrift(self):
        assert markdown_archived_section(["o/alt"])[0] == ARCHIVED_HEADING_MD

    def test_umlaute_bleiben_echt(self):
        text = "\n".join(markdown_private_section(["o/r"], REASON_TRAFFIC))
        assert "veröffentlicht" in text
        assert "ue" not in "prüfen"


class TestAsciiBlock:
    def test_leere_liste_erzeugt_keinen_block(self):
        assert ascii_group_block("EGAL", []) == []
        assert ascii_group_block("EGAL", None) == []

    def test_haelt_die_breite_ein(self):
        repos = [f"games-org/ein-langer-spielname-{i:02d}" for i in range(15)]
        for line in ascii_group_block("PRIVATE MODULE", repos, width=80):
            assert len(line) <= 80, f"{len(line)}: {line!r}"

    def test_beginnt_mit_leerzeile_und_ueberschrift(self):
        block = ascii_group_block("PRIVATE MODULE", ["o/r"])
        assert block[0] == ""
        assert block[1] == "  PRIVATE MODULE"

    @pytest.mark.parametrize("width", [40, 80, 120])
    def test_trennlinie_folgt_der_breite(self, width):
        block = ascii_group_block("X", ["o/r"], width=width)
        assert len(block[2]) == width
