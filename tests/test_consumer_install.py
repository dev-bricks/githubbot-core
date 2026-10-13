"""Consumer test: install the package into a fresh virtual environment and use the CLI.

Slow (creates a venv and builds the wheel), therefore opt-in:
    GITHUBBOT_CORE_CONSUMER_TEST=1 python -m pytest tests/test_consumer_install.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=env, **kwargs)


def test_installed_package_cli_without_private_extension(tmp_path: Path) -> None:
    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(env_dir)
    bindir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    python = bindir / ("python.exe" if sys.platform == "win32" else "python")
    install = _run([str(python), "-m", "pip", "install", "-q", str(ROOT)], cwd=tmp_path, timeout=600)
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
    role_descriptor = _run([str(cli), "--json", "multi-bot-role", "--mode", "aftercare"], cwd=work)
    assert role_descriptor.returncode == 0
    assert json.loads(role_descriptor.stdout)["data"]["id"] == "multi-bot-aftercare"
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
