"""All explicit console starts stay hidden, including CLI and signing adapters."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from githubbot_core import credentials, multibot_probe, review_gate, transport, typed_actions


@pytest.mark.parametrize("windows_flag", [None, 0x08000000], ids=["posix", "windows"])
def test_every_console_boundary_preserves_windowless_flags(monkeypatch, tmp_path, windows_flag):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "-Y" in command and "sign" in command:
            Path(command[-1] + ".sig").write_bytes(b"synthetic-signature")
        text = kwargs.get("text", False)
        return subprocess.CompletedProcess(command, 0, "{}" if text else b"", "" if text else b"")

    adapter = SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    if windows_flag is not None:
        adapter.CREATE_NO_WINDOW = windows_flag
    for module in (credentials, multibot_probe, review_gate, transport, typed_actions):
        monkeypatch.setattr(module, "subprocess", adapter)
    monkeypatch.setattr(typed_actions, "_git", run)
    monkeypatch.setattr(review_gate.shutil, "which", lambda _: "ssh-keygen")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert transport.GhCliTransport(runner=run).get("repos/example/repo") == ({}, None)
    assert credentials.GhCliTokenProvider(runner=run).token() == "{}"
    assert multibot_probe.query(["api", "user"], run=run)["ok"] is True
    assert typed_actions._run_git(str(tmp_path), "status") == ("{}", None)
    review_gate.verify_ssh_signature(b"{}", b"synthetic-signature", b"synthetic-signers", principal="reviewer")
    assert review_gate._resolve_token() == "{}"

    now = datetime.now(timezone.utc)
    receipt = review_gate.create_receipt(
        kind="model_review",
        repository="example/repo",
        pr_number=1,
        head_sha="a" * 40,
        verdict="approve",
        reviewer="synthetic-reviewer",
        artifact_sha256="b" * 64,
        findings={"critical": 0, "high": 0, "medium": 0, "low": 0},
        checks=["synthetic-only"],
        issued_at=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        valid_until=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        nonce="windowless-synthetic-0001",
    )
    receipt_path, key_path, output_path = (tmp_path / name for name in ("receipt.json", "key", "signature"))
    receipt_path.write_bytes(review_gate.canonical_receipt_bytes(receipt))
    key_path.write_bytes(b"synthetic-key")
    review_gate.sign_receipt(receipt_path, key_path, output_path)
    assert output_path.read_bytes() == b"synthetic-signature"

    assert len(calls) == 7
    assert all(kwargs["creationflags"] == (windows_flag or 0) for _, kwargs in calls)
    assert all(kwargs["capture_output"] is True for _, kwargs in calls)
