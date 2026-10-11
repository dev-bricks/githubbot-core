# Changelog / Änderungsprotokoll

Alle wesentlichen Änderungen an diesem Projekt werden hier dokumentiert.
All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Geändert / Changed

- Gesamter Baum mit `ruff format` formatiert (keine Codeänderung, AST identisch); CI prüft jetzt
  zusätzlich `ruff format --check` / whole tree formatted with `ruff format` (no code change, AST
  identical); CI now also runs `ruff format --check`.
- README (EN/DE) mit Banner (`assets/banner.png`, Quelle `assets/banner.svg`); Testkommentar
  neutral formuliert / README banner added; a test comment worded neutrally.

## [0.2.1] - 2026-10-11

### Geändert / Changed

- `system-agent`: `sha256` bezieht sich auf schema+id+blueprint und ist für Konsumenten nachrechenbar /
  sha256 scope: schema+id+blueprint, verifiable by consumers (canonical JSON, sorted keys, compact
  separators, UTF-8, `ensure_ascii=False`; excludes version, capability, permissions, `_meta`).
  The hash value of the unchanged v0.2.0 blueprint changes from the old whole-file hash.

## [0.2.0] - 2026-10-11

### Hinzugefügt / Added

- Befehl `system-agent`: versionierte, gehashte Systemagent-Vorlage (`githubbot-core.system-agent/v1`)
  mit Agent-Studio-Blueprint-Feldern, nur bei installiertem Paket vorhanden /
  `system-agent` command: versioned, hashed system agent template with agent-studio blueprint fields,
  present only where the package is installed.

## [0.1.0] - 2026-10-11

### Hinzugefügt / Added

- Installierbares Paket `githubbot-core` mit CLI `githubbot-core` und JSON-Ergebnishülle
  `githubbot-core.result/v1` / installable package with CLI and JSON result envelope.
- Issue-Triage, Duplikaterkennung, PR-Zusammenfassung, Health-/Traffic-/Sterne-Berichte /
  issue triage, duplicate detection, PR summary, health/traffic/star reports.
- Vorschau/Ausführen-Vertrag mit Rechteobergrenze, Belegen und Idempotenz /
  preview/apply contract with permission ceiling, receipts and idempotency.
- Geschichtete Konfiguration, Obergrenzen nur einengbar / layered configuration, ceilings narrow-only.
- Plugin-SPI v1 (Entry-Point-Gruppe `githubbot_core.plugins`) / plugin SPI v1.
- Credential-Adapter (Umgebung, `gh auth token`) mit Schwärzung / credential adapter with redaction.
- Namensraumunabhängiges Exact-SHA-Review-Gate samt Workflow-Scaffold /
  namespace-independent exact-SHA review gate with workflow scaffold.
