"""Contract tests: preview/apply, permission ceilings, config layering, plugin SPI, CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from githubbot_core import actions, cli, plugins
from githubbot_core.actions import Action
from githubbot_core.config import ConfigError, default_config, merge_layers
from githubbot_core.credentials import CredentialUnavailable, EnvTokenProvider, redact, resolve_token
from githubbot_core.pr_review import PRReviewer, PullRequest
from githubbot_core.transport import ReadOnlyTransport

REPO = "example-org/example-repo"
ISSUES = [
    {"number": 1, "title": "How do I configure the output?", "body": "", "labels": [],
     "user": {"login": "someone"}, "created_at": "2026-01-01T00:00:00Z"},
    {"number": 2, "title": "Found XSS vulnerability", "body": "", "labels": [],
     "user": {"login": "someone"}, "created_at": "2026-01-01T00:00:00Z"},
    {"number": 3, "title": "a pull request", "body": "", "labels": [], "pull_request": {},
     "user": {"login": "someone"}, "created_at": "2026-01-01T00:00:00Z"},
]


def _triage_gets():
    return {
        f"repos/{REPO}/issues?state=open&per_page=100": ISSUES,
        f"repos/{REPO}/issues/1/comments?per_page=100": [],
        f"repos/{REPO}/issues/2/comments?per_page=100": [{"body": "x\n<!-- githubbot:security_private_reporting -->"}],
    }


# ---------------------------------------------------------------- preview/apply

def test_preview_writes_nothing(fake_transport, capsys):
    transport = fake_transport(_triage_gets())
    code = cli.main(["--json", "triage", "--repo", REPO], transport=transport)
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["mode"] == "preview" and out["data"]["writes"] == 0
    assert transport.writes == []
    kinds = sorted(a["kind"] for a in out["data"]["actions"])
    # issue 1: question label + docs-hint comment; issue 2: labels only (comment marker already present); PR skipped
    assert kinds == ["issue.add_labels", "issue.add_labels", "issue.comment"]
    assert out["data"]["issues"] == 2


def test_apply_writes_only_permitted_actions_and_is_idempotent(fake_transport, tmp_path, capsys):
    transport = fake_transport(_triage_gets())
    store = tmp_path / "receipts.jsonl"
    assert cli.main(["--json", "triage", "--repo", REPO, "--apply", "--receipts", str(store)], transport=transport) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["mode"] == "apply"
    assert len(transport.writes) == 3
    assert {r["status"] for r in first["data"]["receipts"]} == {"applied"}

    assert cli.main(["--json", "triage", "--repo", REPO, "--apply", "--receipts", str(store)], transport=transport) == 0
    second = json.loads(capsys.readouterr().out)
    assert len(transport.writes) == 3  # nothing repeated
    assert {r["status"] for r in second["data"]["receipts"]} == {"skipped_duplicate"}


def test_apply_refuses_changed_plan(fake_transport):
    transport = fake_transport()
    result = actions.apply([Action("issue.comment", {"repo": REPO, "number": 1, "body": "x"})], transport,
                           default_config(), expected_plan_sha256="0" * 64)
    assert result["writes"] == 0 and "mismatch" in result["error"]
    assert transport.writes == []


def test_disabled_action_is_denied_not_written(fake_transport):
    transport = fake_transport()
    plan = [Action("api.request", {"method": "PUT", "endpoint": "user/following/someone"})]
    result = actions.apply(plan, transport, default_config())
    assert result["receipts"][0]["status"] == "denied"
    assert transport.writes == []


def test_api_request_needs_matching_prefix(fake_transport):
    config = merge_layers([("default", default_config()),
                           ("profile", {"permissions": {"enabled": ["api.request"],
                                                        "api_write_prefixes": ["user/starred/"]}})])
    allowed = Action("api.request", {"method": "PUT", "endpoint": "user/starred/o/r"})
    other = Action("api.request", {"method": "PUT", "endpoint": "user/following/someone"})
    sneaky = Action("api.request", {"method": "PUT", "endpoint": "user/starred/../following/x"})
    read = Action("api.request", {"method": "GET", "endpoint": "user/starred/o/r"})
    assert actions.denial_reason(allowed, config) is None
    assert "not covered" in actions.denial_reason(other, config)
    assert "not covered" in actions.denial_reason(sneaky, config)
    assert "not a write method" in actions.denial_reason(read, config)


def test_failed_write_is_reported(fake_transport, capsys):
    transport = fake_transport(_triage_gets(), fail_writes=True)
    assert cli.main(["--json", "triage", "--repo", REPO, "--apply"], transport=transport) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and {r["status"] for r in out["data"]["receipts"]} == {"failed"}


# ---------------------------------------------------------------- config layering

def test_later_layer_cannot_widen_ceiling():
    with pytest.raises(ConfigError, match="may only be narrowed"):
        merge_layers([("default", default_config()),
                      ("profile", {"permissions": {"ceiling": ["issue.comment"], "enabled": ["issue.comment"]}}),
                      ("local", {"permissions": {"ceiling": ["issue.comment", "api.request"]}})])


def test_enabled_must_stay_inside_ceiling():
    with pytest.raises(ConfigError, match="exceeds"):
        merge_layers([("default", default_config()),
                      ("profile", {"permissions": {"ceiling": ["issue.comment"]}})])


def test_api_prefixes_narrow_only():
    base = [("default", default_config()), ("profile", {"permissions": {"api_write_prefixes": ["user/starred/"]}})]
    assert merge_layers(base)["permissions"]["api_write_prefixes"] == ["user/starred/"]
    with pytest.raises(ConfigError):
        merge_layers(base + [("local", {"permissions": {"api_write_prefixes": ["user/"]}})])


def test_bad_config_file_exit_code(tmp_path, fake_transport):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cli.main(["--config", str(bad), "info"], transport=fake_transport()) == cli.EXIT_USAGE


# ---------------------------------------------------------------- plugin SPI

class _DemoPlugin:
    name = "demo"
    spi_version = plugins.SPI_VERSION

    def describe(self):
        return {"version": "1.0", "capabilities": ["plan", "pr_policy"]}

    def plan(self, ctx):
        assert isinstance(ctx.transport, ReadOnlyTransport)
        assert ctx.transport.request("PUT", "user/starred/o/r")[1].startswith("read-only")
        return [Action("api.request", {"method": "PUT", "endpoint": "user/starred/o/r"}, reason="demo")]

    def pr_policy_checks(self, pr, files, diff):
        return [("no_todo", "TODO" not in diff, "diff contains TODO" if "TODO" in diff else "OK")]


@pytest.fixture
def demo_plugin():
    plugins.register("demo", _DemoPlugin)
    yield
    plugins.unregister("demo")


def test_missing_plugin_is_clean_not_available(fake_transport, capsys):
    code = cli.main(["--json", "run", "not-installed-extension"], transport=fake_transport())
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_PLUGIN
    assert out["ok"] is False and out["data"] == {"error": "plugin_not_available"}
    assert "not installed or not accessible" in out["errors"][0]


def test_broken_or_incompatible_plugin_is_not_available():
    def broken():
        raise ImportError("private package not accessible")

    class Old:
        name, spi_version = "old", 0

    plugins.register("broken", broken)
    plugins.register("old", Old)
    try:
        listing = {p["name"]: p for p in plugins.list_plugins()}
        assert listing["broken"]["available"] is False and "ImportError" in listing["broken"]["error"]
        assert listing["old"]["available"] is False and "SPI version" in listing["old"]["error"]
        with pytest.raises(plugins.PluginNotAvailable):
            plugins.load("broken")
    finally:
        plugins.unregister("broken")
        plugins.unregister("old")


def test_plugin_plan_preview_and_denied_apply(demo_plugin, fake_transport, capsys):
    transport = fake_transport()
    assert cli.main(["--json", "run", "demo"], transport=transport) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "preview" and out["data"]["actions"][0]["source"] == "demo"
    assert out["data"]["actions"][0]["permitted"] is False  # api.request not enabled by default
    assert cli.main(["--json", "run", "demo", "--apply"], transport=transport) == 0
    assert transport.writes == []


def test_plugin_plan_apply_when_profile_allows(demo_plugin, fake_transport, tmp_path, capsys):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"permissions": {"enabled": ["api.request"],
                                                   "api_write_prefixes": ["user/starred/"]}}), encoding="utf-8")
    transport = fake_transport()
    assert cli.main(["--config", str(profile), "--json", "run", "demo", "--apply"], transport=transport) == 0
    assert transport.writes == [("PUT", "user/starred/o/r", None)]


def test_pr_policy_checks_come_from_plugins(demo_plugin, fake_transport):
    transport = fake_transport(
        gets={f"repos/{REPO}/pulls/7/files?per_page=100": [{"filename": "a.py"}]},
        texts={f"repos/{REPO}/pulls/7": "+ TODO later"},
    )
    reviewer = PRReviewer(default_config(), REPO, transport, policy_plugins=plugins.available_plugins())
    result = reviewer.review(PullRequest(7, "t", "", "someone", additions=5))
    assert result.policy_check_results == [("demo:no_todo", False, "diff contains TODO")]
    assert result.escalate is True


def test_pr_review_without_plugins_is_size_only():
    reviewer = PRReviewer(default_config())
    assert reviewer.review(PullRequest(1, "small", "", "a", additions=10)).escalate is False
    big = reviewer.review(PullRequest(2, "big", "", "a", additions=150, deletions=100, changed_files=3))
    assert big.escalate is True and "250 Zeilen" in big.escalate_reason


# ---------------------------------------------------------------- credentials

def test_credentials_resolve_from_env_and_redact():
    token, provider = resolve_token([EnvTokenProvider(environ={"GITHUB_TOKEN": "tok-123"})])
    assert (token, provider) == ("tok-123", "env")
    assert redact("Bearer tok-123 used", token) == "Bearer *** used"
    with pytest.raises(CredentialUnavailable):
        resolve_token([EnvTokenProvider(environ={})])


# ---------------------------------------------------------------- offline CLI + scaffold

def test_offline_classify_and_duplicates(tmp_path, capsys, fake_transport):
    data = tmp_path / "issues.json"
    data.write_text(json.dumps([
        {"number": 1, "title": "Crash on save in editor", "body": "", "labels": []},
        {"number": 2, "title": "Crash on save in editor", "body": "", "labels": []},
    ]), encoding="utf-8")
    assert cli.main(["--json", "duplicates", "--input", str(data)], transport=fake_transport()) == 0
    assert json.loads(capsys.readouterr().out)["data"][0]["original"] == 1
    assert cli.main(["--json", "classify", "--input", str(data)], transport=fake_transport()) == 0
    assert "bug" in json.loads(capsys.readouterr().out)["data"][0]["labels_to_add"]


def test_gate_scaffold_preview_then_apply(tmp_path, capsys, fake_transport):
    dest = tmp_path / "target"
    assert cli.main(["--json", "gate-scaffold", "--dest", str(dest), "--namespace", "acme"], transport=fake_transport()) == 0
    assert not dest.exists()  # preview wrote nothing
    capsys.readouterr()
    assert cli.main(["gate-scaffold", "--dest", str(dest), "--namespace", "acme", "--apply"],
                    transport=fake_transport()) == 0
    workflow = (dest / ".github/workflows/review-gate.yml").read_text(encoding="utf-8")
    assert "types: [acme-review-gate]" in workflow and "__GATE_NAMESPACE__" not in workflow
    assert (dest / ".github/review-gate/review_gate.py").is_file()
    assert cli.main(["gate-scaffold", "--dest", str(dest), "--apply"], transport=fake_transport()) == cli.EXIT_USAGE


def test_review_gate_subcommand_passthrough(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["review-gate", "--help"])
    assert exc.value.code == 0
    assert "--namespace" in capsys.readouterr().out


def test_info_json_envelope(capsys, fake_transport):
    assert cli.main(["--json", "info"], transport=fake_transport()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == cli.RESULT_SCHEMA and out["data"]["spi_version"] == plugins.SPI_VERSION


def test_package_data_is_shipped():
    package = Path(cli.__file__).parent
    assert (package / "default_config.json").is_file()
    assert (package / "templates" / "review-gate.yml").is_file()
