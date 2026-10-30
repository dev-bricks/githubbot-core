"""Native cmd.exe verifies foreground pause without opening a window or provider."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows batch parser")
STARTER = Path(__file__).resolve().parents[1] / "src/githubbot_core/starters/windows/MULTI-BOT.bat"


def env_for(tmp_path, code=23):
    package = tmp_path / "githubbot_core"
    package.mkdir()
    # A namespace package would lose to an installed regular githubbot_core and
    # accidentally exercise the real CLI instead of this isolated no-provider stub.
    (package / "__init__.py").write_text("", encoding="ascii")
    (package / "__main__.py").write_text(
        "import sys\nprint('[TEST] no provider started')\nsys.exit(" + str(code) + ")\n", encoding="ascii"
    )
    env = dict(os.environ, PYTHONPATH=str(tmp_path), MULTIBOT_PYTHON=sys.executable)
    env.pop("TASKPLAN_STARTER_DRY_RUN", None)
    env.pop("MULTIBOT_NO_PAUSE", None)
    return env


@pytest.mark.parametrize("code", [0, 23])
def test_success_and_failure_wait_for_key_then_keep_exitcode(tmp_path, code):
    env = env_for(tmp_path, code)
    command = 'cmd.exe /d /s /c "' + subprocess.list2cmdline([str(STARTER), "--mode", "1"]) + '"'
    process = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        time.sleep(1)
        assert process.poll() is None
        stdout, stderr = process.communicate("\n", timeout=10)
        assert process.returncode == code and "foreground" in stdout and not stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("opt", ["env-dry", "no-pause", "arg-dry"])
def test_explicit_dry_or_no_pause_skips_wait(tmp_path, opt):
    env = env_for(tmp_path)
    args = [str(STARTER), "--mode", "1"]
    if opt == "env-dry":
        env["TASKPLAN_STARTER_DRY_RUN"] = "1"
    elif opt == "no-pause":
        env["MULTIBOT_NO_PAUSE"] = "1"
    else:
        args.append("--dry-run")
    result = subprocess.run(
        'cmd.exe /d /s /c "' + subprocess.list2cmdline(args) + '"',
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 23 and "[TEST]" in result.stdout and not result.stderr
