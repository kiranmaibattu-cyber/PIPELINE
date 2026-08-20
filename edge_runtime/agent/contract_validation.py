"""Strict ApexFabric V1 desired-state validation."""
from __future__ import annotations

import json
from pathlib import Path

from edge_runtime.graph.manifest_loader import ManifestRepository


class ApexFabricV1DesiredStateValidator:
    """Validates one solution image without resolving or exposing Secret values."""

    _ROOT_FIELDS = {"edge_id", "revision", "cameras"}
    _CAMERA_FIELDS = {"camera_id", "source", "solution_pack", "fps", "apps", "config"}

    def __init__(
        self,
        solution_pack: str,
        manifests: ManifestRepository,
        secrets_root: Path = Path("/run/secrets/apexfabric"),
    ) -> None:
        self._solution_pack = solution_pack
        self._manifests = manifests
        self._secrets_root = secrets_root.resolve()

    def validate(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"desired-state file not found: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"desired-state file is invalid: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("desired state must be a JSON object")
        unknown = set(data) - self._ROOT_FIELDS
        if unknown:
            raise ValueError(f"unknown desired-state fields: {sorted(unknown)}")
        if not isinstance(data.get("edge_id"), str) or not data["edge_id"].strip():
            raise ValueError("edge_id must be a non-empty string")
        if not isinstance(data.get("revision"), int) or data["revision"] < 1:
            raise ValueError("revision must be an integer greater than zero")
        cameras = data.get("cameras")
        if not isinstance(cameras, list):
            raise ValueError("cameras must be an array")

        camera_ids: set[str] = set()
        for index, camera in enumerate(cameras):
            self._validate_camera(camera, index, camera_ids)

    def _validate_camera(self, camera, index: int, camera_ids: set[str]) -> None:
        if not isinstance(camera, dict):
            raise ValueError(f"camera at index {index} must be an object")
        unknown = set(camera) - self._CAMERA_FIELDS
        if unknown:
            raise ValueError(f"camera at index {index} has unknown fields: {sorted(unknown)}")
        required = {"camera_id", "source", "solution_pack", "apps"}
        missing = required - set(camera)
        if missing:
            raise ValueError(f"camera at index {index} is missing fields: {sorted(missing)}")

        camera_id = camera["camera_id"]
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError(f"camera at index {index} has an invalid camera_id")
        if camera_id in camera_ids:
            raise ValueError(f"duplicate camera_id: {camera_id}")
        camera_ids.add(camera_id)

        if camera["solution_pack"] != self._solution_pack:
            raise ValueError(
                f"camera {camera_id} targets {camera['solution_pack']!r}; "
                f"this image only supports {self._solution_pack!r}"
            )
        fps = camera.get("fps", 10)
        if not isinstance(fps, (int, float)) or isinstance(fps, bool) or fps <= 0:
            raise ValueError(f"camera {camera_id} fps must be greater than zero")
        apps = camera["apps"]
        if not isinstance(apps, list) or not apps or any(not isinstance(app, str) for app in apps):
            raise ValueError(f"camera {camera_id} apps must be a non-empty string array")
        if len(apps) != len(set(apps)):
            raise ValueError(f"camera {camera_id} contains duplicate apps")
        for app in apps:
            try:
                self._manifests.get(self._solution_pack, app)
            except KeyError as exc:
                raise ValueError(f"camera {camera_id} uses unsupported app: {app}") from exc
        config = camera.get("config", {})
        if not isinstance(config, dict):
            raise ValueError(f"camera {camera_id} config must be an object")
        self._validate_secret_reference(camera_id, camera["source"])

    def _validate_secret_reference(self, camera_id: str, source) -> None:
        if not isinstance(source, str) or not source.startswith("file:"):
            raise ValueError(
                f"camera {camera_id} source must reference a mounted Secret with file:"
            )
        path_text = source.removeprefix("file:").strip()
        if not path_text:
            raise ValueError(f"camera {camera_id} Secret path is empty")
        path = Path(path_text)
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(f"camera {camera_id} Secret file is missing or unreadable") from exc
        if resolved != self._secrets_root and self._secrets_root not in resolved.parents:
            raise ValueError(
                f"camera {camera_id} Secret must be under {self._secrets_root}"
            )
        try:
            value = resolved.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"camera {camera_id} Secret file is missing or unreadable") from exc
        if not value.startswith(("rtsp://", "rtsps://")):
            raise ValueError(f"camera {camera_id} Secret does not contain an RTSP source")
