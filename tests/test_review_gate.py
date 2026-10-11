from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from githubbot_core.review_gate import (
    ACTIONS_APP_ID,
    RECEIPT_SCHEMA,
    ReceiptValidationError,
    build_receipt,
    build_protection_payloads,
    canonical_receipt_bytes,
    create_receipt,
    decode_dispatch_payload,
    encode_dispatch_payload,
    validate_receipt_bytes,
    verify_ssh_signature,
)
from githubbot_core import review_gate


REPO_ROOT = Path(__file__).resolve().parents[1]


def _now() -> datetime:
    return datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def _receipt(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "kind": "model_review",
        "repository": "example-org/example-repo",
        "pr_number": 6,
        "head_sha": "2f9e8f6fe280e2449b2d6081ef55a5cfdee431ea",
        "verdict": "approve",
        "reviewer": "model-reviewer",
        "artifact_sha256": "a" * 64,
        "findings": {"critical": 0, "high": 0, "medium": 1, "low": 2},
        "checks": ["pytest:383", "ruff:clean", "codeql:green"],
        "issued_at": "2026-08-31T09:55:00Z",
        "valid_until": "2026-08-31T10:55:00Z",
        "nonce": "review-nonce-0000000001",
    }
    values.update(overrides)
    return create_receipt(**values)


def test_model_review_receipt_is_canonical_and_valid() -> None:
    receipt = _receipt()
    encoded = canonical_receipt_bytes(receipt)

    assert encoded.endswith(b"\n")
    assert json.loads(encoded) == receipt
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert validate_receipt_bytes(
        encoded,
        expected_repository="example-org/example-repo",
        expected_pr_number=6,
        expected_head_sha="2f9e8f6fe280e2449b2d6081ef55a5cfdee431ea",
        now=_now(),
    ) == receipt


def test_receipt_rejects_noncanonical_bytes_and_wrong_head() -> None:
    receipt = _receipt()
    pretty = json.dumps(receipt, indent=2).encode("utf-8")

    with pytest.raises(ReceiptValidationError, match="canonical"):
        validate_receipt_bytes(pretty, now=_now())

    with pytest.raises(ReceiptValidationError, match="head SHA"):
        validate_receipt_bytes(
            canonical_receipt_bytes(receipt),
            expected_head_sha="b" * 40,
            now=_now(),
        )


def test_approval_rejects_high_findings_and_expired_receipt() -> None:
    receipt = _receipt(findings={"critical": 0, "high": 1, "medium": 0, "low": 0})
    with pytest.raises(ReceiptValidationError, match="high findings"):
        validate_receipt_bytes(canonical_receipt_bytes(receipt), now=_now())

    expired = _receipt(valid_until="2026-08-31T09:59:59Z")
    with pytest.raises(ReceiptValidationError, match="expired"):
        validate_receipt_bytes(canonical_receipt_bytes(expired), now=_now())


def test_owner_override_requires_explicit_decision_and_reason() -> None:
    with pytest.raises(ReceiptValidationError, match="decision_id"):
        _receipt(kind="owner_override", reviewer="octocat", findings={"critical": 1, "high": 1, "medium": 0, "low": 0})

    receipt = _receipt(
        kind="owner_override",
        reviewer="octocat",
        findings={"critical": 1, "high": 1, "medium": 0, "low": 0},
        decision_id="DECISION-0001",
        reason="Owner accepts the exact-SHA bootstrap risk for the pilot repository.",
    )
    assert validate_receipt_bytes(canonical_receipt_bytes(receipt), now=_now()) == receipt


def test_dispatch_payload_round_trip() -> None:
    receipt_bytes = canonical_receipt_bytes(_receipt())
    signature = b"-----BEGIN SSH SIGNATURE-----\nexample\n-----END SSH SIGNATURE-----\n"

    payload = encode_dispatch_payload(receipt_bytes, signature)
    decoded_receipt, decoded_signature = decode_dispatch_payload(payload)

    assert base64.b64decode(payload["receipt_b64"]) == receipt_bytes
    assert decoded_receipt == receipt_bytes
    assert decoded_signature == signature


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH ssh-keygen is unavailable")
def test_real_ssh_signature_is_bound_to_principal_and_receipt(tmp_path: Path) -> None:
    key = tmp_path / "reviewer"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(canonical_receipt_bytes(_receipt()))
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "githubbot-review-gate", str(receipt_path)],
        check=True,
    )
    allowed = tmp_path / "allowed_signers"
    public_key = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    allowed.write_text(f"githubbot-reviewer {public_key}\n", encoding="utf-8")

    verify_ssh_signature(
        receipt_path.read_bytes(),
        Path(f"{receipt_path}.sig").read_bytes(),
        allowed.read_bytes(),
        principal="githubbot-reviewer",
    )

    with pytest.raises(ReceiptValidationError, match="signature"):
        verify_ssh_signature(
            receipt_path.read_bytes(),
            Path(f"{receipt_path}.sig").read_bytes(),
            allowed.read_bytes(),
            principal="githubbot-owner",
        )


def test_protection_plan_replaces_self_review_with_actions_pinned_gate() -> None:
    current = {
        "required_status_checks": None,
        "required_pull_request_reviews": {
            "url": "https://api.github.test/reviews",
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 1,
            "require_last_push_approval": False,
        },
        "enforce_admins": {"enabled": False},
        "restrictions": None,
        "required_conversation_resolution": {"enabled": True},
        "required_linear_history": {"enabled": False},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "block_creations": {"enabled": False},
        "lock_branch": {"enabled": False},
        "allow_fork_syncing": {"enabled": False},
    }

    rollback, desired = build_protection_payloads(current)

    assert rollback["required_pull_request_reviews"]["required_approving_review_count"] == 1
    assert rollback["required_status_checks"] is None
    assert desired["required_pull_request_reviews"] == {
        "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False,
        "required_approving_review_count": 0,
        "require_last_push_approval": False,
    }
    assert desired["required_status_checks"] == {
        "strict": True,
        "checks": [{"context": "githubbot/review-gate", "app_id": ACTIONS_APP_ID}],
    }
    assert desired["enforce_admins"] is True
    assert desired["allow_force_pushes"] is False
    assert desired["allow_deletions"] is False
    assert desired["required_conversation_resolution"] is True


def test_protection_rollback_accepts_an_unpinned_existing_status() -> None:
    current = {
        "required_status_checks": {
            "strict": False,
            "checks": [{"context": "ci/test", "app_id": None}],
        },
        "required_pull_request_reviews": None,
        "enforce_admins": {"enabled": False},
        "restrictions": None,
    }

    rollback, _ = build_protection_payloads(current)

    assert rollback["required_status_checks"] == {
        "strict": False,
        "checks": [{"context": "ci/test", "app_id": -1}],
    }


def test_workflow_never_executes_pull_request_code_with_privileged_token() -> None:
    workflow = (REPO_ROOT / "src" / "githubbot_core" / "templates" / "review-gate.yml").read_text(encoding="utf-8")

    assert "pull_request_target:" in workflow
    assert "repository_dispatch:" in workflow
    assert "statuses: write" in workflow
    assert "pull-requests: read" in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in workflow
    assert "repository: ${{ github.event.pull_request.head.repo.full_name }}" not in workflow
    assert "allow-unsafe-pr-checkout" not in workflow
    assert "pip install" not in workflow
    assert "npm " not in workflow
    assert ".github/review-gate/review_gate.py workflow-pending" in workflow
    assert ".github/review-gate/review_gate.py workflow-dispatch" in workflow
    # B1 (Review 2026-09-03): both privileged jobs must hand the Actions token to the
    # verifier, otherwise _resolve_token() fails before any status is posted.
    assert workflow.count("GH_TOKEN: ${{ github.token }}") == 2
    # S4: the checkout action is pinned to a commit SHA, not a movable tag.
    assert "actions/checkout@v4" not in workflow
    assert re.search(r"uses: actions/checkout@[0-9a-f]{40}", workflow)


def test_build_receipt_hashes_the_review_artifact_and_cli_create_is_reproducible(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "review.md"
    artifact.write_bytes(b"# Review\n\nNo blocking findings.\n")
    receipt = build_receipt(
        kind="model_review",
        repository="example-org/example-repo",
        pr_number=6,
        head_sha="2f9e8f6fe280e2449b2d6081ef55a5cfdee431ea",
        verdict="approve",
        reviewer="model-reviewer",
        artifact_path=artifact,
        findings={"critical": 0, "high": 0, "medium": 0, "low": 0},
        checks=["pytest:green"],
        now=_now(),
    )

    assert receipt["artifact_sha256"] == "8beae8e173a6f781110d12b7c2a5d4cc45932e6b7a548d06d2742cb3bc739744"
    assert receipt["issued_at"] == "2026-08-31T10:00:00Z"


def test_reject_dispatch_posts_one_failure_status(monkeypatch: pytest.MonkeyPatch) -> None:
    current = datetime.now(timezone.utc).replace(microsecond=0)
    receipt = _receipt(
        verdict="reject",
        reason="A blocking governance issue remains.",
        issued_at=(current - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        valid_until=(current + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
    )
    payload = encode_dispatch_payload(canonical_receipt_bytes(receipt), b"signature")
    statuses: list[tuple[str, str]] = []

    monkeypatch.setattr(review_gate, "_resolve_token", lambda: "test-token")
    monkeypatch.setattr(
        review_gate,
        "_live_pr",
        lambda repository, pr_number, token: {
            "state": "open",
            "head": {"sha": receipt["head_sha"]},
        },
    )
    monkeypatch.setattr(review_gate, "verify_ssh_signature", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        review_gate,
        "_post_status",
        lambda repository, sha, state, description, **kwargs: statuses.append((state, description)),
    )

    with pytest.raises(ReceiptValidationError, match="verdict is reject"):
        review_gate.workflow_dispatch("example-org/example-repo", payload, b"allowed signer")

    assert len(statuses) == 1
    assert statuses[0][0] == "failure"


def test_cli_create_writes_a_canonical_receipt(tmp_path: Path) -> None:
    artifact = tmp_path / "review.md"
    artifact.write_text("review evidence\n", encoding="utf-8")
    output = tmp_path / "receipt.json"

    result = review_gate.main(
        [
            "create",
            "--kind",
            "model_review",
            "--repo",
            "example-org/example-repo",
            "--pr",
            "6",
            "--head-sha",
            "2f9e8f6fe280e2449b2d6081ef55a5cfdee431ea",
            "--verdict",
            "approve",
            "--reviewer",
            "model-reviewer",
            "--artifact",
            str(artifact),
            "--check",
            "pytest:green",
            "--check",
            "ruff:clean",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    receipt = validate_receipt_bytes(output.read_bytes(), now=datetime.now(timezone.utc))
    assert receipt["checks"] == ["pytest:green", "ruff:clean"]
    assert receipt["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH ssh-keygen is unavailable")
def test_cli_sign_writes_only_the_requested_signature(tmp_path: Path) -> None:
    key = tmp_path / "reviewer"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    receipt = tmp_path / "receipt.json"
    current = datetime.now(timezone.utc).replace(microsecond=0)
    current_receipt = _receipt(
        issued_at=(current - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        valid_until=(current + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
    )
    receipt.write_bytes(canonical_receipt_bytes(current_receipt))
    signature = tmp_path / "signed" / "receipt.sig"

    assert review_gate.main(
        ["sign", "--receipt", str(receipt), "--key", str(key), "--output", str(signature)]
    ) == 0
    assert signature.is_file()
    assert not Path(f"{receipt}.sig").exists()


def test_local_dispatch_dry_run_verifies_live_head_and_apply_posts_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = datetime.now(timezone.utc).replace(microsecond=0)
    receipt_value = _receipt(
        issued_at=(current - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        valid_until=(current + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(canonical_receipt_bytes(receipt_value))
    signature = tmp_path / "receipt.sig"
    signature.write_bytes(b"signature")
    allowed = tmp_path / "allowed_signers"
    allowed.write_bytes(b"allowed signer")
    posts: list[tuple[str, str, object]] = []

    monkeypatch.setattr(review_gate, "verify_ssh_signature", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_gate, "_resolve_token", lambda: "test-token")
    monkeypatch.setattr(
        review_gate,
        "_live_pr",
        lambda repository, pr_number, token: {
            "state": "open",
            "head": {"sha": receipt_value["head_sha"]},
        },
    )
    monkeypatch.setattr(
        review_gate,
        "_github_api",
        lambda method, path, token, payload=None: posts.append((method, path, payload)),
    )

    summary = review_gate.dispatch_receipt(receipt, signature, allowed, apply=False)
    assert summary["applied"] is False
    assert posts == []

    summary = review_gate.dispatch_receipt(receipt, signature, allowed, apply=True)
    assert summary["applied"] is True
    assert len(posts) == 1
    assert posts[0][0:2] == ("POST", "repos/example-org/example-repo/dispatches")
    assert posts[0][2]["event_type"] == "githubbot-review-gate"


def test_protection_plan_refuses_to_overwrite_a_rollback(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text(
        json.dumps(
            {
                "required_status_checks": None,
                "required_pull_request_reviews": None,
                "enforce_admins": {"enabled": False},
                "restrictions": None,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "plan"
    output.mkdir()
    rollback = output / "rollback.json"
    rollback.write_text("do not overwrite\n", encoding="utf-8")

    assert review_gate.main(
        ["protection-plan", "--current", str(current), "--output-dir", str(output)]
    ) == 2
    assert rollback.read_text(encoding="utf-8") == "do not overwrite\n"
    assert not (output / "desired.json").exists()
