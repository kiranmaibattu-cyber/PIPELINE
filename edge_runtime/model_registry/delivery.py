"""Secure management-to-edge model bundle delivery."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Mapping, Protocol

from edge_runtime.graph.models import ModelBundleReference


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class InstalledModelBundle:
    solution_pack: str
    version: str
    path: Path
    sha256: str
    source: str


class BundleDownloader(Protocol):
    def download(self, reference: ModelBundleReference, destination: Path) -> None:
        """Download one bundle archive to destination."""


class HttpBundleDownloader:
    """Streams a bundle from management with an optional bearer token."""

    def __init__(self, timeout_seconds: float = 120.0, max_bytes: int = 20 * 1024**3) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes

    def download(self, reference: ModelBundleReference, destination: Path) -> None:
        headers = {"User-Agent": "pipeline-edge-agent/1"}
        if reference.auth_token_env:
            token = os.environ.get(reference.auth_token_env)
            if not token:
                raise ValueError(
                    f"model bundle token environment variable is not set: {reference.auth_token_env}"
                )
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(reference.url, headers=headers)
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > self._max_bytes:
                raise ValueError(f"model bundle exceeds {self._max_bytes} bytes")
            self._copy_limited(response, destination)

    def _copy_limited(self, source: BinaryIO, destination: Path) -> None:
        received = 0
        with destination.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                received += len(chunk)
                if received > self._max_bytes:
                    raise ValueError(f"model bundle exceeds {self._max_bytes} bytes")
                output.write(chunk)


class ModelBundleStore:
    """Downloads, verifies, and atomically activates immutable model bundles."""

    def __init__(self, root: Path, downloader: BundleDownloader) -> None:
        self._root = root
        self._downloader = downloader

    def ensure(self, reference: ModelBundleReference) -> InstalledModelBundle:
        self._validate_reference(reference)
        target = (
            self._root
            / ".bundles"
            / reference.solution_pack
            / f"{reference.version}-{reference.sha256[:12]}"
        )
        marker = target / ".pipeline-model-bundle"
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == reference.sha256:
            return self._result(reference, target, "cache")

        downloads = self._root / ".downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        archive_fd, archive_name = tempfile.mkstemp(prefix="bundle-", dir=downloads)
        os.close(archive_fd)
        archive = Path(archive_name)
        staging = Path(tempfile.mkdtemp(prefix="install-", dir=target.parent))
        try:
            self._downloader.download(reference, archive)
            actual = _sha256(archive)
            if actual != reference.sha256:
                raise ValueError(
                    f"model bundle checksum mismatch for {reference.solution_pack}: "
                    f"expected {reference.sha256} got {actual}"
                )
            self._extract(archive, staging, reference.archive_format)
            marker_path = staging / ".pipeline-model-bundle"
            marker_path.write_text(reference.sha256 + "\n", encoding="utf-8")
            if target.exists():
                if marker.is_file() and marker.read_text(encoding="utf-8").strip() == reference.sha256:
                    return self._result(reference, target, "cache")
                raise FileExistsError(f"model bundle target already exists but is invalid: {target}")
            staging.rename(target)
            return self._result(reference, target, "downloaded")
        finally:
            archive.unlink(missing_ok=True)
            if staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def _validate_reference(reference: ModelBundleReference) -> None:
        if not _SAFE_COMPONENT_RE.fullmatch(reference.solution_pack):
            raise ValueError(f"unsafe solution pack in model bundle: {reference.solution_pack}")
        if not _SAFE_COMPONENT_RE.fullmatch(reference.version):
            raise ValueError(f"unsafe model bundle version: {reference.version}")
        if not _SHA256_RE.fullmatch(reference.sha256):
            raise ValueError("model bundle sha256 must be 64 lowercase hexadecimal characters")
        if reference.archive_format not in {"tar", "tar.gz", "tgz", "zip"}:
            raise ValueError(f"unsupported model bundle format: {reference.archive_format}")
        if not reference.url.startswith(("http://", "https://")):
            raise ValueError("model bundle URL must use http or https")

    @staticmethod
    def _extract(archive: Path, destination: Path, archive_format: str) -> None:
        if archive_format in {"tar", "tar.gz", "tgz"}:
            mode = "r:gz" if archive_format in {"tar.gz", "tgz"} else "r:"
            with tarfile.open(archive, mode) as bundle:
                for member in bundle.getmembers():
                    if member.issym() or member.islnk() or member.isdev():
                        raise ValueError(f"unsafe model bundle member: {member.name}")
                    _validate_member_path(destination, member.name)
                bundle.extractall(destination, filter="data")
            return
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                _validate_member_path(destination, member.filename)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"unsafe model bundle member: {member.filename}")
            bundle.extractall(destination)

    @staticmethod
    def _result(
        reference: ModelBundleReference,
        target: Path,
        source: str,
    ) -> InstalledModelBundle:
        return InstalledModelBundle(
            solution_pack=reference.solution_pack,
            version=reference.version,
            path=target,
            sha256=reference.sha256,
            source=source,
        )


def _validate_member_path(destination: Path, name: str) -> None:
    candidate = (destination / name).resolve()
    if candidate != destination.resolve() and destination.resolve() not in candidate.parents:
        raise ValueError(f"model bundle path escapes destination: {name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelBundleResolver:
    """Resolves desired references while preserving local bundles for development."""

    def __init__(self, models_root: Path, store: ModelBundleStore) -> None:
        self._models_root = models_root
        self._store = store

    def resolve(
        self,
        solution_packs: set[str],
        references: tuple[ModelBundleReference, ...],
    ) -> Mapping[str, InstalledModelBundle]:
        by_pack = {reference.solution_pack: reference for reference in references}
        unknown = set(by_pack) - solution_packs
        if unknown:
            raise ValueError(f"model bundles assigned to unused solution packs: {sorted(unknown)}")
        resolved: dict[str, InstalledModelBundle] = {}
        for solution_pack in solution_packs:
            reference = by_pack.get(solution_pack)
            if reference:
                resolved[solution_pack] = self._store.ensure(reference)
            else:
                resolved[solution_pack] = InstalledModelBundle(
                    solution_pack=solution_pack,
                    version="local",
                    path=self._models_root / solution_pack,
                    sha256="",
                    source="local",
                )
        return resolved
