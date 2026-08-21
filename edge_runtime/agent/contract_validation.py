"""Strict ApexFabric V1 desired-state validation."""
from __future__ import annotations

import json
from pathlib import Path

from edge_runtime.graph.manifest_loader import ManifestRepository


class ApexFabricV1DesiredStateValidator:
    """Validates one solution image without resolving or exposing Secret values."""

    _ROOT_FIELDS = {"edge_id", "revision", "cameras"}
    _CAMERA_FIELDS = {"camera_id", "source", "solution_pack", "fps", "apps", "config"}
    _TRAFFIC_LINE_APPS = {"wrong_way", "vehicle_counting", "pedestrian_counting"}
    _TRAFFIC_REQUIRED_LINE_APPS = {"wrong_way"}
    _TRAFFIC_ZONE_APPS = {"illegal_parking"}
    _TRAFFIC_ALLOWED_ZONE_APPS = {"anpr", "illegal_parking"}
    _SURVEILLANCE_LINE_APPS = {"people_counting"}
    _SURVEILLANCE_ZONE_APPS = {"intrusion"}

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
        if self._solution_pack == "traffic":
            self._validate_traffic_config(camera_id, apps, config)
        elif self._solution_pack == "surveillance":
            self._validate_surveillance_config(camera_id, apps, config)
        self._validate_secret_reference(camera_id, camera["source"])

    def _validate_surveillance_config(self, camera_id: str, apps: list[str], config: dict) -> None:
        unknown = set(config) - {"lines", "zones"}
        if unknown:
            raise ValueError(
                f"camera {camera_id} surveillance config has unknown fields: {sorted(unknown)}"
            )
        lines = config.get("lines") or {}
        zones = config.get("zones") or {}
        if not isinstance(lines, dict):
            raise ValueError(f"camera {camera_id} config.lines must be an object")
        if not isinstance(zones, dict):
            raise ValueError(f"camera {camera_id} config.zones must be an object")
        if set(lines) - self._SURVEILLANCE_LINE_APPS:
            raise ValueError(f"camera {camera_id} config.lines only supports people_counting")
        if set(zones) - self._SURVEILLANCE_ZONE_APPS:
            raise ValueError(f"camera {camera_id} config.zones only supports intrusion")

        if "people_counting" in lines and not lines["people_counting"]:
            raise ValueError(
                f"camera {camera_id} config.lines.people_counting must be a non-empty array"
            )
        if "intrusion" in zones and not zones["intrusion"]:
            raise ValueError(f"camera {camera_id} config.zones.intrusion must be a non-empty array")
        for index, line in enumerate(lines.get("people_counting") or []):
            self._validate_surveillance_counting_line(camera_id, index, line)
        for index, zone in enumerate(zones.get("intrusion") or []):
            self._validate_zone(camera_id, "intrusion", index, zone)
        if "intrusion" in apps and not zones.get("intrusion"):
            raise ValueError(f"camera {camera_id} app intrusion requires config.zones.intrusion")

    def _validate_surveillance_counting_line(self, camera_id: str, index: int, line) -> None:
        field = f"config.lines.people_counting[{index}]"
        if not isinstance(line, dict):
            raise ValueError(f"camera {camera_id} {field} must be an object")
        unknown = set(line) - {"name", "a", "b", "in_side"}
        if unknown:
            raise ValueError(f"camera {camera_id} {field} has unknown fields: {sorted(unknown)}")
        if not isinstance(line.get("name"), str) or not line["name"].strip():
            raise ValueError(f"camera {camera_id} {field}.name is required")
        self._validate_point(camera_id, f"{field}.a", line.get("a"))
        self._validate_point(camera_id, f"{field}.b", line.get("b"))
        if line.get("in_side", "right") not in {"left", "right"}:
            raise ValueError(f"camera {camera_id} {field}.in_side must be left or right")

    def _validate_traffic_config(self, camera_id: str, apps: list[str], config: dict) -> None:
        unknown = set(config) - {"lines", "zones"}
        if unknown:
            raise ValueError(f"camera {camera_id} traffic config has unknown fields: {sorted(unknown)}")

        lines = config.get("lines") or {}
        zones = config.get("zones") or {}
        if not isinstance(lines, dict):
            raise ValueError(f"camera {camera_id} config.lines must be an object")
        if not isinstance(zones, dict):
            raise ValueError(f"camera {camera_id} config.zones must be an object")
        unknown_lines = set(lines) - self._TRAFFIC_LINE_APPS
        unknown_zones = set(zones) - self._TRAFFIC_ALLOWED_ZONE_APPS
        if unknown_lines:
            raise ValueError(f"camera {camera_id} config.lines has unknown apps: {sorted(unknown_lines)}")
        if unknown_zones:
            raise ValueError(f"camera {camera_id} config.zones has unknown apps: {sorted(unknown_zones)}")

        for app, app_lines in lines.items():
            if not isinstance(app_lines, list) or not app_lines:
                raise ValueError(f"camera {camera_id} config.lines.{app} must be a non-empty array")
            for index, line in enumerate(app_lines):
                self._validate_line(camera_id, app, index, line)
        for app, app_zones in zones.items():
            if not isinstance(app_zones, list) or not app_zones:
                raise ValueError(f"camera {camera_id} config.zones.{app} must be a non-empty array")
            for index, zone in enumerate(app_zones):
                self._validate_zone(camera_id, app, index, zone)

        for app in self._TRAFFIC_REQUIRED_LINE_APPS & set(apps):
            app_lines = lines.get(app)
            if not isinstance(app_lines, list) or not app_lines:
                raise ValueError(f"camera {camera_id} app {app} requires config.lines.{app}")
        for app in self._TRAFFIC_ZONE_APPS & set(apps):
            app_zones = zones.get(app)
            if not isinstance(app_zones, list) or not app_zones:
                raise ValueError(f"camera {camera_id} app {app} requires config.zones.{app}")

    def _validate_line(self, camera_id: str, app: str, index: int, line) -> None:
        if not isinstance(line, dict):
            raise ValueError(f"camera {camera_id} config.lines.{app}[{index}] must be an object")
        unknown = set(line) - {"id", "name", "a", "b", "direction"}
        if unknown:
            raise ValueError(
                f"camera {camera_id} config.lines.{app}[{index}] has unknown fields: {sorted(unknown)}"
            )
        if not isinstance(line.get("name"), str) or not line["name"].strip():
            raise ValueError(f"camera {camera_id} config.lines.{app}[{index}].name is required")
        self._validate_point(camera_id, f"config.lines.{app}[{index}].a", line.get("a"))
        self._validate_point(camera_id, f"config.lines.{app}[{index}].b", line.get("b"))
        if "direction" in line and line["direction"] not in {"a_to_b", "b_to_a", "both"}:
            raise ValueError(
                f"camera {camera_id} config.lines.{app}[{index}].direction must be a_to_b, b_to_a, or both"
            )

    def _validate_zone(self, camera_id: str, app: str, index: int, zone) -> None:
        if not isinstance(zone, dict):
            raise ValueError(f"camera {camera_id} config.zones.{app}[{index}] must be an object")
        unknown = set(zone) - {"id", "name", "poly"}
        if unknown:
            raise ValueError(
                f"camera {camera_id} config.zones.{app}[{index}] has unknown fields: {sorted(unknown)}"
            )
        if not isinstance(zone.get("name"), str) or not zone["name"].strip():
            raise ValueError(f"camera {camera_id} config.zones.{app}[{index}].name is required")
        poly = zone.get("poly")
        if not isinstance(poly, list) or len(poly) < 3:
            raise ValueError(f"camera {camera_id} config.zones.{app}[{index}].poly needs at least 3 points")
        for point_index, point in enumerate(poly):
            self._validate_point(camera_id, f"config.zones.{app}[{index}].poly[{point_index}]", point)

    @staticmethod
    def _validate_point(camera_id: str, field: str, point) -> None:
        if (
            not isinstance(point, list)
            or len(point) != 2
            or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in point)
            or any(value < 0 or value > 1 for value in point)
        ):
            raise ValueError(f"camera {camera_id} {field} must be [x, y] normalized from 0 to 1")

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
