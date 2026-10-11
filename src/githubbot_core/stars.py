"""
githubbot-core -- Sterne-Rangliste
================================
Ruft die Sternenzahl (stargazers_count) fuer Repos ab und erstellt eine
nach Sternen sortierte Rangliste ueber alle Repos aller konfigurierten Orgs.

Endpunkt:
  - /repos/{owner}/{repo}  -- liefert u.a. "stargazers_count"

Es wird nur der Zaehlerwert pro Repo gelesen (ein API-Call pro Repo), keine
Stargazer-Liste und keine Schreibaktion.

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .transport import GhCliTransport

logger = logging.getLogger(__name__)

from .report_sections import (  # noqa: E402
    ARCHIVED_HEADING_ASCII,
    REASON_STARS as PRIVATE_REASON,
    PRIVATE_HEADING_ASCII,
    ascii_group_block,
    markdown_archived_section,
    markdown_private_section,
)


@dataclass
class RepoStars:
    """Sternenzahl eines Repos."""

    repo: str
    stars: int = 0
    error: Optional[str] = None


class StarsFetcher:
    """
    Ruft die Sternenzahl fuer GitHub-Repos ab.

    Nutzt den Transport-Adapter (Standard: `gh api`) -- keine Zusatzabhaengigkeiten.
    """

    def __init__(self, transport=None) -> None:
        self.transport = transport or GhCliTransport()
        self.last_error: Optional[str] = None

    def fetch(self, repo: str) -> RepoStars:
        """
        Ruft die Sternenzahl fuer ein Repo ab.

        Args:
            repo: "owner/reponame" (z.B. "octo-org/octo-repo")

        Returns:
            RepoStars mit Sternenzahl oder Fehlermeldung
        """
        data = self._call(f"repos/{repo}")
        if data is None:
            detail = self.last_error or "unbekannter Fehler"
            return RepoStars(repo=repo, error=f"Kein Zugriff auf Repo-API: {detail}")
        return RepoStars(repo=repo, stars=data.get("stargazers_count", 0))

    def fetch_all(self, repos: list[str]) -> list[RepoStars]:
        """Ruft die Sternenzahl fuer mehrere Repos ab."""
        results = []
        for repo in repos:
            results.append(self.fetch(repo))
        return results

    def _call(self, endpoint: str) -> dict | list | None:
        data, error = self.transport.get(endpoint)
        self.last_error = error
        return None if error else data


def generate_ascii_ranking(
    summaries: list[RepoStars],
    private_repos: list[str] | None = None,
    archived_repos: list[str] | None = None,
) -> str:
    """
    Erstellt eine ASCII-Rangliste aller Repos, absteigend nach Sternen.

    `summaries` enthaelt nur oeffentliche Repos (der Aufrufer filtert bereits).
    `private_repos`/`archived_repos` ("org/repo"-Strings, optional) erscheinen
    in eigenen Bloecken am Ende und fliessen NICHT in Summen/Raenge/Balken ein.
    """
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    BAR_WIDTH = 40
    NAME_W = 38

    def bar(val, max_val):
        if max_val == 0:
            return "·" * BAR_WIDTH
        filled = val / max_val * BAR_WIDTH
        full = int(filled)
        half = 1 if (filled - full) >= 0.5 else 0
        return "█" * full + ("▌" if half else "") + "·" * (BAR_WIDTH - full - half)

    def fmt(name):
        return (name[: NAME_W - 1] + "…") if len(name) > NAME_W else name.ljust(NAME_W)

    valid = [s for s in summaries if not s.error]
    errors = [s for s in summaries if s.error]
    ranked = sorted(valid, key=lambda s: s.stars, reverse=True)
    max_stars = max((s.stars for s in ranked), default=1)
    total_stars = sum(s.stars for s in ranked)

    W = 80
    lines = [
        "=" * W,
        "  GITHUB REPO STERNE-RANGLISTE",
        f"  Multi-Org  |  Stand: {today}",
        "=" * W,
        "",
        f"  {'Rang':>4}  {'Repo':<{NAME_W}}  {'Stars':>6}  Bar",
        f"  {'─' * 4}  {'─' * NAME_W}  {'─' * 6}  {'─' * BAR_WIDTH}",
    ]

    for i, s in enumerate(ranked, 1):
        lines.append(f"  {i:>4}  {fmt(s.repo)}  {s.stars:>6}  {bar(s.stars, max_stars)}")

    if errors:
        names = ", ".join(s.repo for s in errors)
        lines += ["", f"  (kein Zugriff): {names}"]

    lines += ascii_group_block(PRIVATE_HEADING_ASCII, private_repos, W)
    lines += ascii_group_block(ARCHIVED_HEADING_ASCII, archived_repos, W)

    gesamt_line = f"  GESAMT  |  {len(ranked)} Repos  |  {total_stars} Stars"
    if private_repos:
        gesamt_line += f"  |  Privat: {len(private_repos)} (siehe oben)"

    lines += [
        "",
        "=" * W,
        gesamt_line,
        "=" * W,
    ]

    return "\n".join(lines)


def generate_stars_report(
    summaries: list[RepoStars],
    private_repos: list[str] | None = None,
    archived_repos: list[str] | None = None,
) -> str:
    """
    Erstellt einen Markdown-Report mit der Sterne-Rangliste.

    `summaries` enthaelt nur oeffentliche Repos (der Aufrufer filtert bereits).
    `private_repos`/`archived_repos` ("org/repo"-Strings, optional) erscheinen
    als eigene Sektionen am Ende und fliessen NICHT in die Zahlen ein.
    """
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")

    valid = [s for s in summaries if not s.error]
    errors = [s for s in summaries if s.error]
    ranked = sorted(valid, key=lambda s: s.stars, reverse=True)
    total_stars = sum(s.stars for s in ranked)

    if private_repos:
        header_line = (
            f"**{len(ranked)} öffentliche Repos** in den Zahlen | "
            f"**{total_stars} Stars gesamt** | **{len(private_repos)} privat** (siehe unten)"
        )
    else:
        header_line = f"**{len(ranked)} Repos** | **{total_stars} Stars gesamt**"

    lines = [
        f"## GitHub Sterne-Rangliste -- {today}",
        header_line,
        "",
        "| Rang | Repo | Stars |",
        "|------|------|------:|",
    ]

    for i, s in enumerate(ranked, 1):
        lines.append(f"| {i} | {s.repo} | {s.stars} |")

    if errors:
        lines += ["", "### Fehler (kein Zugriff):"]
        for s in errors:
            lines.append(f"- {s.repo}: {s.error}")

    lines += markdown_private_section(private_repos, PRIVATE_REASON)
    lines += markdown_archived_section(archived_repos)

    return "\n".join(lines)
