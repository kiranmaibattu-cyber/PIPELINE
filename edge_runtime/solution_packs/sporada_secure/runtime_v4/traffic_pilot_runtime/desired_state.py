from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTRACT = "v4"
SOLUTION_PACK = "sentinel-cv-runtime"
MAX_CAMERAS = 8
STREAM_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://")
OUTPUTS = {"observations", "evidence", "face_embeddings", "body_embeddings", "gait_embeddings", "scene_embeddings"}
EVENT_TYPES = {"object_present", "person_count", "vehicle_count", "dwell", "threshold_exceeded", "plate_read", "line_cross", "zone_entry", "zone_exit", "scene_sample", "fire_smoke_suspected", "camera_health"}
PROFILE_KIND = {"adaface-ir101-v18.1": "face", "transreid-ssl-v18.1": "body", "gaitbase-v18.1": "gait", "siglip2-base-v1": "scene"}
OUTPUT_PROFILE_KIND = {"face_embeddings": "face", "body_embeddings": "body", "gait_embeddings": "gait", "scene_embeddings": "scene"}


@dataclass(frozen=True)
class DesiredCamera:
    camera_id: str
    source: str
    apps: tuple[str, ...]
    fps: float = 10.0
    config: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    outputs: tuple[str, ...] = ()
    enabled_event_types: tuple[str, ...] = ()


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

    def load(self, path: Path) -> DesiredState:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError as exc:
            raise ValueError(f"desired-state file not found: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"desired-state file is invalid: {exc}") from exc
        self.validate(data)
        return DesiredState(
            edge_id=data["edge_id"], deployment_id=data["deployment_id"],
            revision=data["revision"],
            cameras=tuple(self._camera(item, data) for item in data["cameras"]),
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )

    def validate(self, data: Any) -> None:
        allowed = {"contract", "edge_id", "deployment_id", "revision", "cameras"}
        self._fields(data, allowed, allowed, "desired state")
        if data["contract"] != CONTRACT:
            raise ValueError(f"contract must be {CONTRACT}")
        for field in ("edge_id", "deployment_id"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise ValueError(f"{field} must be a non-empty string")
        if not isinstance(data["revision"], int) or isinstance(data["revision"], bool) or data["revision"] < 1:
            raise ValueError("revision must be a positive integer")
        cameras = data["cameras"]
        if not isinstance(cameras, list) or not 1 <= len(cameras) <= MAX_CAMERAS:
            raise ValueError(f"cameras must contain 1 to {MAX_CAMERAS} entries")
        camera_ids: set[str] = set()
        issued_ids: set[str] = set()
        for camera in cameras:
            self._validate_camera(camera, camera_ids, issued_ids)

    def _validate_camera(self, camera: Any, camera_ids: set[str], issued_ids: set[str]) -> None:
        required = {"camera_id", "source", "fps", "outputs", "enabled_event_types", "zones", "thresholds", "embedding_profile_ids"}
        self._fields(camera, required, required | {"minimum_quality", "evidence_cooldown_seconds"}, "camera")
        camera_id = camera["camera_id"]
        if not isinstance(camera_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", camera_id):
            raise ValueError("camera_id contains invalid characters")
        if camera_id in camera_ids:
            raise ValueError(f"duplicate camera_id: {camera_id}")
        camera_ids.add(camera_id)
        fps = camera["fps"]
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 0 < fps <= 60:
            raise ValueError(f"camera {camera_id} fps must be greater than zero and at most 60")
        outputs = self._enum_array(camera["outputs"], OUTPUTS, f"camera {camera_id} outputs")
        events = self._enum_array(camera["enabled_event_types"], EVENT_TYPES, f"camera {camera_id} enabled_event_types")
        if "observations" not in outputs or not {"object_present", "camera_health"} <= set(events):
            raise ValueError(f"camera {camera_id} must enable observations, object_present, and camera_health")
        if set(outputs) & set(OUTPUT_PROFILE_KIND) and "evidence" not in outputs:
            raise ValueError(f"camera {camera_id} embedding outputs require evidence")
        if set(events) & {"scene_sample", "fire_smoke_suspected"} and "evidence" not in outputs:
            raise ValueError(f"camera {camera_id} scene/fire events require evidence")
        profiles = self._enum_array(camera["embedding_profile_ids"], set(PROFILE_KIND), f"camera {camera_id} embedding_profile_ids", True)
        kinds = {PROFILE_KIND[item] for item in profiles}
        for output, kind in OUTPUT_PROFILE_KIND.items():
            if output in outputs and kind not in kinds:
                raise ValueError(f"camera {camera_id} {output} requires a {kind} profile")
        required_profiles = {
            profile for profile, kind in PROFILE_KIND.items()
            if f"{kind}_embeddings" in outputs
        }
        if set(profiles) != required_profiles:
            raise ValueError(f"camera {camera_id} embedding profiles must exactly match enabled embedding outputs")
        zone_ids: set[str] = set()
        for zone in camera["zones"]:
            self._validate_zone(zone, zone_ids, issued_ids, camera_id)
        threshold_ids: set[str] = set()
        for threshold in camera["thresholds"]:
            self._validate_threshold(threshold, threshold_ids, issued_ids, zone_ids, camera_id)
        self._range(camera, "minimum_quality", 0, 1, camera_id)
        self._range(camera, "evidence_cooldown_seconds", 0, 3600, camera_id)
        self._validate_source(camera_id, camera["source"])

    def _validate_zone(self, zone, local_ids, issued_ids, camera_id):
        if not isinstance(zone, dict):
            raise ValueError(f"camera {camera_id} zone must be an object")
        kind = zone.get("kind")
        required = {"id", "name", "kind", "points"} | ({"forward_means"} if kind == "line" else set())
        self._fields(zone, required, required, f"camera {camera_id} zone")
        self._uuid(zone["id"], "zone id")
        if zone["id"] in local_ids or zone["id"] in issued_ids:
            raise ValueError(f"duplicate zone id: {zone['id']}")
        local_ids.add(zone["id"]); issued_ids.add(zone["id"])
        if not isinstance(zone["name"], str) or not zone["name"]:
            raise ValueError("zone name must be non-empty")
        if kind not in {"polygon", "line"}:
            raise ValueError("zone kind must be polygon or line")
        points = zone["points"]
        valid_count = isinstance(points, list) and (len(points) == 2 if kind == "line" else len(points) >= 3)
        if not valid_count:
            raise ValueError(f"{kind} has invalid point count")
        for point in points:
            if not isinstance(point, list) or len(point) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in point):
                raise ValueError("zone points must be normalized [x,y] pairs")
        if kind == "line" and zone["forward_means"] not in {"entry", "exit", "neither"}:
            raise ValueError("line forward_means is invalid")

    def _validate_threshold(self, item, local_ids, issued_ids, zone_ids, camera_id):
        required = {"id", "metric", "zone_id", "value", "direction"}
        self._fields(item, required, required, f"camera {camera_id} threshold")
        self._uuid(item["id"], "threshold id")
        if item["id"] in local_ids or item["id"] in issued_ids:
            raise ValueError(f"duplicate threshold id: {item['id']}")
        local_ids.add(item["id"]); issued_ids.add(item["id"])
        if item["zone_id"] not in zone_ids:
            raise ValueError("threshold zone_id must reference a camera zone")
        if item["metric"] not in {"person_count", "vehicle_count", "dwell_seconds"} or item["direction"] not in {"over", "under"}:
            raise ValueError("threshold metric or direction is invalid")
        if isinstance(item["value"], bool) or not isinstance(item["value"], (int, float)) or item["value"] < 0:
            raise ValueError("threshold value must be non-negative")
        if item["metric"] in {"person_count", "vehicle_count"} and not isinstance(item["value"], int):
            raise ValueError("count threshold value must be an integer")

    def _validate_source(self, camera_id: str, value: Any) -> None:
        if not isinstance(value, str) or not value.startswith("file:"):
            raise ValueError(f"camera {camera_id} source must be a file Secret reference")
        try:
            resolved = Path(value.removeprefix("file:")).resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"camera {camera_id} Secret is missing") from exc
        if self.secrets_root not in resolved.parents or resolved.name != f"{camera_id}.url":
            raise ValueError(f"camera {camera_id} Secret path/name is invalid")
        if not resolved.read_text(encoding="utf-8").strip().startswith(STREAM_SCHEMES):
            raise ValueError(f"camera {camera_id} Secret must contain rtsp, rtsps, http, or https")

    def _camera(self, item: dict[str, Any], root: dict[str, Any]) -> DesiredCamera:
        events, outputs = tuple(item["enabled_event_types"]), tuple(item["outputs"])
        return DesiredCamera(camera_id=item["camera_id"], name=item["camera_id"], source=item["source"], fps=float(item["fps"]), apps=_internal_apps(events, outputs), outputs=outputs, enabled_event_types=events, config=_internal_config(item, root))

    @staticmethod
    def _fields(value, required, allowed, label):
        if not isinstance(value, dict): raise ValueError(f"{label} must be an object")
        if required - set(value): raise ValueError(f"{label} is missing fields: {sorted(required - set(value))}")
        if set(value) - allowed: raise ValueError(f"{label} has unknown fields: {sorted(set(value) - allowed)}")

    @staticmethod
    def _enum_array(value, allowed, label, allow_empty=False):
        if not isinstance(value, list) or (not value and not allow_empty) or len(value) != len(set(value)):
            raise ValueError(f"{label} must be a unique array")
        if set(value) - set(allowed): raise ValueError(f"{label} contains unsupported values: {sorted(set(value) - set(allowed))}")
        return value

    @staticmethod
    def _uuid(value, label):
        try: uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc: raise ValueError(f"{label} must be a UUID") from exc

    @staticmethod
    def _range(item, field, minimum, maximum, camera_id):
        if field not in item: return
        value = item[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise ValueError(f"camera {camera_id} {field} is outside its allowed range")


def _internal_apps(events: tuple[str, ...], outputs: tuple[str, ...]) -> tuple[str, ...]:
    apps = {"v4_events"}
    mapping = {"person_count": "pedestrian_counting", "vehicle_count": "vehicle_counting", "plate_read": "plate_detection", "fire_smoke_suspected": "fire_smoke_detection", "scene_sample": "scene_embeddings"}
    apps.update(mapping[event] for event in events if event in mapping)
    if set(outputs) & {"face_embeddings", "body_embeddings", "gait_embeddings"}: apps.add("person_reid")
    if "scene_embeddings" in outputs: apps.add("scene_embeddings")
    return tuple(sorted(apps))


def _internal_config(camera: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    polygons = [z for z in camera["zones"] if z["kind"] == "polygon"]
    lines = [z for z in camera["zones"] if z["kind"] == "line"]
    zone_items = [{"id": z["id"], "name": z["name"], "poly": z["points"]} for z in polygons]
    events, outputs = set(camera["enabled_event_types"]), set(camera["outputs"])
    config: dict[str, Any] = {
        "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
        "emission": {"minimum_quality": float(camera.get("minimum_quality", .2)), "preferred_quality": .65, "selection_window_seconds": 1.5, "cooldown_seconds": float(camera.get("evidence_cooldown_seconds", 30)), "material_change_threshold": .15},
        "zones": {},
        "v4": {"deployment_id": root["deployment_id"], "config_revision": root["revision"], "outputs": list(camera["outputs"]), "enabled_event_types": list(camera["enabled_event_types"]), "zones": camera["zones"], "thresholds": camera["thresholds"], "embedding_profile_ids": camera["embedding_profile_ids"], "minimum_quality": float(camera.get("minimum_quality", .2)), "evidence_cooldown_seconds": float(camera.get("evidence_cooldown_seconds", 30))},
    }
    for event, app in {"vehicle_count": "vehicle_counting", "person_count": "pedestrian_counting", "plate_read": "anpr", "fire_smoke_suspected": "fire_smoke_detection", "scene_sample": "scene_embeddings"}.items():
        if event in events: config["zones"][app] = list(zone_items)
    if outputs & {"face_embeddings", "body_embeddings", "gait_embeddings"}:
        config["zones"]["person_reid"] = list(zone_items)
        config["reid_embedding"] = {
            "body": {"model_id": "transreid_ssl_int8", "model_version": "v1", "embedding_space": "transreid_ssl_int8@v1:l2:384", "dimensions": 384},
            "face": {"model_id": "face-embedding-model-v1", "model_version": "adaface_ir101_int8-v1", "embedding_space": "adaface-ir101-int8@v1:aligned112:l2:512", "dimensions": 512},
            "gait": {"model_id": "gaitbase_int8", "model_version": "v1", "embedding_space": "gaitbase_int8@v1:30x64x44:parts16x256:l2:4096", "dimensions": 4096},
        }
        config["reid_emission"] = {"interval_seconds": 5, "jpeg_quality": 88, "silhouette_interval_frames": 3, "minimum_body_quality": float(camera.get("minimum_quality", .2))}
        config["gait"] = {"silhouette_source": "background_subtraction", "minimum_sequence_frames": 20, "model_sequence_frames": 30, "maximum_buffer_frames": 60, "minimum_motion_pixels": 2}
        config["reassociation"] = {"maximum_gap_seconds": 8, "minimum_body_similarity": .78, "maximum_center_distance_pixels": 320}
    if "scene_embeddings" in outputs or "scene_sample" in events:
        config["scene_embedding"] = {"model_id": "google/siglip2-base-patch16-224", "model_version": "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2", "embedding_space": "google/siglip2-base-patch16-224@75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2:image-text:l2:768", "dimensions": 768}
        config["scene_emission"] = {"interval_seconds": 10, "jpeg_quality": 88}
    config["counting_lines"] = {"vehicle_entry_exit_counts": [{"id": z["id"], "name": z["name"], "a": z["points"][0], "b": z["points"][1], "direction_mapping": {"right_to_left": "in", "left_to_right": "out"}, "forward_means": z["forward_means"]} for z in lines]}
    return config
