"""
githubbot-core -- Auto-Kommentare
==============================
Verwaltet und rendert automatische Kommentare fuer GitHub-Issues und PRs.

Kommentar-Typen:
  - needs_info_bug     : Bug ohne Reproduktionsschritte
  - question_wiki_hint : Verweis auf Wiki/Docs bei Fragen
  - duplicate_reference: Verweis auf Original bei Duplikaten
  - security_private   : Umleitung zu privatem Reporting bei Security-Issues

Templates kommen aus dem Config-Schluessel ``auto_comments`` (Default oder
eigene Konfigurationsschicht).

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class CommentTemplate:
    """Ein einzelnes Kommentar-Template."""
    key: str
    trigger: str
    template: str

    def render(self, **kwargs) -> str:
        """
        Rendert das Template mit den gegebenen Variablen.

        Unterstuetzte Variablen (je nach Template):
          - {original_issue}: Nummer des Original-Issues (bei duplicate_reference)
          - {wiki_url}: URL zum Wiki (bei question_wiki_hint)
          - Weitere koennen jederzeit hinzugefuegt werden
        """
        try:
            return self.template.format(**kwargs)
        except KeyError:
            return self.template  # Fallback: Template unveraendert zurueckgeben


# ============================================================
# COMMENT MANAGER
# ============================================================

class AutoCommentManager:
    """
    Verwaltet Kommentar-Templates und verhindert Doppel-Kommentare.

    Verwendet:
        manager = AutoCommentManager(config)
        comment = manager.get_comment("needs_info_bug")
        if comment:
            gh_client.add_comment(issue_number, comment)
    """

    def __init__(self, config: dict):
        self.config = config
        self.templates = self._load_templates(config.get("auto_comments", {}))

    def _load_templates(self, comments_config: dict) -> dict[str, CommentTemplate]:
        """Ladet Templates aus Config."""
        templates = {}
        for key, data in comments_config.items():
            templates[key] = CommentTemplate(
                key=key,
                trigger=data.get("trigger", key),
                template=data.get("template", ""),
            )
        return templates

    def get_comment(self, key: str, **kwargs) -> Optional[str]:
        """
        Gibt gerenderten Kommentar-Text zurueck.

        Args:
            key: Template-Key (z.B. "needs_info_bug")
            **kwargs: Template-Variablen (z.B. original_issue=42)

        Returns:
            Gerenderten Kommentar-Text oder None wenn Template nicht gefunden
        """
        template = self.templates.get(key)
        if not template:
            return None
        return template.render(**kwargs)

    def has_template(self, key: str) -> bool:
        """Prueft ob ein Template fuer den gegebenen Key existiert."""
        return key in self.templates

    def list_templates(self) -> list[str]:
        """Gibt alle verfuegbaren Template-Keys zurueck."""
        return list(self.templates.keys())

    def check_already_commented(
        self,
        issue_number: int,
        comment_type: str,
        existing_comments: list[dict],
    ) -> bool:
        """
        Prueft ob der Bot bereits einen Kommentar dieses Typs hinterlassen hat.

        Verhindert Duplikat-Kommentare bei mehrmaligem Ausfuehren der Triage.
        Bot-Kommentare werden anhand eines HTML-Markers erkannt
        (z.B. "<!-- githubbot:needs_info_bug -->").
        """
        marker = f"<!-- githubbot:{comment_type} -->"
        for comment in existing_comments:
            if marker in comment.get("body", ""):
                return True
        return False

    def add_marker(self, text: str, comment_type: str) -> str:
        """
        Fuegt unsichtbaren Marker zum Kommentar hinzu.

        Damit kann check_already_commented() erkennen ob dieser Typ
        bereits gepostet wurde.
        """
        marker = f"\n<!-- githubbot:{comment_type} -->"
        return text + marker
