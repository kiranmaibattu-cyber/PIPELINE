"""Deployment-provided camera trust anchors and credential-safe RTSPS errors.

Trust is configured before a camera bin can join a running shared graph.
Certificate verification is always enabled; a rejected certificate is never
accepted by the diagnostic callback. No desired-state or wire fields change.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import ssl
import tempfile
import threading

from pipeline.structured_logging import log_event, sanitize_text

logger = logging.getLogger(__name__)
DEFAULT_CA_DIRECTORY = "/run/secrets/sentinel/camera-ca"
_CERTIFICATE_ERRORS = (
    (1, "unknown_ca"), (2, "bad_identity"), (4, "not_activated"),
    (8, "expired"), (16, "revoked"), (32, "insecure"),
    (64, "generic_error"),
)


class CameraTlsConfigurationError(RuntimeError):
    """Explicit camera trust configuration is missing or invalid."""


def camera_ca_files() -> list[Path]:
    """Read a mounted Secret directory, including Kubernetes symlink entries."""
    result: list[Path] = []
    explicit = os.getenv("SENTINEL_CAMERA_CA_FILE", "").strip()
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise CameraTlsConfigurationError("configured camera CA file is missing")
        result.append(path)
    configured_directory = os.getenv("SENTINEL_CAMERA_CA_DIR", "").strip()
    directory = Path(configured_directory or DEFAULT_CA_DIRECTORY)
    if directory.exists():
        if not directory.is_dir():
            raise CameraTlsConfigurationError("camera CA directory is not a directory")
        try:
            result.extend(sorted(
                p for p in directory.iterdir()
                if p.suffix.lower() in {".crt", ".pem"} and not p.name.startswith(".")
            ))
        except OSError as exc:
            raise CameraTlsConfigurationError("camera CA directory is unreadable") from exc
    elif configured_directory and directory != Path(DEFAULT_CA_DIRECTORY):
        raise CameraTlsConfigurationError("configured camera CA directory is missing")
    if len(result) > 128:
        raise CameraTlsConfigurationError("camera CA file count exceeds 128")
    return list(dict.fromkeys(result))


def camera_anchor_bundle(files: list[Path]) -> Path | None:
    """Add site CAs to system roots, without replacing normal public trust."""
    if not files:
        return None
    system = ssl.get_default_verify_paths().cafile
    if not system:
        raise CameraTlsConfigurationError("system CA bundle is unavailable")
    try:
        parts = [Path(system).read_bytes()]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        for path in files:
            data = path.read_bytes()
            if len(data) > 1024 * 1024 or sum(map(len, parts)) + len(data) > 4 * 1024 * 1024:
                raise CameraTlsConfigurationError("camera CA bundle size limit exceeded")
            if b"PRIVATE KEY" in data or b"-----BEGIN CERTIFICATE-----" not in data:
                raise CameraTlsConfigurationError("camera CA file must contain public PEM certificates only")
            context.load_verify_locations(cadata=data.decode("ascii"))
            parts.append(data)
    except (OSError, UnicodeError, ssl.SSLError) as exc:
        raise CameraTlsConfigurationError("camera CA file is unreadable or invalid PEM") from exc
    content = b"\n".join(parts) + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    directory = Path(os.getenv("XDG_RUNTIME_DIR", "/tmp/sentinel/runtime")) / "camera-ca"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"anchors-{digest}.pem"
        if not destination.exists():
            fd, temporary = tempfile.mkstemp(prefix=".anchors-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
    except OSError as exc:
        raise CameraTlsConfigurationError("camera CA cache is not writable") from exc
    return destination


class CameraSourceTls:
    """Per-camera certificate diagnostics, also used by decoder health/retries."""

    def __init__(self, camera_id: str, stream_session_id: str) -> None:
        self.camera_id = str(camera_id)
        self.stream_session_id = str(stream_session_id)
        self._lock = threading.Lock()
        self._flags = 0
        self._configuration_error = False

    def configure(self, source) -> None:
        from gi.repository import Gio

        with self._lock:
            self._flags = 0
            self._configuration_error = False
        try:
            backend = Gio.TlsBackend.get_default()
            if not backend.supports_tls():
                raise CameraTlsConfigurationError("GStreamer TLS backend is unavailable")
            files = camera_ca_files()
            bundle = camera_anchor_bundle(files)
            source.set_property("tls-validation-flags", Gio.TlsCertificateFlags.VALIDATE_ALL)
            if bundle is not None:
                source.set_property("tls-database", Gio.TlsFileDatabase.new(str(bundle)))
            source.connect("accept-certificate", self._reject_certificate)
            log_event(
                logger, logging.INFO, "camera_tls_configured",
                "RTSPS certificate verification enabled",
                camera_id=self.camera_id, stream_session_id=self.stream_session_id,
                trust_source="system_and_camera_ca" if bundle else "system",
                camera_ca_files=len(files), tls_backend=backend.__class__.__name__,
            )
        except Exception as exc:
            with self._lock:
                self._configuration_error = True
            if isinstance(exc, CameraTlsConfigurationError):
                raise
            raise CameraTlsConfigurationError("could not configure camera TLS trust") from exc

    def _reject_certificate(self, _source, _connection, _certificate, flags) -> bool:
        # Runs on a GStreamer thread; provide explicit camera/session context.
        with self._lock:
            self._flags = int(flags)
        log_event(
            logger, logging.ERROR, "camera_tls_certificate_rejected",
            self.describe("RTSPS server certificate rejected"),
            camera_id=self.camera_id, stream_session_id=self.stream_session_id,
            tls_certificate_error_flags=int(flags),
            retry_reason=self.reason(""),
        )
        return False

    def reason(self, detail: str) -> str:
        with self._lock:
            flags, configuration_error = self._flags, self._configuration_error
        if configuration_error:
            return "tls_configuration_error"
        if flags:
            names = [name for flag, name in _CERTIFICATE_ERRORS if flags & flag]
            return "tls_" + (names[0] if names else "certificate_rejected")
        if "401" in str(detail) or "unauthorized" in str(detail).lower():
            return "source_authentication_failed"
        return "stream_disconnected"

    def clear_error(self) -> None:
        """A real received frame proves that a previous TLS failure recovered."""
        with self._lock:
            self._flags = 0
            self._configuration_error = False

    def describe(self, detail: str) -> str:
        safe = sanitize_text(detail, limit=1024)
        with self._lock:
            flags, configuration_error = self._flags, self._configuration_error
        if configuration_error:
            return "camera TLS configuration failed; " + safe
        if not flags:
            return safe
        names = [name for flag, name in _CERTIFICATE_ERRORS if flags & flag]
        diagnosis = f"RTSPS certificate rejected: {','.join(names) or 'unknown'} (flags={flags})"
        if flags & 1:
            diagnosis += "; mount the trusted camera CA under " + DEFAULT_CA_DIRECTORY
        return diagnosis + "; " + safe
