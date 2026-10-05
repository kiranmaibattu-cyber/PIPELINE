"""Contract-v4 typed event production from tracked detections."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Any
import uuid

import cv2

from .sentinel_delivery import SentinelV2Outbox, SentinelV2Uploader


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


class V4EventPipeline:
    """Turns positive detector/tracker facts into closed V4 event variants."""

    def __init__(self, camera_id: str, camera_config: dict[str, Any]) -> None:
        self.camera_id = camera_id
        config = (camera_config.get("analytics") or {}).get("v4_events") or {}
        self.config = config.get("v4") or {}
        self.enabled = set(self.config.get("enabled_event_types") or [])
        self.zones = list(self.config.get("zones") or [])
        self.polygons = [item for item in self.zones if item.get("kind") == "polygon"]
        self.lines = [item for item in self.zones if item.get("kind") == "line"]
        self.thresholds = list(self.config.get("thresholds") or [])
        self.session_id = str(uuid.uuid4())
        self.first_seen: dict[str, float] = {}
        self.last_object: dict[str, float] = {}
        self.inside: dict[tuple[str, str], bool] = {}
        self.entered_at: dict[tuple[str, str], float] = {}
        self.last_dwell: dict[tuple[str, str], float] = {}
        self.line_side: dict[tuple[str, str], float] = {}
        self.threshold_active: dict[tuple[str, str], bool] = {}
        self.last_counts = 0.0
        self.reported_plates: set[tuple[str, str]] = set()
        self.last_hazard: dict[str, float] = {}
        self.health_reported = False
        self.store = SentinelV2Outbox(
            Path(os.getenv("APEXFABRIC_STATE_ROOT", "/state")), camera_id
        )
        self.uploader = None
        base_url = os.getenv("SENTINEL_INGEST_BASE_URL", "").strip()
        token = os.getenv("SENTINEL_EDGE_TOKEN", "").strip()
        if base_url and token:
            self.uploader = SentinelV2Uploader(self.store, base_url, token)
            self.uploader.start()

    def close(self) -> None:
        if self.uploader is not None:
            self.uploader.stop()

    def process(self, packet: Any) -> int:
        now = time.time()
        emitted = 0
        if "camera_health" in self.enabled and not self.health_reported:
            self._persist(self._event("camera_health", now, stream_session_id=self.session_id,
                                      as_of=utc(now), state="healthy", reason="none"))
            self.health_reported = True
            emitted += 1
        height, width = packet.frame.shape[:2]
        tracked = [
            item for item in packet.detections
            if item.model_name == "vehicle" and item.metadata.get("track_id") is not None
        ]
        occupancy = {zone["id"]: {"person": 0, "vehicle": 0} for zone in self.polygons}
        dwell_values: list[tuple[str, str, str, float]] = []
        for detection in tracked:
            track_id = str(detection.metadata["track_id"])
            object_type = "person" if detection.class_name == "pedestrian" else "vehicle"
            key = f"{object_type}:{track_id}"
            self.first_seen.setdefault(key, now)
            point = self._anchor(detection.bbox, width, height)
            containing = [zone for zone in self.polygons if self._inside(point, zone["points"])]
            for zone in containing:
                occupancy[zone["id"]][object_type] += 1

            if "object_present" in self.enabled:
                cooldown = float(self.config.get("evidence_cooldown_seconds", 30))
                if now - self.last_object.get(key, 0) >= max(1.0, cooldown):
                    self.last_object[key] = now
                    self._persist(self._event(
                        "object_present", now, stream_session_id=self.session_id,
                        track_id=track_id, object_type=object_type,
                        vehicle_type=self._vehicle_type(detection.class_name),
                        bbox_normalized=self._bbox(detection.bbox, width, height),
                        confidence=max(0.0, min(1.0, float(detection.confidence))),
                        first_seen_at=utc(self.first_seen[key]),
                        zone_id=containing[0]["id"] if containing else None,
                    ))
                    emitted += 1

            for zone in self.polygons:
                state_key = (key, zone["id"])
                current = zone in containing
                previous = self.inside.get(state_key)
                self.inside[state_key] = current
                if previous is False and current and "zone_entry" in self.enabled:
                    self._persist(self._transition("zone_entry", now, track_id, object_type, zone["id"]))
                    emitted += 1
                if current and not previous:
                    self.entered_at[state_key] = now
                if previous is True and not current:
                    if "zone_exit" in self.enabled:
                        self._persist(self._transition("zone_exit", now, track_id, object_type, zone["id"]))
                        emitted += 1
                    started = self.entered_at.pop(state_key, None)
                    if started is not None and "dwell" in self.enabled:
                        self._persist(self._dwell(now, track_id, object_type, zone["id"], started, "completed"))
                        emitted += 1
                if current and "dwell" in self.enabled:
                    started = self.entered_at.setdefault(state_key, now)
                    dwell_values.append((track_id, object_type, zone["id"], now - started))
                    if now - self.last_dwell.get(state_key, 0) >= 5:
                        self.last_dwell[state_key] = now
                        self._persist(self._dwell(now, track_id, object_type, zone["id"], started, "ongoing"))
                        emitted += 1

            if "line_cross" in self.enabled:
                for line in self.lines:
                    line_key = (key, line["id"])
                    side = self._line_side(point, line["points"])
                    previous = self.line_side.get(line_key)
                    self.line_side[line_key] = side
                    if previous is not None and previous * side < 0:
                        direction = "forward" if previous < side else "reverse"
                        self._persist(self._event(
                            "line_cross", now, stream_session_id=self.session_id,
                            track_id=track_id, object_type=object_type,
                            zone_id=line["id"], direction=direction, crossed_at=utc(now),
                        ))
                        emitted += 1

        if now - self.last_counts >= 1:
            self.last_counts = now
            for zone in self.polygons:
                values = occupancy[zone["id"]]
                for event_type, kind in (("person_count", "person"), ("vehicle_count", "vehicle")):
                    if event_type in self.enabled:
                        self._persist(self._event(
                            event_type, now, stream_session_id=self.session_id,
                            zone_id=zone["id"], count=values[kind], as_of=utc(now),
                            count_semantics="visible_now", counting_method="frame_snapshot",
                            counting_method_note="Current tracked detections whose bottom-center lies in the polygon.",
                            coverage_state="complete", entries_since_session_start=None,
                            exits_since_session_start=None,
                        ))
                        emitted += 1
                emitted += self._threshold_events(zone["id"], values, dwell_values, now)

        emitted += self._plate_events(packet, width, height, now)
        emitted += self._hazard_events(packet, now)
        return emitted

    def _plate_events(self, packet, width, height, now):
        if "plate_read" not in self.enabled:
            return 0
        emitted = 0
        vehicles = {str(item.metadata.get("track_id")): item for item in packet.detections
                    if item.model_name == "vehicle" and item.metadata.get("track_id") is not None}
        for plate in packet.detections:
            if plate.model_name != "license_plate":
                continue
            text = re.sub(r"[^A-Z0-9 -]", "", str(plate.metadata.get("ocr_text") or "").upper()).strip()
            if not text:
                continue
            track_id = str(plate.parent_id) if plate.parent_id is not None else ""
            key = (track_id, text)
            if key in self.reported_plates:
                continue
            self.reported_plates.add(key)
            parent = vehicles.get(track_id)
            fields = dict(stream_session_id=self.session_id, plate_text=text,
                          confidence=max(0.0, min(1.0, float(plate.confidence))), partial=False,
                          vehicle_type=self._vehicle_type(parent.class_name if parent else "other"),
                          bbox_normalized=self._bbox(plate.bbox, width, height))
            if track_id:
                fields["track_id"] = track_id
            self._persist(self._event("plate_read", now, **fields))
            emitted += 1
        return emitted

    def _hazard_events(self, packet, now):
        if "fire_smoke_suspected" not in self.enabled:
            return 0
        emitted = 0
        for detection in packet.detections:
            if detection.model_name != "smoke_fire" or detection.class_name not in {"fire", "smoke"}:
                continue
            if now - self.last_hazard.get(detection.class_name, 0) < 10:
                continue
            self.last_hazard[detection.class_name] = now
            ok, encoded = cv2.imencode(".jpg", packet.frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            if not ok:
                continue
            body = encoded.tobytes()
            observation_id, evidence_id = str(uuid.uuid4()), str(uuid.uuid4())
            event = self._event("fire_smoke_suspected", now, observation_id=observation_id,
                                stream_session_id=self.session_id, hazard=detection.class_name,
                                score=max(0.0, min(1.0, float(detection.confidence))),
                                evidence_id=evidence_id)
            evidence = {"schema_version": "4.0", "evidence_id": evidence_id,
                        "observation_id": observation_id, "evidence_type": "frame",
                        "evidence_role": "context", "captured_at": utc(now),
                        "content_type": "image/jpeg", "width": int(packet.frame.shape[1]),
                        "height": int(packet.frame.shape[0]), "size_bytes": len(body),
                        "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}
            self.store.persist(event, [(evidence, body)])
            emitted += 1
        return emitted

    def _threshold_events(self, zone_id, values, dwell_values, now):
        if "threshold_exceeded" not in self.enabled:
            return 0
        emitted = 0
        for threshold in self.thresholds:
            if threshold["zone_id"] != zone_id:
                continue
            metric = threshold["metric"]
            candidates = []
            if metric in {"person_count", "vehicle_count"}:
                candidates = [(None, None, float(values["person" if metric == "person_count" else "vehicle"]))]
            else:
                candidates = [(track, kind, value) for track, kind, zone, value in dwell_values if zone == zone_id]
            for track_id, object_type, value in candidates:
                active = value > threshold["value"] if threshold["direction"] == "over" else value < threshold["value"]
                key = (threshold["id"], track_id or "count")
                was_active = self.threshold_active.get(key, False)
                self.threshold_active[key] = active
                if not active or was_active:
                    continue
                fields = dict(stream_session_id=self.session_id, threshold_ref=threshold["id"],
                              metric=metric, zone_id=zone_id, threshold_value=threshold["value"],
                              observed_value=value, direction=threshold["direction"], as_of=utc(now))
                if metric == "dwell_seconds":
                    fields.update(track_id=track_id, object_type=object_type)
                else:
                    fields["threshold_value"] = int(threshold["value"])
                    fields["observed_value"] = int(value)
                self._persist(self._event("threshold_exceeded", now, **fields))
                emitted += 1
        return emitted

    def _event(self, event_type, now, observation_id=None, **fields):
        return {"schema_version": "4.0", "observation_id": observation_id or str(uuid.uuid4()),
                "deployment_id": self.config["deployment_id"], "camera_id": self.camera_id,
                "config_revision": int(self.config["config_revision"]), "event_type": event_type,
                "observed_at": utc(now), **fields}

    def _transition(self, event_type, now, track_id, object_type, zone_id):
        return self._event(event_type, now, stream_session_id=self.session_id,
                           track_id=track_id, object_type=object_type, zone_id=zone_id,
                           transition_at=utc(now))

    def _dwell(self, now, track_id, object_type, zone_id, started, status):
        return self._event("dwell", now, stream_session_id=self.session_id,
                           track_id=track_id, object_type=object_type, zone_id=zone_id,
                           started_at=utc(started), ended_at=utc(now) if status == "completed" else None,
                           status=status)

    def _persist(self, event):
        self.store.persist(event, [])

    @staticmethod
    def _anchor(bbox, width, height):
        return ((float(bbox[0]) + float(bbox[2])) / (2 * width), float(bbox[3]) / height)

    @staticmethod
    def _bbox(bbox, width, height):
        values = (bbox[0] / width, bbox[1] / height, bbox[2] / width, bbox[3] / height)
        return dict(zip(("x1", "y1", "x2", "y2"), [max(0.0, min(1.0, float(v))) for v in values]))

    @staticmethod
    def _inside(point, points):
        polygon = [(float(p[0] if isinstance(p, list) else p["x"]),
                    float(p[1] if isinstance(p, list) else p["y"])) for p in points]
        x, y, inside, previous = point[0], point[1], False, polygon[-1]
        for current in polygon:
            xi, yi = current; xj, yj = previous
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
                inside = not inside
            previous = current
        return inside

    @staticmethod
    def _line_side(point, points):
        a, b = points
        ax, ay = (a[0], a[1]) if isinstance(a, list) else (a["x"], a["y"])
        bx, by = (b[0], b[1]) if isinstance(b, list) else (b["x"], b["y"])
        return (bx - ax) * (point[1] - ay) - (by - ay) * (point[0] - ax)

    @staticmethod
    def _vehicle_type(name):
        return None if name == "pedestrian" else name if name in {"car", "truck", "van", "bus", "motorcycle"} else "other"
