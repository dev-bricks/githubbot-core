"""
githubbot-core -- Duplikat-Erkennung
=================================
Erkennt aehnliche GitHub-Issues mit einem Text-Fingerprint-Verfahren.

Methode: Jaccard-Aehnlichkeit auf Wort-Tokens (einfach, keine Dependencies).
Schwelle: >80% Aehnlichkeit = Duplikat-Kandidat.

Fuer grosse Repos: Erweiterbar mit TF-IDF oder sentence-transformers.

"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================
# HELPER
# ============================================================


def tokenize(text: str) -> set[str]:
    """
    Normalisiert und tokenisiert Text fuer Aehnlichkeitsvergleich.

    - Lowercase
    - Nur alphanumerische Tokens
    - Stopwoerter entfernen (einfache Liste)
    """
    STOPWORDS = {
        "the",
        "a",
        "an",
        "is",
        "it",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "and",
        "or",
        "not",
        "are",
        "was",
        "has",
        "have",
        "i",
        "my",
        "me",
        "ich",
        "mein",
        "das",
        "die",
        "der",
        "und",
        "oder",
        "nicht",
        "ein",
        "eine",
        "beim",
        "ist",
        "im",
        "von",
    }
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return tokens - STOPWORDS


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """Berechnet Jaccard-Aehnlichkeit zwischen zwei Token-Sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union


# ============================================================
# DATA CLASSES
# ============================================================


@dataclass
class DuplicateCandidate:
    """Ein moegliches Duplikat-Paar."""

    issue_number: int
    issue_title: str
    original_number: int
    original_title: str
    similarity: float

    @property
    def is_duplicate(self) -> bool:
        return self.similarity >= 0.80

    def __str__(self) -> str:
        pct = int(self.similarity * 100)
        return (
            f"#{self.issue_number} '{self.issue_title[:40]}' "
            f"<-> #{self.original_number} '{self.original_title[:40]}' "
            f"({pct}% Aehnlichkeit)"
        )


# ============================================================
# DETECTOR
# ============================================================


class DuplicateDetector:
    """
    Erkennt Duplikat-Issues via Jaccard-Aehnlichkeit auf Token-Level.

    Schwelle konfigurierbar (default: 0.80 = 80%).

    Verwendung:
        detector = DuplicateDetector(threshold=0.80)
        candidates = detector.find_duplicates(issues)
        for c in candidates:
            if c.is_duplicate:
                print(c)
    """

    def __init__(self, threshold: float = 0.80):
        self.threshold = threshold

    def fingerprint(self, issue_number: int, title: str, body: str) -> dict:
        """
        Erstellt Text-Fingerprint eines Issues.

        Returns:
            Dict mit issue_number, title, tokens (fuer Vergleich)
        """
        # Titel hat hoehere Gewichtung (doppelt zaehlen)
        title_tokens = tokenize(title)
        body_tokens = tokenize(body[:500])  # Nur erste 500 Zeichen des Bodys
        combined = title_tokens | title_tokens | body_tokens  # Titel doppelt

        return {
            "number": issue_number,
            "title": title,
            "tokens": combined,
        }

    def find_duplicates(self, issues: list) -> list[DuplicateCandidate]:
        """
        Sucht Duplikat-Kandidaten in einer Liste von Issues.

        Args:
            issues: Liste von GitHubIssue-Objekten (oder Dicts mit number/title/body)

        Returns:
            Liste von DuplicateCandidate (sortiert nach Aehnlichkeit absteigend)

        Komplexitaet: O(n^2) -- fuer grosse Repos (>1000 Issues) optimieren!
        TODO: Fuer grosse Repos: LSH (Locality-Sensitive Hashing) nutzen
        TODO: Oder: Nur neue Issues gegen bestehende pruefen (nicht alle gegen alle)
        """
        fingerprints = []

        for issue in issues:
            if hasattr(issue, "number"):
                # GitHubIssue-Objekt
                fp = self.fingerprint(issue.number, issue.title, issue.body)
            else:
                # Dict-Interface
                fp = self.fingerprint(issue["number"], issue.get("title", ""), issue.get("body", ""))
            fingerprints.append(fp)

        candidates = []

        for i, fp_a in enumerate(fingerprints):
            for j, fp_b in enumerate(fingerprints):
                if j <= i:
                    continue  # Jedes Paar nur einmal pruefen

                sim = jaccard_similarity(fp_a["tokens"], fp_b["tokens"])

                if sim >= self.threshold:
                    candidate = DuplicateCandidate(
                        issue_number=fp_b["number"],
                        issue_title=fp_b["title"],
                        original_number=fp_a["number"],
                        original_title=fp_a["title"],
                        similarity=sim,
                    )
                    candidates.append(candidate)

        # Sortiert nach Aehnlichkeit (hoechste zuerst)
        return sorted(candidates, key=lambda c: c.similarity, reverse=True)

    def find_for_new_issue(
        self,
        new_issue_number: int,
        new_title: str,
        new_body: str,
        existing_issues: list,
    ) -> list[DuplicateCandidate]:
        """
        Prueft ein einzelnes neues Issue gegen alle bestehenden.

        Effizienter als find_duplicates() wenn nur ein neues Issue geprueft werden soll.

        """
        fp_new = self.fingerprint(new_issue_number, new_title, new_body)
        candidates = []

        for issue in existing_issues:
            if hasattr(issue, "number"):
                if issue.number == new_issue_number:
                    continue
                fp_existing = self.fingerprint(issue.number, issue.title, issue.body)
            else:
                if issue["number"] == new_issue_number:
                    continue
                fp_existing = self.fingerprint(issue["number"], issue.get("title", ""), issue.get("body", ""))

            sim = jaccard_similarity(fp_new["tokens"], fp_existing["tokens"])
            if sim >= self.threshold:
                candidates.append(
                    DuplicateCandidate(
                        issue_number=new_issue_number,
                        issue_title=new_title,
                        original_number=fp_existing["number"],
                        original_title=fp_existing["title"],
                        similarity=sim,
                    )
                )

        return sorted(candidates, key=lambda c: c.similarity, reverse=True)
