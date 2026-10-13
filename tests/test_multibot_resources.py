"""Packaged entrypoints, authorization defaults and external launcher boundary."""

import hashlib
from importlib import resources
import json
import sys
from unittest.mock import Mock, patch

from githubbot_core import cli, multibot


def test_system_agent_extends_compatible_template_with_verified_roles(capsys):
    assert cli.main(["--json", "system-agent"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["id"] == "githubbot" and data["sha256"].startswith("2e72c000")
    assert [r["mode"] for r in data["roles"]] == list(multibot.MODES)
    prompt = resources.files("githubbot_core").joinpath("prompts/MULTI-BOT.de.md").read_bytes()
    for role in data["roles"]:
        assert role["sha256"] == cli.template_sha256(data["schema"], role["id"], role["blueprint"])
        assert role["prompt_sha256"] == hashlib.sha256(prompt).hexdigest()
        assert role["blueprint"]["governance"]["profile"] == "read_only_research"


def test_packaged_resources_are_neutral_and_ascii_starters():
    base = resources.files("githubbot_core")
    paths = [
        "starters/windows/MULTI-BOT.bat",
        "starters/posix/start-multi-bot.sh",
        *[f"starters/windows/providers/MULTI-BOT-{p}.bat" for p in ("CLAUDE", "CODEX", "AGY", "KIMI")],
    ]
    for path in paths:
        text = base.joinpath(path).read_text(encoding="ascii")
        assert "githubbot_core multi-bot" in text
        assert "TRUSTED_AUTOMATION=1" not in text and "OneDrive" not in text
    prompt = base.joinpath("prompts/MULTI-BOT.de.md").read_text(encoding="utf-8")
    assert "ellmos-multi-bot" not in prompt and "lukisch" not in prompt and "C:\\Users" not in prompt


def test_empty_instance_launch_has_no_trusted_automation_or_identity(monkeypatch, tmp_path):
    monkeypatch.delenv("TASKPLAN_TRUSTED_AUTOMATION", raising=False)
    launch = Mock(return_value=0)
    assert multibot.main(["--root", str(tmp_path), "--mode", "1", "--no-interactive", "--dry-run"], launch=launch) == 0
    args = launch.call_args.kwargs
    assert args["label"] == "multi-bot" and args["provider"] == ""
    assert args["env"]["TASKPLAN_WORKDIR"] == str(tmp_path.resolve())
    assert "TASKPLAN_TRUSTED_AUTOMATION" not in args["env"]
    assert args["env"]["TASKPLAN_STARTER_DRY_RUN"] == "1"
    assert "Merge enabled: false" in args["request"] and "merge account: none" in args["request"]


def test_instance_config_can_explicitly_opt_in(tmp_path):
    prompt = tmp_path / "instance.md"
    prompt.write_text("Authorized local rules", encoding="utf-8")
    config = tmp_path / "instance.json"
    config.write_text(
        json.dumps(
            {
                "multi_bot": {
                    "root": str(tmp_path),
                    "instance_prompt": str(prompt),
                    "merge_account": "example-reviewer",
                    "merge_enabled": True,
                    "trusted_automation": True,
                }
            }
        )
    )
    launch = Mock(return_value=0)
    assert multibot.main(["--config", str(config), "--mode", "2", "--no-interactive"], launch=launch) == 0
    args = launch.call_args.kwargs
    assert args["env"]["TASKPLAN_TRUSTED_AUTOMATION"] == "1"
    assert "example-reviewer" in args["request"] and "Merge enabled: true" in args["request"]
    assert str(prompt) in args["request"]


def test_missing_optional_launcher_is_explicit_and_fails_closed(capsys, tmp_path):
    with patch.dict(sys.modules, {"taskplan": None, "taskplan.launcher": None}):
        assert cli.main(["multi-bot", "--root", str(tmp_path), "--mode", "triage", "--no-interactive"]) == 1
    assert "pip install taskplan" in capsys.readouterr().err


def test_stop_file_is_checked_without_optional_launcher(tmp_path):
    (tmp_path / "STOP.MULTI-BOT").touch()
    with patch.dict(sys.modules, {"taskplan": None, "taskplan.launcher": None}):
        assert multibot.main(["--root", str(tmp_path), "--no-interactive"]) == 0


def test_missing_private_data_helpers_fail_without_writes(tmp_path, capsys):
    assert cli.main(["multi-bot-care", "--root", str(tmp_path), "next"]) == 1
    assert cli.main(["multi-bot-probe", "--root", str(tmp_path)]) == 1
    assert not list(tmp_path.iterdir())
    capsys.readouterr()
