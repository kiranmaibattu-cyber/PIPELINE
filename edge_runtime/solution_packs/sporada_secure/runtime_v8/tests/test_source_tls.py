"""Camera trust provisioning, rejection diagnostics and fail-closed settings."""
from pathlib import Path
import logging
import os
import ssl
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "worker"))
from pipeline.source_tls import (
    CameraSourceTls, CameraTlsConfigurationError, DEFAULT_CA_DIRECTORY,
    camera_anchor_bundle, camera_ca_files, camera_tls_mode,
)


@pytest.fixture
def ca_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("SENTINEL_CAMERA_CA_FILE", raising=False)
    directory = tmp_path / "camera-ca"
    directory.mkdir()
    monkeypatch.setenv("SENTINEL_CAMERA_CA_DIR", str(directory))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    return directory


@pytest.fixture
def certificate(tmp_path):
    path = tmp_path / "test-ca.crt"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-days", "1", "-subj", "/CN=Camera TLS test root",
        "-keyout", str(tmp_path / "test-only.key"), "-out", str(path),
    ], check=True, capture_output=True)
    return path


def test_no_extra_ca_keeps_system_database(ca_environment):
    assert camera_ca_files() == []
    assert camera_anchor_bundle([]) is None


def test_default_matches_authorized_legacy_camera_behavior(monkeypatch):
    monkeypatch.delenv("SENTINEL_CAMERA_TLS_MODE", raising=False)
    assert camera_tls_mode() == "legacy-compatible"
    monkeypatch.setenv("SENTINEL_CAMERA_TLS_MODE", "strict")
    assert camera_tls_mode() == "strict"


@pytest.mark.parametrize("value", ["", "disabled", "false", "verify", "typo"])
def test_invalid_mode_fails_closed(monkeypatch, value):
    monkeypatch.setenv("SENTINEL_CAMERA_TLS_MODE", value)
    with pytest.raises(CameraTlsConfigurationError, match="must be"):
        camera_tls_mode()


def test_unmounted_default_directory_is_optional(monkeypatch):
    monkeypatch.delenv("SENTINEL_CAMERA_CA_FILE", raising=False)
    monkeypatch.setenv("SENTINEL_CAMERA_CA_DIR", DEFAULT_CA_DIRECTORY)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert camera_ca_files() == []


def test_explicit_missing_file_fails_closed(ca_environment, monkeypatch):
    monkeypatch.setenv("SENTINEL_CAMERA_CA_FILE", str(ca_environment / "missing.crt"))
    with pytest.raises(CameraTlsConfigurationError, match="file is missing"):
        camera_ca_files()


def test_explicit_missing_directory_fails_closed(ca_environment, monkeypatch):
    monkeypatch.setenv("SENTINEL_CAMERA_CA_DIR", str(ca_environment / "missing"))
    with pytest.raises(CameraTlsConfigurationError, match="directory is missing"):
        camera_ca_files()


def test_kubernetes_secret_symlinks_are_loaded(ca_environment, certificate):
    data = ca_environment / "..data"
    data.mkdir()
    (data / "root.crt").write_bytes(certificate.read_bytes())
    (ca_environment / "root.crt").symlink_to("..data/root.crt")
    (ca_environment / "ignored.key").write_text("not a certificate")
    assert camera_ca_files() == [ca_environment / "root.crt"]


def test_site_anchor_preserves_public_roots_and_has_private_cache_permissions(
    ca_environment, certificate,
):
    system = Path(ssl.get_default_verify_paths().cafile).read_bytes()
    path = camera_anchor_bundle([certificate])
    assert system in path.read_bytes()
    assert certificate.read_bytes() in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    context = ssl.create_default_context()
    original = context.cert_store_stats()["x509_ca"]
    context.load_verify_locations(cafile=str(path))
    assert context.cert_store_stats()["x509_ca"] == original + 1


def test_bundle_reuse_and_certificate_rotation(ca_environment, certificate):
    first = camera_anchor_bundle([certificate])
    assert camera_anchor_bundle([certificate]) == first
    certificate.write_bytes(certificate.read_bytes() + b"\n")
    second = camera_anchor_bundle([certificate])
    assert first != second
    assert first.exists() and second.exists()


@pytest.mark.parametrize("content", [
    b"not PEM", b"-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----",
    b"-----BEGIN PRIVATE KEY-----\nnot a public trust anchor",
    b"x" * (1024 * 1024 + 1),
])
def test_invalid_or_private_material_is_rejected(ca_environment, content):
    path = ca_environment / "invalid.crt"
    path.write_bytes(content)
    with pytest.raises(CameraTlsConfigurationError):
        camera_anchor_bundle([path])


def test_rejection_keeps_verification_and_has_camera_context(caplog):
    policy = CameraSourceTls("camera-04", "session-04")
    with caplog.at_level(logging.ERROR):
        assert policy._reject_certificate(None, None, None, 1) is False
    record = caplog.records[-1]
    assert record.camera_id == "camera-04"
    assert record.stream_session_id == "session-04"
    assert record.tls_certificate_error_flags == 1
    assert record.retry_reason == "tls_unknown_ca"
    assert "trusted camera CA" in record.message
    assert policy.reason("generic error") == "tls_unknown_ca"


def test_multiple_errors_remain_rejected_and_diagnostics_are_redacted():
    policy = CameraSourceTls("camera-04", "session-04")
    assert policy._reject_certificate(None, None, None, 1 | 2 | 8) is False
    text = policy.describe("rtsps://user:SECRET@host/path token=SECRETTOKEN")
    assert "unknown_ca,bad_identity,expired" in text
    assert "SECRET" not in text
    policy.clear_error()
    assert policy.reason("timeout") == "stream_disconnected"
    assert "certificate rejected" not in policy.describe("timeout")


@pytest.mark.parametrize("message", ["401 Unauthorized", "authentication: unauthorized"])
def test_authentication_failure_is_separate_from_tls(message):
    policy = CameraSourceTls("camera-15", "session-15")
    assert policy.reason(message) == "source_authentication_failed"
