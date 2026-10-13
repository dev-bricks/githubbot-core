"""Packaged entrypoints, authorization defaults and external launcher boundary."""

from importlib import resources
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from githubbot_core import cli, multibot
from githubbot_core.config import load_config


def test_system_agent_extends_compatible_template_with_verified_roles(capsys):
    assert cli.main(["--json", "system-agent"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["id"] == "githubbot" and data["sha256"].startswith("2e72c000")
    descriptor_fields = {"schema", "id", "version", "sha256", "capability", "blueprint", "permissions"}
    assert set(data) == descriptor_fields
    resource = json.loads(resources.files("githubbot_core").joinpath("multibot_roles.json").read_text(encoding="utf-8"))
    assert [r["mode"] for r in resource["roles"]] == list(multibot.ALL_MODES)
    for mode in multibot.ALL_MODES:
        assert cli.main(["--json", "multi-bot-role", "--mode", mode]) == 0
        role = json.loads(capsys.readouterr().out)["data"]
        assert set(role) == descriptor_fields
        assert role["sha256"] == cli.template_sha256(role["schema"], role["id"], role["blueprint"])
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


def test_actual_policy_mutation_changes_all_role_hashes(tmp_path, capsys):
    package = resources.files("githubbot_core")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "multibot_roles.json").write_bytes(package.joinpath("multibot_roles.json").read_bytes())
    policy = tmp_path / "prompts/MULTI-BOT.de.md"
    policy.write_bytes(package.joinpath("prompts/MULTI-BOT.de.md").read_bytes())

    # load_config needs the packaged default while the role fixture intentionally replaces only the role resources.
    config = load_config()

    def role_hashes():
        result = {}
        with patch.object(cli.resources, "files", return_value=tmp_path):
            for mode in multibot.ALL_MODES:
                cli.cmd_multibot_role(SimpleNamespace(mode=mode, json=True), config, None)
                result[mode] = json.loads(capsys.readouterr().out)["data"]["sha256"]
        return result

    before = role_hashes()
    policy.write_bytes(policy.read_bytes() + b"\nActual changed safety policy.\n")
    after = role_hashes()
    assert all(before[mode] != after[mode] for mode in multibot.ALL_MODES)


def test_launched_persona_is_exactly_the_hashed_descriptor(tmp_path, capsys):
    cli.main(["--json", "multi-bot-role", "--mode", "aftercare"])
    descriptor = json.loads(capsys.readouterr().out)["data"]

    def launch(_role, **kwargs):
        from pathlib import Path

        assert Path(kwargs["prompt_file"]).read_text(encoding="utf-8") == descriptor["blueprint"]["persona_prompt"]
        assert descriptor["sha256"] in kwargs["request"]
        return 0

    assert multibot.main(["--root", str(tmp_path), "--mode", "aftercare", "--no-interactive"], launch=launch) == 0


def test_readonly_ceiling_and_config_layers_reach_the_run_contract(tmp_path):
    path = tmp_path / "readonly.json"
    path.write_text(
        json.dumps(
            {
                "permissions": {"ceiling": [], "enabled": [], "api_write_prefixes": []},
                "credentials": {"token": "must-not-copy-secret"},
            }
        )
    )
    default, readonly = Mock(return_value=0), Mock(return_value=0)
    args = ["--root", str(tmp_path), "--mode", "aftercare", "--no-interactive"]
    assert multibot.main(args, launch=default) == 0
    assert multibot.main(["--config", str(path), *args], launch=readonly) == 0
    request = readonly.call_args.kwargs["request"]
    assert request != default.call_args.kwargs["request"] and str(path.resolve()) in request
    contract = json.loads(
        request.split("AUTHORITATIVE CORE RUN CONTRACT (not credentials):\n")[1].split("\nPreserve config_layers")[0]
    )
    assert contract == {
        "permissions": {"ceiling": [], "enabled": [], "api_write_prefixes": []},
        "config_layers": [str(path.resolve())],
    }
    assert "must-not-copy-secret" not in request and "unknown write authority means HOLD" in request


def test_explicit_layer_order_is_preserved(tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_text(json.dumps({"permissions": {"enabled": ["issue.comment"]}}))
    second.write_text(json.dumps({"permissions": {"enabled": []}}))
    launch = Mock(return_value=0)
    assert (
        multibot.main(
            [
                "--config",
                str(first),
                "--config",
                str(second),
                "--root",
                str(tmp_path),
                "--mode",
                "triage",
                "--no-interactive",
            ],
            launch=launch,
        )
        == 0
    )
    request = launch.call_args.kwargs["request"]
    contract = json.loads(
        request.split("AUTHORITATIVE CORE RUN CONTRACT (not credentials):\n")[1].split("\nPreserve config_layers")[0]
    )
    assert contract["config_layers"] == [str(first.resolve()), str(second.resolve())]
    assert contract["permissions"]["enabled"] == []
