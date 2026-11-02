"""Client certificate state must fail closed without empty TLS-path overrides."""
import subprocess

import pytest

from githubbot_core import typed_actions


def test_bot_transport_does_not_pass_empty_client_certificate_paths():
    flags = typed_actions._git_auth_config("example/repo")
    assert not any(flag.lower().endswith(("sslcert=", "sslkey=")) for flag in flags)
    assert "http.sslVerify=true" in flags
    assert "credential.helper=" in flags


@pytest.mark.parametrize("key", ["http.sslCert", "http.sslKey", "http.https://github.com/.sslCert"])
def test_local_client_certificate_is_rejected_even_with_empty_value(tmp_path, key):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", key, ""], check=True, capture_output=True)
    assert "client-certificate" in typed_actions._client_certificate_policy_error(str(tmp_path))


def test_clean_local_configuration_needs_no_client_certificate(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    assert typed_actions._client_certificate_policy_error(str(tmp_path)) is None


def test_included_client_key_is_also_rejected(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    included = tmp_path / "certificate.config"
    included.write_text('[http "https://github.com/"]\n sslKey = ignored-private-path\n', encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "include.path", str(included)],
        check=True, capture_output=True,
    )
    assert "client-certificate" in typed_actions._client_certificate_policy_error(str(tmp_path))


def test_certificate_probe_errors_are_fail_closed_and_redacted(monkeypatch):
    monkeypatch.setattr(
        typed_actions, "_git",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 128, "private-config", "private-error"),
    )
    error = typed_actions._client_certificate_policy_error(".")
    assert error and "private" not in error
