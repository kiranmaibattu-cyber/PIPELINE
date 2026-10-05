from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

CONTRACT = "sentry-v5"
SCHEMA_VERSION = "5.2"
SOLUTION_PACK = "sentinel-cv-runtime"
MAX_CAMERAS = 8
STREAM_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "image_schema" / "desired-state.schema.json"
LINE_APPS = {"vehicle_entry_exit_counts", "people_entry_exit_counts", "line_crossing"}
PRESENCE_APPS = {"vehicle_presence", "person_presence"}


@dataclass(frozen=True)
class DesiredCamera:
    camera_id: str
    source: str
    apps: tuple[str, ...]
    fps: float
    config: dict[str, Any] = field(default_factory=dict)
    name: str | None = None


@dataclass(frozen=True)
class DesiredState:
    edge_id: str
    deployment_id: str
    revision: int
    cameras: tuple[DesiredCamera, ...]
    content_hash: str
    contract: str = CONTRACT


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DesiredStateValidator:
    def __init__(self, secrets_root: Path) -> None:
        self.secrets_root = secrets_root.resolve()
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.validator = jsonschema.Draft202012Validator(self.schema, format_checker=jsonschema.FormatChecker())

    def load(self, path: Path) -> DesiredState:
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise ValueError(f"desired-state file is invalid: {exc}") from exc
        self.validate(data)
        return DesiredState(
            edge_id=data["edge_id"], deployment_id=data["deployment_id"],
            revision=data["revision"],
            cameras=tuple(self._camera(item, data) for item in data["cameras"]),
            content_hash=hashlib.sha256(raw).hexdigest(),
        )

    def validate(self, data: Any) -> None:
        errors = sorted(self.validator.iter_errors(data), key=lambda error: list(map(str, error.path)))
        if errors:
            error = errors[0]
            raise ValueError(f"desired state {'.'.join(map(str, error.path))}: {error.message}")
        camera_ids: set[str] = set()
        all_use_cases: set[str] = set()
        for camera in data["cameras"]:
            camera_id = camera["camera_id"]
            if camera_id in camera_ids:
                raise ValueError(f"duplicate camera_id: {camera_id}")
            camera_ids.add(camera_id)
            self._validate_source(camera_id, camera["source"])
            geometry = camera.get("geometry") or {}
            zones = {item["id"]: item for item in geometry.get("zones") or []}
            lines = {item["id"]: item for item in geometry.get("counting_lines") or []}
            geometry_ids = [item["id"] for item in geometry.get("zones") or []] + [item["id"] for item in geometry.get("counting_lines") or []]
            if len(geometry_ids) != len(set(geometry_ids)):
                raise ValueError(f"camera {camera_id} has duplicate geometry IDs")
            bindings = camera["bindings"]
            local_bindings = {item["use_case_id"]: item for item in bindings}
            if len(local_bindings) != len(bindings):
                raise ValueError(f"camera {camera_id} has duplicate use_case_id")
            for binding in bindings:
                use_case_id = binding["use_case_id"]
                if use_case_id in all_use_cases:
                    raise ValueError(f"duplicate use_case_id: {use_case_id}")
                all_use_cases.add(use_case_id)
                ids = binding["geometry_ids"]
                expected = lines if binding["app"] in LINE_APPS else zones
                if binding["scope"] == "geometry" and any(item not in expected for item in ids):
                    raise ValueError(f"{use_case_id} references missing or incompatible geometry")
                if binding["app"] == "scene_search":
                    for trigger_id in binding.get("scene_trigger_use_case_ids") or []:
                        if trigger_id == use_case_id or trigger_id not in local_bindings:
                            raise ValueError(f"{use_case_id} has an invalid scene trigger binding")

    def _validate_source(self, camera_id: str, value: str) -> None:
        try:
            resolved = Path(value.removeprefix("file:")).resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"camera {camera_id} Secret is missing") from exc
        if self.secrets_root not in resolved.parents or resolved.name != f"{camera_id}.url":
            raise ValueError(f"camera {camera_id} Secret path/name is invalid")
        if not resolved.read_text(encoding="utf-8").strip().startswith(STREAM_SCHEMES):
            raise ValueError(f"camera {camera_id} Secret must contain a supported URL")

    @staticmethod
    def _camera(item: dict[str, Any], root: dict[str, Any]) -> DesiredCamera:
        bindings = item["bindings"]
        apps = {"v5_events"}
        if any(binding["app"] == "anpr" for binding in bindings):
            apps.add("plate_detection")
        if any(binding["app"] == "fire_smoke" for binding in bindings):
            apps.add("fire_smoke_detection")
        profiles = {profile for binding in bindings for profile in binding.get("embedding_profile_ids") or []}
        if "adaface-ir101-v18.1" in profiles:
            apps.add("face_recognition")
        if profiles & {"transreid-ssl-v18.1", "gaitbase-v18.1"}:
            apps.add("person_reid")
        if "siglip2-base-v1" in profiles:
            apps.add("scene_embeddings")
        return DesiredCamera(
            camera_id=item["camera_id"], name=item["camera_id"],
            source=item["source"], fps=float(item["fps"]), apps=tuple(sorted(apps)),
            config={"v5": {"deployment_id": root["deployment_id"],
                           "config_revision": root["revision"],
                           "bindings": bindings, "geometry": item.get("geometry") or {}}},
        )
