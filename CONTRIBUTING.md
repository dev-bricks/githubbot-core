# Beitragsrichtlinie / Contributing Guide

## Deutsch

Vielen Dank für Ihr Interesse, zu diesem Projekt beizutragen!

### Wie Sie beitragen koennen

1. **Bug melden:** Erstellen Sie ein Issue mit dem Label `bug`
2. **Feature vorschlagen:** Erstellen Sie ein Issue mit dem Label `enhancement`
3. **Code beitragen:** Erstellen Sie einen Pull Request

### Pull Requests

1. Forken Sie das Repository
2. Erstellen Sie einen Feature-Branch: `git checkout -b feature/mein-feature`
3. Committen Sie Ihre Änderungen: `git commit -m "Beschreibung der Änderung"`
4. Pushen Sie den Branch: `git push origin feature/mein-feature`
5. Erstellen Sie einen Pull Request

### Lizenz- und Rechtehinweise

Falls dieses Projekt ein CLA, DCO oder eine andere Beitragsvereinbarung nutzt,
ist dies im Repository gesondert dokumentiert. Ohne ausdrückliche Zusatzregel
gelten Pull Requests unter der Lizenz des Projekts.

### Code-Richtlinien

- Python: PEP 8 Stil, nur Standardbibliothek zur Laufzeit
- Encoding: UTF-8 für alle Dateien
- Sprache: Code und Kommentare auf Deutsch oder Englisch
- Keine hardcoded Pfade oder API-Keys

### Erste Schritte

```bash
pip install -e ".[dev]"
python -m pytest
ruff check .
```

Schreibende Funktionen laufen ausschließlich über `githubbot_core.actions` (Vorschau/Ausführen,
Rechteobergrenze, Belege). Neue Schreibaktionen bitte dort ergänzen, nicht direkt per API.

---

## English

Thank you for your interest in contributing to this project!

### How to Contribute

1. **Report bugs:** Create an issue with the `bug` label
2. **Suggest features:** Create an issue with the `enhancement` label
3. **Contribute code:** Create a Pull Request

### Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m "Description of change"`
4. Push the branch: `git push origin feature/my-feature`
5. Create a Pull Request

### Licensing and Contribution Terms

If this project uses a CLA, DCO, or any other contribution agreement, it
should be documented separately in the repository. Unless stated otherwise,
pull requests are understood to be submitted under the project's license.

### Code Guidelines

- Python: PEP 8 style, standard library only at runtime
- Encoding: UTF-8 for all files
- Language: Code and comments in German or English
- No hardcoded paths or API keys

### Getting Started

```bash
pip install -e ".[dev]"
python -m pytest
ruff check .
```

Every write goes through `githubbot_core.actions` (preview/apply, permission
ceiling, receipts). Please add new write actions there, not as direct API calls.
