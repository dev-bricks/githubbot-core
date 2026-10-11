"""System agent template: versioned, hashed, shaped like the agent-studio blueprint payload."""
from __future__ import annotations

import hashlib
import json
import re
from importlib import resources

from githubbot_core import __version__, cli

# Rules of the consuming agent-studio save/materialize path (blueprint service).
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
KINDS = {"agent", "role", "skill", "workflow", "service", "contractus"}
ANIMUS = {"api", "cli", "subscription", "local"}
EXECUTABLE_MODUS = {"casualis", "usus"}


def _system_agent(capsys, *extra: str) -> dict:
    assert cli.main([*extra, "--json", "system-agent"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True and envelope["mode"] == "read"
    return envelope["data"]


def test_template_identity_is_version_plus_canonical_hash(capsys):
    data = _system_agent(capsys)
    template = json.loads(resources.files("githubbot_core").joinpath("system_agent.json").read_text(encoding="utf-8"))
    canonical = json.dumps(template, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert data["schema"] == "githubbot-core.system-agent/v1"
    assert data["id"] == "githubbot" and data["version"] == __version__
    assert data["sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert data["capability"] == {"package": "githubbot-core", "version": __version__, "spi_version": 1}


def test_blueprint_is_accepted_by_the_agent_studio_payload_rules(capsys):
    bp = _system_agent(capsys)["blueprint"]
    assert NAME.fullmatch(bp["name"]) and bp["name"] == bp["name"].lower()
    assert bp["kind"] in KINDS and bp["kind"] == "agent"
    assert bp["persona_prompt"].strip() and len(bp["title"]) <= 120
    assert bp["animus_type"] in ANIMUS and bp["modus"] in EXECUTABLE_MODUS
    assert isinstance(bp["skills"], list) and all(NAME.fullmatch(s) for s in bp["skills"])
    assert isinstance(bp["contractus"], dict) and isinstance(bp["governance"], dict)
    assert "is_template" not in bp  # system templates are registered by the consumer, never self-declared


def test_permissions_are_the_effective_narrowed_configuration(capsys, tmp_path):
    layer = tmp_path / "narrow.json"
    layer.write_text(json.dumps({"permissions": {"enabled": []}}), encoding="utf-8")
    assert _system_agent(capsys)["permissions"]["enabled"] == ["issue.add_labels", "issue.comment"]
    assert _system_agent(capsys, "--config", str(layer))["permissions"]["enabled"] == []
