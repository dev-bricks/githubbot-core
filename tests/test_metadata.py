"""Repository metadata: required root documents, license and packaging rules."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "README.md",
    "README_de.md",
    "LICENSE",
    ".gitignore",
    "CHANGELOG.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "THIRD_PARTY_LICENSES.md",
    "pyproject.toml",
]


def test_required_root_documents_exist():
    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    assert missing == []


def test_license_is_mit_and_referenced():
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    for readme in ("README.md", "README_de.md"):
        assert "[MIT](LICENSE)" in (ROOT / readme).read_text(encoding="utf-8")


def test_pyproject_uses_pep639_without_license_classifier():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in text
    assert "License ::" not in text
    if tomllib:
        data = tomllib.loads(text)
        assert data["project"]["dependencies"] == []
        assert data["project"]["scripts"]["githubbot-core"] == "githubbot_core.cli:main"


def test_bilingual_policy_documents():
    for name in ("SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "## Deutsch" in text and "## English" in text, name


def test_security_policy_uses_private_reporting_not_email():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "Private Vulnerability Reporting" in text
    assert "@" not in text
