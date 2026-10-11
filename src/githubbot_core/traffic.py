"""
githubbot-core -- Traffic-Abruf
===========================
Ruft GitHub Traffic-Daten fuer ein Repo ab (14-Tage-Fenster).

Endpunkte:
  - /traffic/views        -- Seitenaufrufe (total + unique)
  - /traffic/clones       -- Klone (total + unique)
  - /traffic/popular/referrers  -- Top-Referrer (bis 10)
  - /traffic/popular/paths      -- Meistbesuchte Pfade (bis 10)

Voraussetzung: `gh auth login` mit push-Rechten auf das Repo.

"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from .transport import GhCliTransport

logger = logging.getLogger(__name__)

from .report_sections import (  # noqa: E402
    ARCHIVED_HEADING_ASCII,
    REASON_TRAFFIC as PRIVATE_REASON,
    PRIVATE_HEADING_ASCII,
    ascii_group_block,
    markdown_archived_section,
    markdown_private_section,
)


@dataclass
class TrafficSummary:
    """Zusammengefasste Traffic-Daten eines Repos (14-Tage-Fenster)."""

    repo: str
    views_total: int = 0
    views_unique: int = 0
    clones_total: int = 0
    clones_unique: int = 0
    top_referrers: list[dict] = field(default_factory=list)
    top_paths: list[dict] = field(default_factory=list)
    error: Optional[str] = None

    def is_empty(self) -> bool:
        return self.views_total == 0 and self.clones_total == 0

    def format(self) -> str:
        if self.error:
            return f"  [TRAFFIC ERROR] {self.error}"

        lines = [
            f"  Views (14d): {self.views_total} gesamt / {self.views_unique} unique",
            f"  Clones (14d): {self.clones_total} gesamt / {self.clones_unique} unique",
        ]

        if self.top_referrers:
            refs = ", ".join(f"{r['referrer']} ({r['count']})" for r in self.top_referrers[:3])
            lines.append(f"  Referrer: {refs}")

        if self.top_paths:
            paths = ", ".join(f"{p['path']} ({p['count']})" for p in self.top_paths[:3])
            lines.append(f"  Top-Pfade: {paths}")

        return "\n".join(lines)


class TrafficFetcher:
    """
    Ruft Traffic-Daten fuer GitHub-Repos ab.

    Nutzt den Transport-Adapter (Standard: `gh api`) -- keine Zusatzabhaengigkeiten.
    Token braucht push-Rechte (eigene Repos: automatisch vorhanden).
    """

    def __init__(self, transport=None) -> None:
        self.transport = transport or GhCliTransport()
        self.last_error: Optional[str] = None

    def fetch(self, repo: str) -> TrafficSummary:
        """
        Ruft alle Traffic-Endpunkte fuer ein Repo ab.

        Args:
            repo: "owner/reponame" (z.B. "octo-org/octo-repo")

        Returns:
            TrafficSummary mit allen verfuegbaren Daten
        """
        summary = TrafficSummary(repo=repo)

        views = self._call(f"repos/{repo}/traffic/views")
        if views is None:
            detail = self.last_error or "unbekannter Fehler"
            summary.error = f"Kein Zugriff auf Traffic-Views-API: {detail}"
            return summary
        summary.views_total = views.get("count", 0)
        summary.views_unique = views.get("uniques", 0)

        clones = self._call(f"repos/{repo}/traffic/clones")
        if clones is None:
            detail = self.last_error or "unbekannter Fehler"
            summary.error = f"Kein Zugriff auf Traffic-Clones-API: {detail}"
            return summary
        summary.clones_total = clones.get("count", 0)
        summary.clones_unique = clones.get("uniques", 0)

        referrers = self._call(f"repos/{repo}/traffic/popular/referrers")
        if referrers:
            summary.top_referrers = referrers

        paths = self._call(f"repos/{repo}/traffic/popular/paths")
        if paths:
            summary.top_paths = paths

        return summary

    def fetch_all(self, repos: list[str]) -> list[TrafficSummary]:
        """Ruft Traffic fuer mehrere Repos ab."""
        results = []
        for repo in repos:
            results.append(self.fetch(repo))
        return results

    def _call(self, endpoint: str) -> dict | list | None:
        data, error = self.transport.get(endpoint)
        self.last_error = error
        return None if error else data


# Balkenbreite und Namensspalte gelten fuer BEIDE Grafiken -- nur so sind ein
# Repo-Balken im Gesamtchart und derselbe Balken in der Org-Ansicht optisch
# vergleichbar. Deshalb liegen bar()/fmt() auf Modulebene statt lokal.
BAR_WIDTH = 40
NAME_W = 30


def bar(val: int, max_val: int) -> str:
    """Balken fester Breite, halbe Stufe ab 0,5 -- identisch in allen Grafiken."""
    if max_val == 0:
        return "·" * BAR_WIDTH
    filled = val / max_val * BAR_WIDTH
    full = int(filled)
    half = 1 if (filled - full) >= 0.5 else 0
    return "█" * full + ("▌" if half else "") + "·" * (BAR_WIDTH - full - half)


def fmt(name: str) -> str:
    """Repo-Name auf feste Spaltenbreite, zu lange Namen mit Auslassung."""
    return (name[: NAME_W - 1] + "…") if len(name) > NAME_W else name.ljust(NAME_W)


def generate_ascii_chart(
    summaries: list[TrafficSummary],
    private_repos: list[str] | None = None,
    archived_repos: list[str] | None = None,
) -> str:
    """
    Erstellt eine ASCII-Balkengrafik mit Clones und Views aller Repos.

    `summaries` enthaelt nur oeffentliche Repos (der Aufrufer filtert bereits).
    `private_repos`/`archived_repos` ("org/repo"-Strings, optional) erscheinen
    in eigenen Bloecken am Ende und fliessen NICHT in Summen/Raenge/Balken ein.
    """
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")

    valid = [s for s in summaries if not s.error]
    max_clones = max((s.clones_total for s in valid), default=1)
    max_views = max((s.views_total for s in valid), default=1)

    by_clones = sorted(valid, key=lambda s: s.clones_total, reverse=True)
    by_views = sorted(valid, key=lambda s: s.views_total, reverse=True)
    with_views = [s for s in by_views if s.views_total > 0]
    no_views = [s for s in by_views if s.views_total == 0]

    total_clones = sum(s.clones_total for s in valid)
    total_cu = sum(s.clones_unique for s in valid)
    total_views = sum(s.views_total for s in valid)
    total_vu = sum(s.views_unique for s in valid)

    W = 80
    lines = [
        "=" * W,
        "  GITHUB REPO TRAFFIC  |  letzte 14 Tage",
        f"  Multi-Org  |  Stand: {today}",
        "=" * W,
        "",
        "  CLONES (gesamt / unique)",
        "  " + "─" * (W - 2),
        f"  {'Repo':<{NAME_W}}  {'Clones':>7}  Bar",
        f"  {'─' * NAME_W}  {'─' * 7}  {'─' * BAR_WIDTH}",
    ]

    for s in by_clones:
        name = s.repo.split("/")[1]
        val = f"{s.clones_total:>4} /{s.clones_unique:>3}u"
        lines.append(f"  {fmt(name)}  {val}  {bar(s.clones_total, max_clones)}")

    lines += [
        "",
        "  VIEWS (gesamt / unique)",
        "  " + "─" * (W - 2),
        f"  {'Repo':<{NAME_W}}  {'Views':>7}  Bar",
        f"  {'─' * NAME_W}  {'─' * 7}  {'─' * BAR_WIDTH}",
    ]

    for s in with_views:
        name = s.repo.split("/")[1]
        val = f"{s.views_total:>4} /{s.views_unique:>3}u"
        lines.append(f"  {fmt(name)}  {val}  {bar(s.views_total, max_views)}")

    if no_views:
        names = ", ".join(s.repo.split("/")[1] for s in no_views)
        lines.append(f"  {'(keine Views):':<{NAME_W}}  {'':>7}  {names}")

    lines += ascii_group_block(PRIVATE_HEADING_ASCII, private_repos, W)
    lines += ascii_group_block(ARCHIVED_HEADING_ASCII, archived_repos, W)

    gesamt_line = (
        f"  GESAMT  |  Clones: {total_clones} ({total_cu} unique)  |  Views: {total_views} ({total_vu} unique)"
    )
    if private_repos:
        gesamt_line += f"  |  Privat: {len(private_repos)} (siehe oben)"

    lines += [
        "",
        "=" * W,
        gesamt_line,
        "=" * W,
    ]

    return "\n".join(lines)


GROUP_FALLBACK = "Sonstiges"


def load_repo_groups(path) -> tuple[dict[str, str], str]:
    """
    Laedt die Zuordnung Repo -> Bundle/Thema aus `config/repo_groups.json`.

    Rueckgabe: ({"org/repo": "Gruppenname"}, Fallback-Name). Fehlt die Datei
    oder ist sie unlesbar, kommt eine leere Zuordnung zurueck -- die Grafik
    entsteht dann vollstaendig, nur eben mit allen Repos unter "Sonstiges".
    Der Bericht zaehlt diese Faelle in der Kopfzeile, damit eine veraltete
    Zuordnung auffaellt statt still zu bleiben.
    """
    from pathlib import Path

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Repo-Gruppen nicht geladen (%s): %s", path, exc)
        return {}, GROUP_FALLBACK

    fallback = raw.get("fallback") or GROUP_FALLBACK
    groups = {}
    for repo, entry in (raw.get("groups") or {}).items():
        name = entry.get("group") if isinstance(entry, dict) else entry
        if name:
            groups[repo] = name
    return groups, fallback


def generate_ascii_chart_by_group(
    summaries: list[TrafficSummary],
    groups: dict[str, str],
    private_repos: list[str] | None = None,
    archived_repos: list[str] | None = None,
    fallback: str = GROUP_FALLBACK,
) -> str:
    """
    Zweite Grafik: dieselben Zahlen, nach Organisation und darin nach Bundle
    bzw. Thema gegliedert -- je ein Abschnitt fuer Views und fuer Clones.

    Innerhalb jeder Ebene wird absteigend nach der Kennzahl des Abschnitts
    sortiert: Orgs nach ihrer Summe, Gruppen nach ihrer Summe, Repos nach
    ihrem eigenen Wert. Balkenzeichen und Skalierung sind dieselben wie in
    `generate_ascii_chart` (gemeinsames `bar()`), damit sich beide Dateien
    nebeneinander lesen lassen.

    `groups` bildet "org/repo" auf einen Gruppennamen ab; was dort fehlt,
    landet unter `fallback`.
    """
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    W = 80

    valid = [s for s in summaries if not s.error]
    max_views = max((s.views_total for s in valid), default=1)
    max_clones = max((s.clones_total for s in valid), default=1)
    ungrouped = sum(1 for s in valid if s.repo not in groups)

    def split(summary):
        org, _, name = summary.repo.partition("/")
        return (org or "?"), (name or summary.repo)

    # org -> gruppe -> [summary]
    tree: dict[str, dict[str, list[TrafficSummary]]] = {}
    for s in valid:
        org, _ = split(s)
        tree.setdefault(org, {}).setdefault(groups.get(s.repo, fallback), []).append(s)

    def section(title, unit, value, unique, max_val):
        out = [
            "",
            f"  {title} (gesamt / unique)",
            "  " + "─" * (W - 2),
        ]
        orgs = sorted(
            tree.items(),
            key=lambda kv: (-sum(value(s) for g in kv[1].values() for s in g), kv[0]),
        )
        for org, by_group in orgs:
            org_sum = sum(value(s) for g in by_group.values() for s in g)
            org_uni = sum(unique(s) for g in by_group.values() for s in g)
            out.append("")
            out.append(f"  {org}  ({org_sum} {unit} / {org_uni} unique)")
            ordered = sorted(
                by_group.items(),
                key=lambda kv: (-sum(value(s) for s in kv[1]), kv[0]),
            )
            for group, members in ordered:
                group_sum = sum(value(s) for s in members)
                group_uni = sum(unique(s) for s in members)
                out.append(f"    {group}")
                for s in sorted(members, key=lambda s: (-value(s), split(s)[1].lower())):
                    name = split(s)[1]
                    val = f"{value(s):>4} /{unique(s):>3}u"
                    out.append(f"      {fmt(name)}  {val}  {bar(value(s), max_val)}")
                out.append(f"      {fmt('Summe ' + group)}  {group_sum:>4} /{group_uni:>3}u")
            out.append(f"    {fmt('Summe ' + org):<{NAME_W + 2}}  {org_sum:>4} /{org_uni:>3}u")
        return out

    lines = [
        "=" * W,
        "  GITHUB REPO TRAFFIC NACH ORGANISATION  |  letzte 14 Tage",
        f"  Gruppierung: Bundle bzw. Thema  |  {fallback}: {ungrouped}  |  Stand: {today}",
        "=" * W,
    ]
    lines += section("VIEWS", "Views", lambda s: s.views_total, lambda s: s.views_unique, max_views)
    lines += section(
        "CLONES (Downloads)",
        "Clones",
        lambda s: s.clones_total,
        lambda s: s.clones_unique,
        max_clones,
    )

    lines += ascii_group_block(PRIVATE_HEADING_ASCII, private_repos, W)
    lines += ascii_group_block(ARCHIVED_HEADING_ASCII, archived_repos, W)

    total_views = sum(s.views_total for s in valid)
    total_vu = sum(s.views_unique for s in valid)
    total_clones = sum(s.clones_total for s in valid)
    total_cu = sum(s.clones_unique for s in valid)
    gesamt_line = (
        f"  GESAMT  |  Views: {total_views} ({total_vu} unique)  |  Clones: {total_clones} ({total_cu} unique)"
    )
    if private_repos:
        gesamt_line += f"  |  Privat: {len(private_repos)} (siehe oben)"

    lines += ["", "=" * W, gesamt_line, "=" * W]
    return "\n".join(lines)


def generate_traffic_report(
    summaries: list[TrafficSummary],
    private_repos: list[str] | None = None,
    archived_repos: list[str] | None = None,
) -> str:
    """
    Erstellt einen Markdown-Report ueber alle Traffic-Daten.

    `summaries` enthaelt nur oeffentliche Repos (der Aufrufer filtert bereits).
    `private_repos`/`archived_repos` ("org/repo"-Strings, optional) erscheinen
    als eigene Sektionen am Ende und fliessen NICHT in die Zahlen ein.
    """
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")

    if private_repos:
        header_line = (
            f"**{len(summaries)} öffentliche Repos** in den Zahlen | "
            f"**{len(private_repos)} privat** (siehe unten) | 14-Tage-Fenster"
        )
    else:
        header_line = f"**{len(summaries)} Repos** | 14-Tage-Fenster"

    lines = [
        f"## GitHub Traffic Report -- {today}",
        header_line,
        "",
    ]

    # Sortiert nach Views (hoeher = weiter oben)
    sorted_summaries = sorted(summaries, key=lambda s: s.views_total, reverse=True)

    active = [s for s in sorted_summaries if not s.is_empty() and not s.error]
    inactive = [s for s in sorted_summaries if s.is_empty() and not s.error]
    errors = [s for s in sorted_summaries if s.error]

    if active:
        lines.append("### Aktive Repos (Traffic in den letzten 14 Tagen):")
        lines.append("")
        for s in active:
            lines.append(f"**{s.repo}**")
            lines.append(s.format())
            lines.append("")

    if inactive:
        names = ", ".join(s.repo.split("/")[1] for s in inactive)
        lines.append(f"### Keine Aktivität (14d): {names}")
        lines.append("")

    if errors:
        lines.append("### Fehler (kein Zugriff):")
        for s in errors:
            lines.append(f"- {s.repo}: {s.error}")
        lines.append("")

    lines += markdown_private_section(private_repos, PRIVATE_REASON)
    lines += markdown_archived_section(archived_repos)

    return "\n".join(lines)
