"""Consumer test: install the package into a fresh virtual environment and use the CLI.

Slow (creates a venv and builds the wheel), therefore opt-in:
    GITHUBBOT_CORE_CONSUMER_TEST=1 python -m pytest tests/test_consumer_install.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("GITHUBBOT_CORE_CONSUMER_TEST") != "1", reason="set GITHUBBOT_CORE_CONSUMER_TEST=1"
)


def _run(cmd, cwd, **kwargs):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        **kwargs,
    )


@pytest.mark.parametrize("artifact_variable", ["GITHUBBOT_CORE_TEST_WHEEL", "GITHUBBOT_CORE_TEST_SDIST"])
def test_installed_package_cli_without_private_extension(tmp_path: Path, artifact_variable: str) -> None:
    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(env_dir)
    bindir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    python = bindir / ("python.exe" if sys.platform == "win32" else "python")
    artifact = os.environ.get(artifact_variable) or str(ROOT)
    install = _run([str(python), "-m", "pip", "install", "-q", artifact], cwd=tmp_path, timeout=600)
    assert install.returncode == 0, install.stderr

    cli = bindir / ("githubbot-core.exe" if sys.platform == "win32" else "githubbot-core")
    work = tmp_path / "work"
    work.mkdir()

    info = _run([str(cli), "--json", "info"], cwd=work)
    assert info.returncode == 0, info.stderr
    envelope = json.loads(info.stdout)
    assert envelope["ok"] is True and envelope["data"]["plugins"] == []

    agent = _run([str(cli), "--json", "system-agent"], cwd=work)  # template ships as package data
    assert agent.returncode == 0, agent.stderr
    assert json.loads(agent.stdout)["data"]["blueprint"]["name"] == "githubbot"
    assert "roles" not in json.loads(agent.stdout)["data"]
    import hashlib

    installed = _run([str(python), "-c", "import githubbot_core; print(githubbot_core.__file__)"], cwd=work)
    assert installed.returncode == 0 and "site-packages" in installed.stdout
    package_root = Path(installed.stdout.strip()).parent
    policy_bytes = (package_root / "prompts/MULTI-BOT.de.md").read_bytes()
    policy_sha = hashlib.sha256(policy_bytes).hexdigest()

    for mode in ("operator", "bot-triage", "github-maintain", "triage", "review-merge", "aftercare"):
        role_descriptor = _run([str(cli), "--json", "multi-bot-role", "--mode", mode], cwd=work)
        assert role_descriptor.returncode == 0
        descriptor = json.loads(role_descriptor.stdout)["data"]
        assert descriptor["id"] == "multi-bot-" + mode
        assert set(descriptor) == {"schema", "id", "version", "sha256", "capability", "blueprint", "permissions"}
        identity = {key: descriptor[key] for key in ("schema", "id", "blueprint")}
        assert (
            descriptor["sha256"]
            == hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        )
        persona = descriptor["blueprint"]["persona_prompt"]
        assert f"Packaged policy SHA256: {policy_sha}" in persona
        assert policy_bytes.decode("utf-8") in persona
        assert "BEGIN PACKAGED MULTI-BOT POLICY" in persona
    role = _run([str(cli), "multi-bot", "--no-interactive", "--dry-run"], cwd=work)
    assert role.returncode == 1 and "pip install taskplan" in role.stderr
    assert not list(work.iterdir())  # no private data or provider process created
    resource = _run(
        [
            str(python),
            "-c",
            "from importlib import resources; "
            "p=resources.files('githubbot_core'); "
            "assert p.joinpath('prompts/MULTI-BOT.de.md').is_file(); "
            "assert p.joinpath('starters/windows/MULTI-BOT.bat').is_file(); "
            "assert p.joinpath('starters/posix/start-multi-bot.sh').is_file()",
        ],
        cwd=work,
    )
    assert resource.returncode == 0, resource.stderr

    origin = _run([str(python), "-c", "import githubbot_core, sys; print(githubbot_core.__file__)"], cwd=work)
    assert "site-packages" in origin.stdout, origin.stdout  # not the source tree

    missing = _run([str(cli), "--json", "run", "private-extension"], cwd=work)
    assert missing.returncode == 3
    assert json.loads(missing.stdout)["data"] == {"error": "plugin_not_available"}

    issues = work / "issues.json"
    issues.write_text(
        json.dumps([{"number": 1, "title": "Found XSS vulnerability", "body": "", "labels": []}]), encoding="utf-8"
    )
    classify = _run([str(cli), "--json", "classify", "--input", str(issues)], cwd=work)
    assert classify.returncode == 0 and "security" in json.loads(classify.stdout)["data"][0]["labels_to_add"]

    before = sorted(p.name for p in work.iterdir())
    scaffold = _run([str(cli), "--json", "gate-scaffold", "--dest", str(work)], cwd=work)
    assert scaffold.returncode == 0 and json.loads(scaffold.stdout)["mode"] == "preview"
    assert sorted(p.name for p in work.iterdir()) == before  # preview wrote nothing

    snapshot = work / "snapshot.json"
    snapshot.write_text(
        json.dumps({"candidates": [{"id": "own/repo#7", "revision": "a" * 40, "category": "prs", "status": "ready"}]}),
        encoding="utf-8",
    )
    readonly = work / "readonly.json"
    readonly.write_text(json.dumps({"permissions": {"ceiling": [], "enabled": [], "api_write_prefixes": []}}))
    before = {p.name: p.read_bytes() for p in work.iterdir()}
    selected = _run(
        [str(cli), "--json", "--config", str(readonly), "multi-bot-select", "--input", str(snapshot)], cwd=work
    )
    assert selected.returncode == 0 and json.loads(selected.stdout)["mode"] == "read"
    assert json.loads(selected.stdout)["data"]["candidate"]["category"] == "prs"
    assert {p.name: p.read_bytes() for p in work.iterdir()} == before

    if os.name == "nt":
        # Exercise the actual installed BAT through native cmd.exe with a harmless stub.
        stub = tmp_path / "stub"
        (stub / "githubbot_core").mkdir(parents=True)
        (stub / "githubbot_core/__init__.py").write_text("", encoding="ascii")
        (stub / "githubbot_core/__main__.py").write_text("import sys; sys.exit(23)\n", encoding="ascii")
        starter = package_root / "starters/windows/MULTI-BOT.bat"
        assert starter.read_bytes().isascii()
        env = dict(os.environ, PYTHONPATH=str(stub), MULTIBOT_PYTHON=str(python))
        env.pop("MULTIBOT_NO_PAUSE", None)
        env.pop("TASKPLAN_STARTER_DRY_RUN", None)
        command = 'cmd.exe /d /s /c "' + subprocess.list2cmdline([str(starter), "--mode", "1"]) + '"'
        process = subprocess.Popen(
            command,
            cwd=stub,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            time.sleep(1)
            assert process.poll() is None
            stdout, stderr = process.communicate("\n", timeout=10)
            assert process.returncode == 23 and "foreground" in stdout and not stderr, (stdout, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
