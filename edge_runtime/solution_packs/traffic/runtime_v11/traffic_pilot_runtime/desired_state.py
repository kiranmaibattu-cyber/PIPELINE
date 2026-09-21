from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STREAM_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://")
SOLUTION_PACK = "sporada-secure"
MAX_CAMERAS = 8
MAX_FPS = 60.0
FACE_MODEL_ID = "face-embedding-model-v1"
FACE_EMBEDDING_DIMENSIONS = 512
SUPPORTED_APPS = {
    "vehicle_counting",
    "pedestrian_counting",
    "plate_detection",
    "fire_smoke_detection",
    "face_recognition",
}
SCHEMA_APP_NAMES = {
    "anpr",
    "vehicle_counting",
    "pedestrian_counting",
    "fire_smoke_detection",
    "face_recognition",
}
APP_ALIASES = {
    "anpr": "plate_detection",
}
APP_CONFIG_KEYS = {
    "plate_detection": ("plate_detection", "anpr"),
}
# The Sporada Secure contract allows only four-point zones per application;
# lines are rejected for every use case.
ZONE_APP_KEYS = {
    "vehicle_counting",
    "pedestrian_counting",
    "anpr",
    "fire_smoke_detection",
    "face_recognition",
}
@dataclass(frozen=True)
class DesiredCamera:
    camera_id: str
    source: str
    apps: tuple[str, ...]
    fps: float = 10.0
    config: dict[str, Any] = field(default_factory=dict)
    name: str | None = None


@dataclass(frozen=True)
class DesiredState:
    edge_id: str
    revision: int
    cameras: tuple[DesiredCamera, ...]
    content_hash: str


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DesiredStateValidator:
    def __init__(self, secrets_root: Path) -> None:
        self.secrets_root = secrets_root.resolve()

    def load(self, path: Path) -> DesiredState:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError as exc:
            raise ValueError(f"desired-state file not found: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"desired-state file is invalid: {exc}") from exc
        self.validate(data)
        cameras = []
        for cam in data["cameras"]:
            camera_id = _camera_id(cam)
            cameras.append(
                DesiredCamera(
                    camera_id=camera_id,
                    name=str(cam.get("name") or camera_id),
                    source=str(cam["source"]),
                    apps=tuple(_canonical_app(str(app)) for app in cam["apps"]),
                    fps=float(cam.get("fps", 10.0)),
                    config=dict(cam.get("config") or {}),
                )
            )
        cameras = tuple(cameras)
        return DesiredState(
            edge_id=str(data["edge_id"]),
            revision=int(data["revision"]),
            cameras=cameras,
            content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )

    def validate(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise ValueError("desired state must be a JSON object")
        unknown = set(data) - {"edge_id", "revision", "cameras"}
        if unknown:
            raise ValueError(f"unknown desired-state fields: {sorted(unknown)}")
        if not isinstance(data.get("edge_id"), str) or not data["edge_id"].strip():
            raise ValueError("edge_id must be a non-empty string")
        if not isinstance(data.get("revision"), int) or data["revision"] < 1:
            raise ValueError("revision must be an integer greater than zero")
        cameras = data.get("cameras")
        if not isinstance(cameras, list) or not cameras:
            raise ValueError("cameras must be a non-empty array")
        if len(cameras) > MAX_CAMERAS:
            raise ValueError(f"cameras must contain at most {MAX_CAMERAS} entries")
        seen: set[str] = set()
        for index, camera in enumerate(cameras):
            self._validate_camera(index, camera, seen)

    def _validate_camera(self, index: int, camera: Any, seen: set[str]) -> None:
        if not isinstance(camera, dict):
            raise ValueError(f"camera at index {index} must be an object")
        unknown = set(camera) - {"camera_id", "source", "fps", "apps", "config", "solution_pack"}
        if unknown:
            raise ValueError(f"camera at index {index} has unknown fields: {sorted(unknown)}")
        missing = {"camera_id", "source", "solution_pack", "apps", "config"} - set(camera)
        if missing:
            raise ValueError(f"camera at index {index} is missing fields: {sorted(missing)}")
        if camera.get("solution_pack") != SOLUTION_PACK:
            raise ValueError(f"camera at index {index} solution_pack must be {SOLUTION_PACK}")
        camera_id = _camera_id(camera)
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError(f"camera at index {index} has an invalid camera_id")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", camera_id):
            raise ValueError(f"camera at index {index} camera_id has invalid characters")
        if camera_id in seen:
            raise ValueError(f"duplicate camera_id: {camera_id}")
        seen.add(camera_id)
        fps = camera.get("fps", 10.0)
        if not isinstance(fps, (int, float)) or isinstance(fps, bool) or not 0 < fps <= MAX_FPS:
            raise ValueError(f"camera {camera_id} fps must be greater than zero and at most {MAX_FPS:g}")
        apps = camera["apps"]
        if not isinstance(apps, list) or not apps or any(not isinstance(app, str) for app in apps):
            raise ValueError(f"camera {camera_id} apps must be a non-empty string array")
        if len(apps) != len(set(apps)):
            raise ValueError(f"camera {camera_id} contains duplicate apps")
        unsupported = set(apps) - SCHEMA_APP_NAMES
        if unsupported:
            raise ValueError(f"camera {camera_id} uses unsupported apps: {sorted(unsupported)}")
        canonical_apps = [_canonical_app(app) for app in apps]
        config = camera.get("config") or {}
        if not isinstance(config, dict):
            raise ValueError(f"camera {camera_id} config must be an object")
        self._validate_source(camera_id, camera["source"])
        self._validate_geometry_config(camera_id, set(canonical_apps), config)

    def _validate_source(self, camera_id: str, source: Any) -> None:
        if not isinstance(source, str) or not source.startswith("file:"):
            raise ValueError(
                f"camera {camera_id} source must reference a Secret under /run/secrets/apexfabric with file:"
            )
        default_root = Path("/run/secrets/apexfabric").resolve()
        if self.secrets_root == default_root and not re.fullmatch(r"file:/run/secrets/apexfabric/[A-Za-z0-9._-]+", source):
            raise ValueError(
                f"camera {camera_id} source must reference a Secret under /run/secrets/apexfabric with file:"
            )
        secret_path = Path(source.removeprefix("file:").strip())
        if not str(secret_path):
            raise ValueError(f"camera {camera_id} Secret path is empty")
        if secret_path.suffix != ".url":
            raise ValueError(f"camera {camera_id} Secret file must end in .url")
        try:
            resolved = secret_path.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(f"camera {camera_id} Secret file is missing or unreadable") from exc
        if resolved != self.secrets_root and self.secrets_root not in resolved.parents:
            raise ValueError(f"camera {camera_id} Secret must be under {self.secrets_root}")
        try:
            value = resolved.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"camera {camera_id} Secret file is missing or unreadable") from exc
        if not value:
            raise ValueError(f"camera {camera_id} Secret is empty")
        if not value.startswith(STREAM_SCHEMES):
            raise ValueError(
                f"camera {camera_id} Secret must contain an rtsp, rtsps, http, or https stream URL"
            )

    def _validate_geometry_config(self, camera_id: str, apps: set[str], config: dict[str, Any]) -> None:
        unknown = set(config) - {"embedding", "emission", "zones"}
        if unknown:
            raise ValueError(f"camera {camera_id} config has unknown fields: {sorted(unknown)}")
        missing = {"embedding", "emission", "zones"} - set(config)
        if missing:
            raise ValueError(f"camera {camera_id} config is missing fields: {sorted(missing)}")
        self._validate_embedding(camera_id, config["embedding"])
        self._validate_emission(camera_id, config["emission"])
        zones = config.get("zones") or {}
        if not isinstance(zones, dict):
            raise ValueError(f"camera {camera_id} zones must be an object")
        unknown_zones = set(zones) - ZONE_APP_KEYS
        if unknown_zones:
            raise ValueError(f"camera {camera_id} zones has unknown apps: {sorted(unknown_zones)}")
        for app_key, items in zones.items():
            if not isinstance(items, list) or not items:
                raise ValueError(f"camera {camera_id} zones.{app_key} must be a non-empty array")
            for index, item in enumerate(items):
                self._validate_zone(camera_id, f"zones.{app_key}[{index}]", item)

    @staticmethod
    def _validate_embedding(camera_id: str, embedding: Any) -> None:
        if not isinstance(embedding, dict) or set(embedding) != {"model_id", "dimensions"}:
            raise ValueError(f"camera {camera_id} embedding must contain only model_id and dimensions")
        if embedding["model_id"] != FACE_MODEL_ID:
            raise ValueError(f"camera {camera_id} embedding model_id must be {FACE_MODEL_ID}")
        if embedding["dimensions"] != FACE_EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"camera {camera_id} embedding dimensions must be {FACE_EMBEDDING_DIMENSIONS}"
            )

    @staticmethod
    def _validate_emission(camera_id: str, emission: Any) -> None:
        fields = {"minimum_quality", "cooldown_seconds", "material_change_threshold"}
        if not isinstance(emission, dict) or set(emission) != fields:
            raise ValueError(f"camera {camera_id} emission must contain exactly {sorted(fields)}")
        limits = {
            "minimum_quality": (0.0, 1.0),
            "cooldown_seconds": (0.0, 3600.0),
            "material_change_threshold": (0.0, 1.0),
        }
        for field, (minimum, maximum) in limits.items():
            value = emission[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
                raise ValueError(
                    f"camera {camera_id} emission.{field} must be between {minimum:g} and {maximum:g}"
                )

    def _validate_zone(self, camera_id: str, field: str, zone: Any) -> None:
        if not isinstance(zone, dict):
            raise ValueError(f"camera {camera_id} {field} must be an object")
        unknown = set(zone) - {"id", "name", "poly"}
        if unknown:
            raise ValueError(f"camera {camera_id} {field} has unknown fields: {sorted(unknown)}")
        missing = {"id", "name", "poly"} - set(zone)
        if missing:
            raise ValueError(f"camera {camera_id} {field} is missing fields: {sorted(missing)}")
        if not isinstance(zone["id"], str) or not zone["id"].strip():
            raise ValueError(f"camera {camera_id} {field} id must be a non-empty string")
        if not isinstance(zone["name"], str) or not zone["name"].strip():
            raise ValueError(f"camera {camera_id} {field} name must be a non-empty string")
        poly = zone.get("poly")
        if not isinstance(poly, list) or len(poly) != 4:
            raise ValueError(f"camera {camera_id} {field} poly must contain exactly four [x, y] points")
        points = []
        for point_index, point in enumerate(poly):
            self._validate_point(camera_id, f"{field} point {point_index}", point)
            points.append([float(point[0]), float(point[1])])
        if _zero_area(points):
            raise ValueError(f"camera {camera_id} {field} poly has zero area")
        if _self_intersecting(points):
            raise ValueError(f"camera {camera_id} {field} poly is self-intersecting")

    @staticmethod
    def _validate_point(camera_id: str, field: str, point: Any) -> None:
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in point)
            or any(value < 0 or value > 1 for value in point)
        ):
            raise ValueError(f"camera {camera_id} {field} must be [x, y] normalized from 0 to 1")


def _canonical_app(app: str) -> str:
    return APP_ALIASES.get(app, app)


def _camera_id(camera: dict[str, Any]) -> str:
    return str(camera.get("camera_id") or camera.get("id"))


def _zero_area(points: list[list[float]]) -> bool:
    area = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return abs(area / 2.0) < 1e-9


def _self_intersecting(points: list[list[float]]) -> bool:
    """True when a four-point polygon's non-adjacent edges cross properly."""
    edges = [(points[i], points[(i + 1) % len(points)]) for i in range(len(points))]
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            if i == (j + 1) % len(edges) or j == (i + 1) % len(edges):
                continue  # adjacent edges share an endpoint; not an intersection
            if _segments_properly_intersect(edges[i], edges[j]):
                return True
    return False


def _segments_properly_intersect(a: list[list[float]], b: list[list[float]]) -> bool:
    p1, p2 = a
    p3, p4 = b

    def _orientation(o, p, q):
        value = (p[1] - o[1]) * (q[0] - p[0]) - (p[0] - o[0]) * (q[1] - p[1])
        if abs(value) < 1e-12:
            return 0
        return 1 if value > 0 else -1

    d1 = _orientation(p3, p4, p1)
    d2 = _orientation(p3, p4, p2)
    d3 = _orientation(p1, p2, p3)
    d4 = _orientation(p1, p2, p4)
    if d1 == 0 or d2 == 0 or d3 == 0 or d4 == 0:
        return False
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))
