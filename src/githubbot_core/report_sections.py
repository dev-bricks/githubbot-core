"""
githubbot-core -- gemeinsame Berichtssektionen
==========================================
Bausteine, die Traffic- und Sterne-Bericht identisch brauchen: die Sektion
"Diese Module sind privat", die Archiviert-Sektion und der Zeilenumbruch fuer
lange Namenslisten -- je einmal in Markdown und im 80-Zeichen-ASCII-Stil.

Warum getrennt vom Bericht selbst: Beide Berichte zeigen dieselbe Aussage --
private Repos stehen nicht in den Zahlen, sondern hier. Lebte der Baustein
zweimal, driften die Formulierungen auseinander, und der Leser haelt einen
Unterschied fuer eine Aussage.

Der einzige echte Unterschied ist die Begruendung, warum die privaten Module
nicht in den Zahlen stehen -- deshalb ist sie ein Parameter.

"""

from __future__ import annotations

PRIVATE_HEADING_MD = "### Diese Module sind privat:"
PRIVATE_HEADING_ASCII = "PRIVATE MODULE (nicht in den Zahlen)"
ARCHIVED_HEADING_MD = "### Archiviert:"
ARCHIVED_HEADING_ASCII = "ARCHIVIERT"

AUDIT_HINT = (
    "Die Auflistung dient dem Sichtbarkeits-Audit: so lässt sich prüfen, ob "
    "wirklich die richtigen Module privat sind und welche veröffentlicht "
    "werden sollten."
)

# Warum die privaten Module in diesem Bericht nicht in den Zahlen stehen.
REASON_TRAFFIC = (
    "Private Repos liefern über die Traffic-API systematisch keine Werte "
    "(0 Views/0 Clones) und stehen deshalb nicht in den Zahlen oben. "
)
REASON_STARS = (
    "Private Repos werden hier bewusst separat aufgeführt, unabhängig von "
    "ihrer Sternenzahl. "
)


def group_repos_by_org(repo_full_names: list[str]) -> dict[str, list[str]]:
    """
    Gruppiert 'org/repo'-Strings nach Organisation.

    Orgs UND Repo-Namen (ohne Org-Praefix) werden alphabetisch sortiert
    zurueckgegeben -- fuer die Privat-/Archiviert-Sektionen der Berichte.
    """
    groups: dict[str, list[str]] = {}
    for full in repo_full_names:
        org, sep, name = full.partition("/")
        if not sep:
            org, name = "", full
        groups.setdefault(org, []).append(name)
    return {org: sorted(names) for org, names in sorted(groups.items())}


def wrap_repo_names(names: list[str], prefix: str, width: int = 80,
                    indent: str = "    ") -> list[str]:
    """
    Bricht eine kommaseparierte Namensliste auf mehrere Zeilen um.

    Umgebrochene Zeilen enden auf einem Komma -- ohne es liest sich die
    Fortsetzung wie eine neue Aufzaehlung statt wie dieselbe Liste.
    Deshalb bleibt beim Anhaengen ein Zeichen Reserve: keine Zeile
    ueberschreitet `width`, auch nicht mit dem angehaengten Komma. Ausnahme:
    ein einzelner Name ist laenger als `width` -- dann steht er allein auf
    seiner Zeile und darf ueberstehen.

    `prefix` steht am Anfang der ersten Zeile (z. B. "  example-org (11): ").
    """
    lines: list[str] = []
    current = prefix
    line_has_item = False
    for name in names:
        piece = name if not line_has_item else f", {name}"
        if line_has_item and len(current) + len(piece) > width - 1:
            lines.append(current + ",")
            current = indent + name
            continue
        current += piece
        line_has_item = True
    lines.append(current)
    return lines


def markdown_private_section(private_repos: list[str] | None, reason: str) -> list[str]:
    """Markdown-Sektion 'Diese Module sind privat'. Leere Liste = keine Sektion."""
    if not private_repos:
        return []
    lines = [PRIVATE_HEADING_MD, ""]
    for org, names in group_repos_by_org(private_repos).items():
        lines.append(f"- **{org}** ({len(names)}): {', '.join(names)}")
    lines += ["", reason + AUDIT_HINT, ""]
    return lines


def markdown_archived_section(archived_repos: list[str] | None) -> list[str]:
    """Markdown-Sektion 'Archiviert'. Leere Liste = keine Sektion."""
    if not archived_repos:
        return []
    lines = [ARCHIVED_HEADING_MD, ""]
    for org, names in group_repos_by_org(archived_repos).items():
        lines.append(f"- **{org}** ({len(names)}): {', '.join(names)}")
    lines.append("")
    return lines


def ascii_group_block(heading: str, repo_full_names: list[str] | None,
                      width: int = 80) -> list[str]:
    """Blockabschnitt (Privat/Archiviert) im ASCII-Stil der uebrigen Grafiken."""
    if not repo_full_names:
        return []
    block = ["", f"  {heading}", "  " + "─" * (width - 2)]
    for org, names in group_repos_by_org(repo_full_names).items():
        block.extend(wrap_repo_names(names, f"  {org} ({len(names)}): ", width=width))
    return block
