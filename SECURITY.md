# Sicherheitsrichtlinie / Security Policy

## Deutsch

### Sicherheitslücken melden

1. **Kein öffentliches Issue eröffnen.**
2. **GitHub Private Vulnerability Reporting verwenden:** `Security` → `Advisories` → `New`.
3. Beschreibung, Reproduktionsschritte, betroffene Version und mögliche Auswirkungen angeben.

Falls Private Vulnerability Reporting noch nicht aktiviert ist, kontaktieren Sie die Maintainer
über GitHub und veröffentlichen Sie keine Details in einem öffentlichen Issue.

### Geltungsbereich

- Schreibaktionen gegen GitHub (Vorschau/Ausführen, Rechteobergrenzen, Belege)
- Umgang mit Zugangsdaten (Token-Auflösung, Schwärzung)
- Review-Gate: Signaturprüfung, Exact-SHA-Bindung, Workflow mit `pull_request_target`
- Plugin-Schnittstelle (Laden, Rechte beim Planen)

### Sicherheitsprinzipien

- Ohne `--apply` wird nichts geschrieben; jede Schreibaktion wird gegen die konfigurierte
  Rechteobergrenze geprüft, die spätere Konfigurationsschichten nur einengen können.
- Tokens werden zur Laufzeit gelesen (`GH_TOKEN`, `GITHUB_TOKEN`, `gh auth token`) und weder
  gespeichert noch mitgeliefert.
- Der Review-Gate-Workflow führt ausschließlich den Prüfer vom Default-Branch aus und checkt
  keinen PR-Code aus.

### Reaktionszeit

Dies ist ein kleines Projekt; Reaktionszeiten können variieren. Kritische Meldungen werden
priorisiert. Bitte lassen Sie vor einer Veröffentlichung angemessen Zeit.

---

## English

### Reporting a Vulnerability

1. **Do not open a public issue.**
2. **Use GitHub Private Vulnerability Reporting:** `Security` → `Advisories` → `New`.
3. Include a description, reproduction steps, the affected version and the potential impact.

If private vulnerability reporting is not enabled yet, contact the maintainers through GitHub and
do not publish details in a public issue.

### Scope

- Writes to GitHub (preview/apply, permission ceilings, receipts)
- Credential handling (token lookup, redaction)
- Review gate: signature verification, exact-SHA binding, `pull_request_target` workflow
- Plugin interface (loading, permissions during planning)

### Security Principles

- Nothing is written without `--apply`; every write is checked against the configured permission
  ceiling, which later configuration layers can only narrow.
- Tokens are read at runtime (`GH_TOKEN`, `GITHUB_TOKEN`, `gh auth token`) and are neither stored
  nor bundled.
- The review-gate workflow only runs the verifier from the default branch and never checks out
  pull-request code.

### Response Time

This is a small project; response times may vary. Critical reports are prioritised. Please allow
reasonable time before public disclosure.
