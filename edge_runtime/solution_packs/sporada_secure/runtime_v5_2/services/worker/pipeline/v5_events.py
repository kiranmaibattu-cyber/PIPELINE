"""V5.2 camera-local state and boundary event production."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import nullcontext
import hashlib
import math
import os
from pathlib import Path
import re
import time
from time import perf_counter
from typing import Any
import uuid

import cv2
import numpy as np

from .face_samples import FaceSamplePipeline
from .sentinel_delivery import SentinelV2Outbox, SentinelV2Uploader


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def inside(point, points) -> bool:
    x, y = point
    result = False
    previous = points[-1]
    for current in points:
        ax, ay = current
        bx, by = previous
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / ((by - ay) or 1e-9) + ax:
            result = not result
        previous = current
    return result


def boundary_distance(point, points) -> float:
    px, py = point
    nearest = float("inf")
    for a, b in zip(points, points[1:] + points[:1]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length_squared = dx * dx + dy * dy
        fraction = max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / length_squared)) if length_squared else 0.0
        nearest = min(nearest, math.hypot(px - a[0] - fraction * dx,
                                         py - a[1] - fraction * dy))
    return nearest


def bbox_normalized(box, width, height):
    x1, y1, x2, y2 = box
    return {"x1": max(0.0, min(1.0, float(x1) / width)),
            "y1": max(0.0, min(1.0, float(y1) / height)),
            "x2": max(0.0, min(1.0, float(x2) / width)),
            "y2": max(0.0, min(1.0, float(y2) / height))}


class V5EventPipeline:
    def __init__(self, camera_id: str, config: dict[str, Any], *, scene=None, body=None,
                 gait=None, telemetry=None):
        self.camera_id = camera_id
        self.config = (config.get("analytics") or {}).get("v5_events", {}).get("v5") or {}
        self.bindings = list(self.config["bindings"])
        geometry = self.config.get("geometry") or {}
        self.zones = {item["id"]: item for item in geometry.get("zones") or []}
        self.lines = {item["id"]: item for item in geometry.get("counting_lines") or []}
        self.session_id = str(uuid.uuid4())
        self.scene, self.body, self.gait = scene, body, gait
        self.telemetry = telemetry
        self.tracks: dict[tuple[str, str], dict[str, Any]] = {}
        self.presences: dict[str, dict[str, Any]] = {}
        self.counts: dict[tuple[str, str | None], dict[str, Any]] = {}
        self.last_scene: dict[tuple[str, str | None], float] = {}
        self.scene_pressure_limit = int(os.getenv("SENTINEL_SCENE_MAX_PENDING_BYTES", str(1024 ** 3)))
        self.scene_pressure_checked_at = 0.0
        self.scene_pressure = False
        self.scene_gap_started_at: float | None = None
        self.scene_pressure_reported_at = 0.0
        self.hazards: dict[tuple[str, str | None], dict[str, Any]] = {}
        self.face_candidates: dict[str, dict[str, Any]] = {}
        self.face_selection_window = float(os.getenv("SENTINEL_FACE_SELECTION_WINDOW_SECONDS", "4.0"))
        self.zone_boundary_margin = float(os.getenv("SENTINEL_ZONE_BOUNDARY_MARGIN", "0.01"))
        self.candidate_max_gap = float(os.getenv("SENTINEL_CANDIDATE_MAX_GAP_SECONDS", "1.5"))
        self.hazard_min_confidence = float(os.getenv("SENTINEL_HAZARD_MIN_CONFIDENCE", "0.55"))
        self.feature_cooldown = float(os.getenv("SENTINEL_FEATURE_IMPROVEMENT_COOLDOWN_SECONDS", "5"))
        self.plate_outcome_delay = float(os.getenv("SENTINEL_PLATE_OUTCOME_DELAY_SECONDS", "3"))
        self.plate_confidence_improvement = float(os.getenv("SENTINEL_PLATE_CONFIDENCE_IMPROVEMENT", "0.10"))
        self.plate_update_cooldown = float(os.getenv("SENTINEL_PLATE_UPDATE_COOLDOWN_SECONDS", "5"))
        self.plate_change_min_confidence = float(os.getenv("SENTINEL_PLATE_CHANGE_MIN_CONFIDENCE", "0.90"))
        self.plate_change_margin = float(os.getenv("SENTINEL_PLATE_CHANGE_MARGIN", "0.10"))
        self.plate_low_confidence_replace = float(os.getenv("SENTINEL_PLATE_LOW_CONFIDENCE_REPLACE", "0.50"))
        self.plate_candidate_window = float(os.getenv("SENTINEL_PLATE_CANDIDATE_WINDOW_SECONDS", "2.0"))
        self.plate_emit_min_reads = int(os.getenv("SENTINEL_PLATE_EMIT_MIN_READS", "2"))
        self.plate_emit_min_confidence = float(os.getenv("SENTINEL_PLATE_EMIT_MIN_CONFIDENCE", "0.30"))
        self.plate_single_read_confidence = float(os.getenv("SENTINEL_PLATE_SINGLE_READ_CONFIDENCE", "0.75"))
        self.plate_fast_emit_confidence = float(os.getenv("SENTINEL_PLATE_FAST_EMIT_CONFIDENCE", "0.85"))
        self.presence_close_unobserved_after = float(os.getenv("SENTINEL_PRESENCE_UNOBSERVED_AFTER_SECONDS", "2"))
        self.presence_close_ended_after = float(os.getenv("SENTINEL_PRESENCE_ENDED_UNKNOWN_AFTER_SECONDS", "8"))
        self.presence_wide_unobserved_after = float(os.getenv("SENTINEL_WIDE_PRESENCE_UNOBSERVED_AFTER_SECONDS", "4.5"))
        self.presence_wide_ended_after = float(os.getenv("SENTINEL_WIDE_PRESENCE_ENDED_UNKNOWN_AFTER_SECONDS", "18"))
        self.vehicle_recovery_gap = float(os.getenv("SENTINEL_VEHICLE_RECOVERY_GAP_SECONDS", "12"))
        self.store = SentinelV2Outbox(Path(os.getenv("APEXFABRIC_STATE_ROOT", "/state")), camera_id)
        self.uploader = None
        base_url = os.getenv("SENTINEL_INGEST_BASE_URL", "").strip()
        token = os.getenv("SENTINEL_EDGE_TOKEN", "").strip()
        if base_url and token:
            self.uploader = SentinelV2Uploader(self.store, base_url, token)
            self.uploader.start()
        self.last_health = 0.0
        self.health_state = "healthy"
        self.last_reliable_at: float | None = None

    def close(self):
        if self.uploader is not None:
            self.uploader.stop()

    def reconfigure(self, camera_config: dict[str, Any]) -> None:
        updated = (camera_config.get("analytics") or {}).get("v5_events", {}).get("v5") or {}
        if int(updated.get("config_revision", 0)) <= int(self.config["config_revision"]):
            return
        now = time.time()
        for candidate in self.face_candidates.values():
            candidate["deadline"] = now
        self._flush_face_candidates(now)
        self.config = updated
        self.bindings = list(updated["bindings"])
        geometry = updated.get("geometry") or {}
        self.zones = {item["id"]: item for item in geometry.get("zones") or []}
        self.lines = {item["id"]: item for item in geometry.get("counting_lines") or []}
        binding_by_id = {item["use_case_id"]: item for item in self.bindings}
        for state in self.presences.values():
            owner = state["owner"]
            if owner is None:
                continue
            replacement = binding_by_id.get(owner["use_case_id"])
            if replacement is not None and replacement["app"] == owner["app"]:
                state["owner"] = replacement
                continue
            width_height = state.get("frame_size")
            if width_height is None:
                state["owner"] = None
                continue
            width, height = width_height
            box = state["box"]
            point = ((box[0] + box[2]) / (2 * width), box[3] / height)
            containing = {zone_id for zone_id, zone in self.zones.items() if inside(point, zone["poly"])}
            state["owner"] = self._owner(state["kind"], containing)
        enabled_ids = set(binding_by_id)
        self.counts = {key: value for key, value in self.counts.items() if key[0] in enabled_ids}
        self.last_scene = {key: value for key, value in self.last_scene.items() if key[0] in enabled_ids}

    def camera_status(self, status: str, reason: str) -> None:
        now = time.time()
        if status == self.health_state:
            return
        if status == "unavailable":
            for candidate in self.face_candidates.values():
                candidate["deadline"] = now
            self._flush_face_candidates(now)
            for state in self.presences.values():
                if state["hits"] < 3 or state["lost"]:
                    continue
                state["lost"] = True
                for episode in state["episodes"].values():
                    episode["observation_state"] = "unobserved"
                self._persist(self._event("track_lost", now, None,
                                          presence_id=state["id"], track_id=state["track_id"],
                                          object_type=state["kind"], last_seen_at=utc(state["last_seen"]),
                                          zone_episodes=self._episodes(state),
                                          reason="stream_interrupted"))
            self.health_state = "unavailable"
            self._persist(self._event("camera_health", now, None,
                                      state="unavailable", reason=reason, as_of=utc(now),
                                      last_reliable_at=utc(self.last_reliable_at) if self.last_reliable_at is not None else None,
                                      sampling_gap_seconds=max(0.0, now - self.last_reliable_at) if self.last_reliable_at is not None else 0.0))
            return
        if status == "healthy" and self.health_state == "unavailable":
            for candidate in self.face_candidates.values():
                candidate["deadline"] = now
            self._flush_face_candidates(now)
            for state in self.presences.values():
                if state["hits"] < 3:
                    continue
                for episode in state["episodes"].values():
                    episode["observation_state"] = "ended_unknown"
                self._persist(self._event("presence_ended_unknown", now, None,
                                          presence_id=state["id"], track_id=state["track_id"],
                                          object_type=state["kind"], last_seen_at=utc(state["last_seen"]),
                                          zone_episodes=self._episodes(state)))
            self.tracks.clear()
            self.presences.clear()
            self.counts.clear()
            self.last_scene.clear()
            self.session_id = str(uuid.uuid4())
            self.health_state = "healthy"
            self.last_health = now
            self._persist(self._event("camera_health", now, None,
                                      state="healthy", reason="none", as_of=utc(now),
                                      last_reliable_at=utc(now), sampling_gap_seconds=0))

    def _event(self, event_type, now, frame_id, use_case_id=None, **fields):
        return {"schema_version": "5.2", "observation_id": str(uuid.uuid4()),
                "deployment_id": self.config["deployment_id"], "camera_id": self.camera_id,
                "config_revision": int(self.config["config_revision"]),
                "stream_session_id": self.session_id, "use_case_id": use_case_id,
                "event_type": event_type, "observed_at": utc(now),
                "source_frame_id": frame_id, **fields}

    def _persist(self, event, evidence=(), embeddings=()):
        started = perf_counter()
        self.store.persist(event, list(evidence), list(embeddings))
        if self.telemetry is not None:
            self.telemetry.event_persisted(event, perf_counter() - started)

    def _measure(self, stage: str, device: str | None = None):
        if self.telemetry is None:
            return nullcontext()
        return self.telemetry.measure(stage, device=device)

    def _evidence(self, event, frame_id, image, kind, bounds, frame_shape, now):
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise RuntimeError("JPEG evidence encoding failed")
        body = encoded.tobytes()
        height, width = frame_shape[:2]
        evidence_id = str(uuid.uuid4())
        metadata = {"schema_version": "5.2", "evidence_id": evidence_id,
                    "observation_id": event["observation_id"], "evidence_type": kind,
                    "captured_at": utc(now), "source_frame_id": frame_id,
                    "source_bbox_normalized": bbox_normalized(bounds, width, height) if bounds else None,
                    "content_type": "image/jpeg", "width": int(image.shape[1]),
                    "height": int(image.shape[0]), "size_bytes": len(body),
                    "sha256": "sha256:" + hashlib.sha256(body).hexdigest()}
        return metadata, body

    def _embedding(self, event, evidence, frame_id, vector, profile, kind, now, presence_id,
                   quality=None, sequence=None):
        models = {
            "adaface-ir101-v18.1": ("AdaFace-IR101-INT8", "adaface_ir101_int8-v1", "aligned112-rgb-v1", 512),
            "transreid-ssl-v18.1": ("transreid_ssl_int8", "v1", "rgb256x128-minus1to1-v1", 384),
            "gaitbase-v18.1": ("gaitbase_int8", "v1", "mog2-opengait-30x64x44-v1", 4096),
            "siglip2-base-v1": ("google/siglip2-base-patch16-224", "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2", "rgb224-minus1to1-v1", 768),
        }
        model_id, version, preprocessing, dimension = models[profile]
        vector = np.asarray(vector, np.float32).reshape(-1)
        if len(vector) != dimension or not np.isfinite(vector).all() or not math.isclose(float(np.linalg.norm(vector)), 1.0, abs_tol=1e-3):
            raise ValueError(f"invalid {profile} embedding")
        item = {"schema_version": "5.2", "embedding_id": str(uuid.uuid4()),
                "observation_id": event["observation_id"], "use_case_id": event["use_case_id"],
                "evidence_id": evidence["evidence_id"], "profile_id": profile,
                "kind": kind, "vector": [float(value) for value in vector],
                "captured_at": utc(now), "source_frame_id": frame_id,
                "presence_id": presence_id, "model_id": model_id,
                "model_version": version, "preprocessing_id": preprocessing,
                "normalization": "l2"}
        if quality is not None:
            item["quality"] = max(0.0, min(1.0, float(quality)))
        if sequence is not None:
            item.update(sequence)
        return item

    def _matches(self, binding, kind, containing):
        if kind not in binding["object_types"]:
            return False
        return binding["scope"] == "full_frame" or bool(set(binding["geometry_ids"]) & containing)

    def _owner(self, kind, containing):
        eligible = [item for item in self.bindings
                    if item["app"] in {"vehicle_presence", "person_presence", "person_identity", "anpr"}
                    and self._matches(item, kind, containing)]
        eligible.sort(key=lambda item: (item["app"] not in {"vehicle_presence", "person_presence"}, item["use_case_id"]))
        return eligible[0] if eligible else None

    @staticmethod
    def _episodes(state):
        return list(state["episodes"].values())

    @staticmethod
    def _center(box):
        return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)

    @staticmethod
    def _box_size(box):
        return max(1.0, float(box[2] - box[0])), max(1.0, float(box[3] - box[1]))

    @staticmethod
    def _vehicle_type(class_name):
        return class_name if class_name in {"car", "truck", "van", "bus", "motorcycle"} else "other"

    def _presence_timeouts(self, state):
        width_height = state.get("frame_size")
        if width_height is None:
            return self.presence_close_unobserved_after, self.presence_close_ended_after
        width, height = width_height
        box_width, box_height = self._box_size(state["box"])
        norm_width = box_width / max(1.0, float(width))
        norm_height = box_height / max(1.0, float(height))
        if max(norm_width, norm_height) < 0.12 or min(norm_width, norm_height) < 0.05:
            return self.presence_wide_unobserved_after, self.presence_wide_ended_after
        return self.presence_close_unobserved_after, self.presence_close_ended_after

    def _recovery_distance_limit(self, state, box):
        width_height = state.get("frame_size")
        frame_scale = 320.0
        if width_height is not None:
            frame_scale = max(160.0, 0.17 * math.hypot(*width_height))
        old_w, old_h = self._box_size(state["box"])
        new_w, new_h = self._box_size(box)
        subject_scale = 2.5 * max(old_w, old_h, new_w, new_h)
        return min(520.0, max(120.0, frame_scale, subject_scale))

    def _reassociate(self, kind, box, vector, now, active_keys, class_name=None):
        if kind == "person":
            if vector is None:
                return None
            best = None
            for state in self.presences.values():
                if state["kind"] != kind or not state["lost"] or state["track_key"] in active_keys:
                    continue
                _, ended_after = self._presence_timeouts(state)
                if now - state["last_seen"] > ended_after or state.get("body_vector") is None:
                    continue
                if np.linalg.norm(np.subtract(self._center(box), self._center(state["box"]))) > self._recovery_distance_limit(state, box):
                    continue
                similarity = float(np.dot(vector, state["body_vector"]))
                if similarity >= 0.78 and (best is None or similarity > best[0]):
                    best = similarity, state
            return best[1] if best else None

        if kind != "vehicle":
            return None
        candidates = []
        for state in self.presences.values():
            if state["kind"] != kind or not state["lost"] or state["track_key"] in active_keys:
                continue
            _, ended_after = self._presence_timeouts(state)
            if now - state["last_seen"] > min(ended_after, self.vehicle_recovery_gap):
                continue
            limit = self._recovery_distance_limit(state, box)
            distance = float(np.linalg.norm(np.subtract(self._center(box), self._center(state["box"]))))
            if distance > limit:
                continue
            old_w, old_h = self._box_size(state["box"])
            new_w, new_h = self._box_size(box)
            size_ratio = max(old_w / new_w, new_w / old_w, old_h / new_h, new_h / old_h)
            if size_ratio > 2.4:
                continue
            previous_type = state.get("vehicle_type")
            current_type = self._vehicle_type(class_name)
            type_cost = 0.0 if previous_type in {None, current_type, "other"} or current_type == "other" else 0.25
            score = distance / limit + min(0.5, abs(math.log(size_ratio))) + type_cost
            candidates.append((score, state))
        candidates.sort(key=lambda item: item[0])
        if not candidates or candidates[0][0] > 0.95:
            return None
        if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < 0.20:
            return None
        return candidates[0][1]

    def quiet_frame_needed(self, now: float) -> bool:
        """Return whether pixels are useful while object inference is idle.

        The current built-in motion policy does not force independent scene-only
        conversion during idle. Scene samples ride on active inference frames and
        on the periodic sweep frame. This method remains available for future
        callers that can cheaply provide pixels.
        """
        return False

    def process_quiet(self, now: float, index: int, frame=None) -> None:
        """Advance lifecycle state for a frame where models did not run.

        Quiet frames are unknown observations. They may age presences toward
        track_lost/ended_unknown and emit health heartbeats, but they must not
        produce count=0, zone_exit, fire_smoke_cleared, or other negative facts.
        """
        self.last_reliable_at = now
        if not self.scene_pressure and now - self.last_health >= 30:
            self.last_health = now
            self._persist(self._event("camera_health", now, None, state="healthy", reason="none",
                                      as_of=utc(now), last_reliable_at=utc(now),
                                      sampling_gap_seconds=0))
        self._lost_and_unknown(set(), now)
        self._flush_face_candidates(now)
        self._plate_idle_timeouts(now)
        if frame is not None:
            from types import SimpleNamespace
            self._scenes(SimpleNamespace(frame=frame), now, f"{self.camera_id}:{index}")

    def process(self, packet, faces=()):
        now = (getattr(packet, "frame_observed_at", None)
               or getattr(packet, "frame_received_at", None)
               or time.time())
        self.last_reliable_at = now
        frame_id = f"{self.camera_id}:{packet.index}"
        frame = packet.frame
        height, width = frame.shape[:2]
        if not self.scene_pressure and now - self.last_health >= 30:
            self.last_health = now
            self._persist(self._event("camera_health", now, None, state="healthy", reason="none",
                                      as_of=utc(now), last_reliable_at=utc(now), sampling_gap_seconds=0))
        detections = [item for item in packet.detections
                      if item.model_name == "vehicle" and item.metadata.get("track_id") is not None
                      and not item.metadata.get("predicted")]
        active_keys = {("person" if item.class_name == "pedestrian" else "vehicle", str(item.metadata["track_id"])) for item in detections}
        face_by_track = {str(face.track_id): face for face in faces}
        body_vectors = self._body_vectors(packet, detections, now)
        for item in detections:
            kind = "person" if item.class_name == "pedestrian" else "vehicle"
            track_id = str(item.metadata["track_id"])
            key = (kind, track_id)
            vector = body_vectors.get(track_id)
            state = self.tracks.get(key)
            recovered_from = None
            if state is None:
                state = self._reassociate(kind, item.bbox, vector, now, active_keys, item.class_name)
                if state is None:
                    state = {"id": str(uuid.uuid4()), "kind": kind, "track_key": key,
                             "track_id": track_id, "first_seen": now, "last_seen": now,
                             "hits": 0, "box": item.bbox, "episodes": {}, "zone_state": {},
                             "zone_votes": {}, "line_state": {}, "owner": None,
                             "last_object": 0.0, "last_feature": {}, "plates": {},
                             "plate_attempts": {}, "plate_outcomes": set(),
                             "lost": False, "body_vector": None,
                             "vehicle_type": self._vehicle_type(item.class_name) if kind == "vehicle" else None}
                    self.presences[state["id"]] = state
                else:
                    previous = state["track_id"]
                    self.tracks.pop(state["track_key"], None)
                    state["track_key"] = key
                    state["track_id"] = track_id
                    recovered_from = previous
                self.tracks[key] = state
            elif state["lost"]:
                recovered_from = track_id
            previous_last_seen = state["last_seen"]
            if state["hits"] < 3 and now - previous_last_seen > self.candidate_max_gap:
                state["hits"] = 0
                state["first_seen"] = now
            state["hits"] += 1
            state["last_seen"] = now
            state["box"] = item.bbox
            state["frame_size"] = (width, height)
            state["lost"] = False
            if kind == "vehicle":
                state["vehicle_type"] = self._vehicle_type(item.class_name)
            if vector is not None:
                state["body_vector"] = vector
                state["last_body_at"] = now
            if state["hits"] < 3:
                continue
            point = ((item.bbox[0] + item.bbox[2]) / (2 * width), item.bbox[3] / height)
            containing = {identifier for identifier, zone in self.zones.items() if inside(point, zone["poly"])}
            if recovered_from is not None:
                for zone_id, episode in state["episodes"].items():
                    if zone_id in containing:
                        episode["last_confirmed_inside_at"] = utc(now)
                        episode["observation_state"] = "observed_inside"
                self._persist(self._event("presence_recovered", now, frame_id,
                                          presence_id=state["id"], previous_track_id=recovered_from,
                                          track_id=track_id, object_type=kind,
                                          last_seen_at=utc(previous_last_seen),
                                          zone_episodes=self._episodes(state)))
            if state["hits"] == 3:
                state["owner"] = self._owner(kind, containing)
                presence_zones = {zone_id for binding in self.bindings
                                  if binding["app"] in {"vehicle_presence", "person_presence"}
                                  and kind in binding["object_types"]
                                  for zone_id in binding["geometry_ids"]}
                for zone_id in containing & presence_zones:
                    if boundary_distance(point, self.zones[zone_id]["poly"]) < self.zone_boundary_margin:
                        continue
                    state["zone_state"][zone_id] = True
                    state["episodes"][zone_id] = self._episode(zone_id, now, False)
            with self._measure("presence.zone_lifecycle"):
                self._zones(packet, state, containing, point, now, frame_id)
            with self._measure("line_crossing.policy"):
                self._lines(packet, state, point, now, frame_id)
            if state["owner"] is None:
                state["owner"] = self._owner(kind, containing)
            owner = state["owner"]
            if owner and (state["last_object"] == 0 or now - state["last_object"] >= 60):
                with self._measure("object_presence.emission"):
                    self._object(packet, item, state, owner, now, frame_id)
            if kind == "person":
                with self._measure("person_identity.policy"):
                    self._features(
                        packet, item, state, containing, face_by_track.get(track_id),
                        vector, now, frame_id,
                    )
        with self._measure("presence.loss_recovery"):
            self._lost_and_unknown(active_keys, now)
        with self._measure("counting.policy"):
            self._counts(packet, detections, now, frame_id)
        with self._measure("anpr.policy"):
            self._plates(packet, now, frame_id)
        with self._measure("fire_smoke.policy"):
            self._hazards(packet, now, frame_id)
        with self._measure("scene_search.policy"):
            self._scenes(packet, now, frame_id)
        with self._measure("person_identity.candidate_flush"):
            self._flush_face_candidates(now)

    def _body_vectors(self, packet, detections, now):
        if self.body is None:
            return {}
        people = [item for item in detections if item.class_name == "pedestrian"
                  and item.metadata.get("track_id") is not None]
        due = [item for item in people if now - self.tracks.get(("person", str(item.metadata["track_id"])), {}).get("last_body_at", 0) >= 1]
        crops = []
        for item in due:
            crop, _ = self._crop(packet.frame, item.bbox)
            if crop is not None and crop.shape[0] >= 64 and crop.shape[1] >= 32 and self._body_quality(crop) >= .2:
                crops.append((item, crop))
        if crops:
            with self._measure("person_identity.body_embedding", "NPU"):
                vectors = self.body.embed([crop for _, crop in crops])
        else:
            vectors = []
        result = {}
        for (item, _), vector in zip(crops, vectors):
            track_id = str(item.metadata["track_id"])
            result[track_id] = np.asarray(vector, np.float32)
            state = self.tracks.get(("person", track_id))
            if state:
                state["last_body_at"] = now
        return result

    @staticmethod
    def _body_quality(image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        area = min(1.0, image.shape[0] * image.shape[1] / 120000.0)
        sharpness = min(1.0, float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 800.0)
        exposure = max(0.0, 1.0 - abs(float(gray.mean()) - 127.5) / 127.5)
        return max(0.0, min(1.0, .4 * area + .4 * sharpness + .2 * exposure))

    @staticmethod
    def _crop(frame, box):
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = [int(value) for value in box]
        x1, x2 = max(0, x1), min(width, x2)
        y1, y2 = max(0, y1), min(height, y2)
        if x2 <= x1 or y2 <= y1:
            return None, None
        return frame[y1:y2, x1:x2].copy(), (x1, y1, x2, y2)

    @staticmethod
    def _episode(zone_id, now, entry):
        return {"zone_id": zone_id, "zone_episode_id": str(uuid.uuid4()),
                "first_observed_inside_at": utc(now), "last_confirmed_inside_at": utc(now),
                "entry_observed": entry, "observation_state": "observed_inside"}

    def _zones(self, packet, state, containing, point, now, frame_id):
        bindings_by_zone = {}
        for binding in self.bindings:
            if binding["app"] in {"vehicle_presence", "person_presence"} and state["kind"] in binding["object_types"]:
                for zone_id in binding["geometry_ids"]:
                    bindings_by_zone.setdefault(zone_id, []).append(binding)
        for zone_id, bindings in bindings_by_zone.items():
            if boundary_distance(point, self.zones[zone_id]["poly"]) < self.zone_boundary_margin:
                continue
            current = zone_id in containing
            previous = state["zone_state"].get(zone_id)
            votes = state["zone_votes"].get(zone_id, (current, 0))
            votes = (current, votes[1] + 1 if votes[0] == current else 1)
            state["zone_votes"][zone_id] = votes
            if previous is None:
                state["zone_state"][zone_id] = current
                if current and zone_id not in state["episodes"]:
                    state["episodes"][zone_id] = self._episode(zone_id, now, False)
                continue
            if current == previous:
                if current and zone_id in state["episodes"]:
                    state["episodes"][zone_id]["last_confirmed_inside_at"] = utc(now)
                    state["episodes"][zone_id]["observation_state"] = "observed_inside"
                continue
            if votes[1] < 2:
                continue
            state["zone_state"][zone_id] = current
            episode = self._episode(zone_id, now, True) if current else state["episodes"].get(zone_id)
            if episode is None:
                continue
            if current:
                state["episodes"][zone_id] = episode
            for binding in bindings:
                event = self._event("zone_entry" if current else "zone_exit", now, frame_id,
                                    binding["use_case_id"], presence_id=state["id"],
                                    track_id=state["track_id"], object_type=state["kind"],
                                    zone_id=zone_id, zone_episode_id=episode["zone_episode_id"],
                                    first_observed_inside_at=episode["first_observed_inside_at"],
                                    transition_at=utc(now))
                evidence = self._evidence(event, frame_id, packet.frame, "frame", None, packet.frame.shape, now)
                event["evidence_ids"] = [evidence[0]["evidence_id"]]
                self._persist(event, [evidence])
                self._trigger_scene(packet, binding["use_case_id"], event["observation_id"], now, frame_id)
            if not current:
                state["episodes"].pop(zone_id, None)

    def _lines(self, packet, state, point, now, frame_id):
        for binding in self.bindings:
            if binding["app"] not in {"line_crossing", "vehicle_entry_exit_counts", "people_entry_exit_counts"} or state["kind"] not in binding["object_types"]:
                continue
            for line_id in binding["geometry_ids"]:
                line = self.lines[line_id]
                a, b = line["a"], line["b"]
                side = (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])
                key = (binding["use_case_id"], line_id)
                prior = state["line_state"].get(key)
                state["line_state"][key] = (side, point, now)
                if prior is None or abs(side) < .005 or abs(prior[0]) < .005 or prior[0] * side >= 0:
                    continue
                if math.dist(point, prior[1]) < .02 or now - state.get("last_cross", {}).get(key, 0) < 10:
                    continue
                segment = (point[0] - prior[1][0], point[1] - prior[1][1])
                line_vector = (b[0] - a[0], b[1] - a[1])
                denominator = line_vector[0] * segment[1] - line_vector[1] * segment[0]
                if abs(denominator) < 1e-9:
                    continue
                relative = (prior[1][0] - a[0], prior[1][1] - a[1])
                line_position = (relative[0] * segment[1] - relative[1] * segment[0]) / denominator
                subject_position = (relative[0] * line_vector[1] - relative[1] * line_vector[0]) / denominator
                if not (0 <= line_position <= 1 and 0 <= subject_position <= 1):
                    continue
                direction_raw = "left_to_right" if prior[0] < side else "right_to_left"
                event = self._event("line_cross", now, frame_id, binding["use_case_id"],
                                    presence_id=state["id"], track_id=state["track_id"],
                                    object_type=state["kind"], line_id=line_id,
                                    direction_raw=direction_raw,
                                    direction=line["direction_mapping"][direction_raw], crossed_at=utc(now))
                evidence = self._evidence(event, frame_id, packet.frame, "frame", None, packet.frame.shape, now)
                event["evidence_ids"] = [evidence[0]["evidence_id"]]
                self._persist(event, [evidence])
                state.setdefault("last_cross", {})[key] = now
                self._trigger_scene(packet, binding["use_case_id"], event["observation_id"], now, frame_id)

    def _object(self, packet, item, state, owner, now, frame_id):
        crop, bounds = self._crop(packet.frame, item.bbox)
        if crop is None:
            return
        height, width = packet.frame.shape[:2]
        first = state["last_object"] == 0
        event = self._event("object_present", now, frame_id, owner["use_case_id"],
                            presence_id=state["id"], track_id=state["track_id"],
                            object_type=state["kind"], vehicle_type=(item.class_name if item.class_name in {"car", "truck", "van", "bus", "motorcycle"} else "other") if state["kind"] == "vehicle" else None,
                            reason="first_seen" if first else "periodic",
                            bbox_normalized=bbox_normalized(item.bbox, width, height),
                            confidence=max(0.0, min(1.0, float(item.confidence))),
                            first_seen_at=utc(state["first_seen"]), zone_episodes=self._episodes(state))
        evidence, embeddings = [], []
        if first:
            media = self._evidence(event, frame_id, crop, "subject_crop", bounds, packet.frame.shape, now)
            evidence.append(media)
            event["evidence_ids"] = [media[0]["evidence_id"]]
            if self.scene is not None and "siglip2-base-v1" in owner.get("embedding_profile_ids", []):
                with self._measure("object_presence.semantic_embedding", "GPU"):
                    vector = self.scene.embed(crop)
                embedding = self._embedding(event, media[0], frame_id, vector,
                                            "siglip2-base-v1", "semantic", now, state["id"])
                embeddings.append(embedding)
                event["embedding_ids"] = [embedding["embedding_id"]]
        self._persist(event, evidence, embeddings)
        state["last_object"] = now

    def _features(self, packet, item, state, containing, face, body_vector, now, frame_id):
        bindings = [binding for binding in self.bindings if binding["app"] == "person_identity"
                    and self._matches(binding, "person", containing)]
        if not bindings:
            return
        binding = sorted(bindings, key=lambda value: value["use_case_id"])[0]
        profiles = set(binding["embedding_profile_ids"])
        candidates = []
        if face is not None and "adaface-ir101-v18.1" in profiles:
            candidates.append(("face", face.embedding, float(face.quality), face.bbox, "adaface-ir101-v18.1", None))
        if body_vector is not None and "transreid-ssl-v18.1" in profiles:
            body_crop, _ = self._crop(packet.frame, item.bbox)
            quality = self._body_quality(body_crop) if body_crop is not None else 0.0
            candidates.append(("body", body_vector, quality, item.bbox, "transreid-ssl-v18.1", None))
        if self.gait is not None and "gaitbase-v18.1" in profiles and packet.index % 3 == 0:
            key = state["id"]
            with self._measure("person_identity.gait_processing", "NPU"):
                result = self.gait.collect(
                    packet.frame, [item], self.camera_id,
                    {int(state["track_id"]): key}, captured_at=now,
                )
            vector = result.get(int(state["track_id"]))
            if vector is not None:
                count = len(self.gait.buffers[key])
                sequence = {"sequence_started_at": utc(self.gait.buffer_times[key][0]),
                            "sequence_ended_at": utc(self.gait.buffer_times[key][-1]),
                            "usable_silhouette_count": count}
                candidates.append(("gait", vector, min(1.0, count / 30), item.bbox, "gaitbase-v18.1", sequence))
        for kind, vector, quality, box, profile, sequence in candidates:
            previous = state["last_feature"].get(kind)
            improvement = .2 if kind == "gait" else .15 if kind == "body" else .1
            if previous and (quality < previous[0] + improvement
                             or now - previous[1] < self.feature_cooldown):
                continue
            if kind == "face":
                self._offer_face_candidate(packet, state, binding, vector, quality, box, now, frame_id)
                continue
            crop_box = box
            crop, bounds = self._crop(packet.frame, crop_box)
            if crop is None:
                continue
            event = self._event("person_feature_sample", now, frame_id, binding["use_case_id"],
                                presence_id=state["id"], track_id=state["track_id"],
                                kind=kind, profile_id=profile, quality=quality,
                                sample_reason="first_qualified" if previous is None else "quality_improved")
            media = self._evidence(event, frame_id, crop, "face_crop" if kind == "face" else "subject_crop", bounds, packet.frame.shape, now)
            embedding = self._embedding(event, media[0], frame_id, vector, profile, kind, now,
                                        state["id"], quality, sequence)
            event["evidence_ids"] = [media[0]["evidence_id"]]
            event["embedding_ids"] = [embedding["embedding_id"]]
            self._persist(event, [media], [embedding])
            state["last_feature"][kind] = (quality, now)

    def _offer_face_candidate(self, packet, state, binding, vector, quality, box, now, frame_id):
        existing = self.face_candidates.get(state["id"])
        if existing is not None and quality <= existing["quality"]:
            return
        crop, bounds = self._crop(packet.frame, self._face_bounds(packet.frame, box))
        if crop is None:
            return
        deadline = existing["deadline"] if existing is not None else now + self.face_selection_window
        self.face_candidates[state["id"]] = {
            "presence_id": state["id"], "track_id": state["track_id"],
            "use_case_id": binding["use_case_id"], "quality": quality,
            "vector": np.asarray(vector, np.float32).copy(), "image": crop,
            "bounds": bounds, "frame_shape": packet.frame.shape,
            "frame_id": frame_id, "captured_at": now, "deadline": deadline,
            "sample_reason": "first_qualified" if "face" not in state["last_feature"] else "quality_improved",
        }

    def _flush_face_candidates(self, now):
        for presence_id, candidate in list(self.face_candidates.items()):
            if now < candidate["deadline"]:
                continue
            event = self._event("person_feature_sample", candidate["captured_at"],
                                candidate["frame_id"], candidate["use_case_id"],
                                presence_id=presence_id, track_id=candidate["track_id"],
                                kind="face", profile_id="adaface-ir101-v18.1",
                                quality=candidate["quality"], sample_reason=candidate["sample_reason"])
            media = self._evidence(event, candidate["frame_id"], candidate["image"],
                                   "face_crop", candidate["bounds"], candidate["frame_shape"],
                                   candidate["captured_at"])
            embedding = self._embedding(event, media[0], candidate["frame_id"],
                                        candidate["vector"], "adaface-ir101-v18.1", "face",
                                        candidate["captured_at"], presence_id, candidate["quality"])
            event["evidence_ids"] = [media[0]["evidence_id"]]
            event["embedding_ids"] = [embedding["embedding_id"]]
            self._persist(event, [media], [embedding])
            state = self.presences.get(presence_id)
            if state is not None:
                state["last_feature"]["face"] = (candidate["quality"], candidate["captured_at"])
            self.face_candidates.pop(presence_id, None)

    @staticmethod
    def _face_bounds(frame, box):
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = [int(round(value)) for value in box]
        side = min(width, height, max(2, 2 * max(x2 - x1, y2 - y1)))
        left = max(0, min(width - side, int(round((x1 + x2) / 2 - side / 2))))
        top = max(0, min(height - side, int(round((y1 + y2) / 2 - side / 2))))
        return left, top, left + side, top + side

    def _lost_and_unknown(self, active_keys, now):
        for key, state in list(self.tracks.items()):
            if key in active_keys:
                continue
            gap = now - state["last_seen"]
            if state["hits"] < 3:
                if gap >= 8:
                    self.tracks.pop(key, None)
                    self.presences.pop(state["id"], None)
                continue
            unobserved_after, ended_after = self._presence_timeouts(state)
            if gap >= unobserved_after and not state["lost"]:
                state["lost"] = True
                for episode in state["episodes"].values():
                    episode["observation_state"] = "unobserved"
                self._persist(self._event("track_lost", now, None, presence_id=state["id"],
                                          track_id=state["track_id"], object_type=state["kind"],
                                          last_seen_at=utc(state["last_seen"]), zone_episodes=self._episodes(state),
                                          reason="unknown"))
            if gap >= ended_after:
                for episode in state["episodes"].values():
                    episode["observation_state"] = "ended_unknown"
                self._persist(self._event("presence_ended_unknown", now, None,
                                          presence_id=state["id"], track_id=state["track_id"],
                                          object_type=state["kind"], last_seen_at=utc(state["last_seen"]),
                                          zone_episodes=self._episodes(state)))
                self.tracks.pop(key, None)
                self.presences.pop(state["id"], None)

    def _plate_idle_timeouts(self, now):
        for state in self.presences.values():
            if state["kind"] != "vehicle" or state["lost"]:
                continue
            for use_case_id, attempt in state["plate_attempts"].items():
                if (use_case_id in state["plates"] or use_case_id in state["plate_outcomes"]
                        or now - attempt["started"] < self.plate_outcome_delay):
                    continue
                selected = self._plate_candidate_ready(attempt, None, now)
                if selected is not None:
                    binding = next((item for item in self.bindings
                                    if item["use_case_id"] == use_case_id), None)
                    if binding is not None:
                        self._emit_plate_read(state, binding, selected, None, now)
                        attempt["started"] = now
                        attempt["candidates"] = []
                        continue
                self._emit_plate_outcome(state, use_case_id, attempt)

    def _counts(self, packet, detections, now, frame_id):
        height, width = packet.frame.shape[:2]
        for binding in self.bindings:
            kind = "vehicle" if binding["app"] == "vehicle_counting" else "person"
            if binding["app"] not in {"vehicle_counting", "people_counting"}:
                continue
            areas = binding["geometry_ids"] if binding["scope"] == "geometry" else [None]
            for zone_id in areas:
                count = 0
                for item in detections:
                    item_kind = "person" if item.class_name == "pedestrian" else "vehicle"
                    key = (item_kind, str(item.metadata["track_id"]))
                    if item_kind != kind or self.tracks[key]["hits"] < 3:
                        continue
                    point = ((item.bbox[0] + item.bbox[2]) / (2 * width), item.bbox[3] / height)
                    if zone_id is None or inside(point, self.zones[zone_id]["poly"]):
                        count += 1
                key = (binding["use_case_id"], zone_id)
                state = self.counts.setdefault(key, {"value": None, "candidate": None, "votes": 0, "sent": 0.0})
                if state["candidate"] == count:
                    state["votes"] += 1
                else:
                    state["candidate"], state["votes"] = count, 1
                if state["votes"] < 2 or (state["value"] == count and now - state["sent"] < 20):
                    continue
                self._persist(self._event("zone_count", now, frame_id, binding["use_case_id"],
                                          zone_id=zone_id, object_type=kind, count=count,
                                          count_semantics="visible_now", as_of=utc(now),
                                          coverage_state="complete"))
                state["value"], state["sent"] = count, now

    def _plate_attempt(self, state, use_case_id, now):
        return state["plate_attempts"].setdefault(use_case_id, {
            "started": now, "provisional": False, "candidates": [],
            "last_frame": None, "last_frame_id": None, "last_captured_at": now,
            "last_bbox": None, "last_frame_shape": None,
        })

    @staticmethod
    def _plate_sharpness(crop):
        if crop is None or getattr(crop, "size", 0) == 0:
            return 0.0
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def _plate_candidate(self, packet, plate, text, confidence, containing, now, frame_id):
        crop, _ = self._crop(packet.frame, plate.bbox)
        width = max(1.0, float(plate.bbox[2] - plate.bbox[0]))
        height = max(1.0, float(plate.bbox[3] - plate.bbox[1]))
        area_score = min(1.0, (width * height) / 1600.0)
        sharpness_score = min(1.0, self._plate_sharpness(crop) / 80.0)
        score = 0.72 * confidence + 0.18 * area_score + 0.10 * sharpness_score
        return {
            "text": text, "confidence": confidence, "score": score,
            "bbox": tuple(float(value) for value in plate.bbox),
            "frame": packet.frame.copy(), "frame_shape": packet.frame.shape,
            "frame_id": frame_id, "captured_at": now,
            "containing": set(containing),
        }

    def _best_plate_candidate(self, attempt):
        candidates = list(attempt.get("candidates") or [])
        if not candidates:
            return None, 0
        counts = {}
        for candidate in candidates:
            counts[candidate["text"]] = counts.get(candidate["text"], 0) + 1
        best = max(candidates, key=lambda item: (
            item["score"] + min(0.15, 0.05 * max(0, counts[item["text"]] - 1)),
            item["confidence"],
        ))
        return best, counts[best["text"]]

    def _plate_read_allowed(self, previous, candidate, sent_at):
        confidence = candidate["confidence"]
        text = candidate["text"]
        if not previous:
            return True
        previous_text, _previous_observation_id = previous[:2]
        previous_confidence = float(previous[2]) if len(previous) > 2 else 0.0
        previous_sent_at = float(previous[3]) if len(previous) > 3 else 0.0
        if text == previous_text:
            return (confidence >= previous_confidence + self.plate_confidence_improvement
                    and sent_at - previous_sent_at >= self.plate_update_cooldown)
        fast_replace = (previous_confidence < self.plate_low_confidence_replace
                        and confidence >= self.plate_change_min_confidence)
        if fast_replace:
            return True
        return (sent_at - previous_sent_at >= self.plate_update_cooldown
                and confidence >= self.plate_change_min_confidence
                and confidence >= previous_confidence + self.plate_change_margin)

    def _plate_candidate_ready(self, attempt, previous, now):
        candidate, read_count = self._best_plate_candidate(attempt)
        if candidate is None:
            return None
        if candidate["confidence"] < self.plate_emit_min_confidence:
            return None
        if not self._plate_read_allowed(previous, candidate, now):
            return None
        elapsed = now - attempt["started"]
        if previous and elapsed >= min(1.0, self.plate_candidate_window):
            return candidate
        repeated = read_count >= self.plate_emit_min_reads
        fast_repeated = repeated and candidate["confidence"] >= self.plate_fast_emit_confidence
        high_single_after_window = (
            elapsed >= self.plate_candidate_window
            and candidate["confidence"] >= self.plate_single_read_confidence
        )
        normal_window = elapsed >= self.plate_candidate_window and repeated
        if fast_repeated or high_single_after_window or normal_window:
            return candidate
        return None

    def _emit_plate_read(self, state, binding, candidate, previous, now):
        height, width = candidate["frame_shape"][:2]
        zone_id = next((value for value in binding["geometry_ids"]
                        if value in candidate["containing"]), None)
        event = self._event("plate_read", candidate["captured_at"], candidate["frame_id"],
                            binding["use_case_id"], presence_id=state["id"],
                            track_id=state["track_id"], zone_id=zone_id,
                            plate_text=candidate["text"], partial=False,
                            confidence=candidate["confidence"],
                            plate_bbox_normalized=bbox_normalized(candidate["bbox"], width, height),
                            supersedes_observation_id=previous[1] if previous else None)
        media = self._evidence(event, candidate["frame_id"], candidate["frame"],
                               "frame", None, candidate["frame_shape"], candidate["captured_at"])
        event["evidence_ids"] = [media[0]["evidence_id"]]
        self._persist(event, [media])
        state["plates"][binding["use_case_id"]] = (
            candidate["text"], event["observation_id"], candidate["confidence"], now,
        )

    def _emit_plate_outcome(self, state, use_case_id, attempt, outcome="unreadable"):
        event_time = float(attempt.get("last_captured_at") or time.time())
        frame_id = attempt.get("last_frame_id")
        event = self._event("plate_outcome", event_time, frame_id, use_case_id,
                            presence_id=state["id"], track_id=state["track_id"],
                            outcome=outcome)
        evidence = []
        frame = attempt.get("last_frame")
        frame_shape = attempt.get("last_frame_shape")
        if frame is not None and frame_id is not None and frame_shape is not None:
            media = self._evidence(event, frame_id, frame, "frame", None, frame_shape, event_time)
            event["evidence_ids"] = [media[0]["evidence_id"]]
            evidence.append(media)
        self._persist(event, evidence)
        state["plate_outcomes"].add(use_case_id)

    def _plates(self, packet, now, frame_id):
        height, width = packet.frame.shape[:2]
        for plate in packet.detections:
            if plate.model_name != "license_plate" or plate.parent_id is None:
                continue
            key = ("vehicle", str(plate.parent_id))
            state = self.tracks.get(key)
            if state is None or state["hits"] < 3:
                continue
            text = re.sub(r"[^A-Z0-9? -]", "", str(plate.metadata.get("ocr_text") or "").upper()).strip()[:32]
            point = ((state["box"][0] + state["box"][2]) / (2 * width), state["box"][3] / height)
            containing = {zone_id for zone_id, zone in self.zones.items() if inside(point, zone["poly"])}
            for binding in self.bindings:
                if binding["app"] != "anpr" or not self._matches(binding, "vehicle", containing):
                    continue
                use_case_id = binding["use_case_id"]
                attempt = self._plate_attempt(state, use_case_id, now)
                attempt["last_frame"] = packet.frame.copy()
                attempt["last_frame_id"] = frame_id
                attempt["last_captured_at"] = now
                attempt["last_bbox"] = tuple(float(value) for value in plate.bbox)
                attempt["last_frame_shape"] = packet.frame.shape
                attempt["provisional"] = attempt["provisional"] or bool(plate.metadata.get("ocr_provisional"))
                if not text:
                    if (use_case_id not in state["plate_outcomes"]
                            and now - attempt["started"] >= self.plate_outcome_delay):
                        self._emit_plate_outcome(state, use_case_id, attempt)
                    continue
                confidence = plate.metadata.get("ocr_confidence", plate.confidence)
                confidence = max(0.0, min(1.0, float(confidence)))
                attempt["candidates"].append(
                    self._plate_candidate(packet, plate, text, confidence, containing, now, frame_id)
                )
                previous = state["plates"].get(use_case_id)
                selected = self._plate_candidate_ready(attempt, previous, now)
                if selected is not None:
                    self._emit_plate_read(state, binding, selected, previous, now)
                    attempt["started"] = now
                    attempt["candidates"] = []
                    state["plate_outcomes"].discard(use_case_id)
        for state in self.presences.values():
            if state["kind"] != "vehicle" or state["lost"]:
                continue
            for use_case_id, attempt in state["plate_attempts"].items():
                if (use_case_id in state["plates"] or use_case_id in state["plate_outcomes"]
                        or now - attempt["started"] < self.plate_outcome_delay):
                    continue
                selected = self._plate_candidate_ready(attempt, None, now)
                if selected is not None:
                    binding = next((item for item in self.bindings
                                    if item["use_case_id"] == use_case_id), None)
                    if binding is not None:
                        self._emit_plate_read(state, binding, selected, None, now)
                        attempt["started"] = now
                        attempt["candidates"] = []
                        continue
                self._emit_plate_outcome(state, use_case_id, attempt)

    def _hazards(self, packet, now, frame_id):
        if not getattr(packet, "analytics_state", {}).get("fire_smoke_evaluated", True):
            return
        for binding in self.bindings:
            if binding["app"] != "fire_smoke":
                continue
            zones = binding["geometry_ids"] if binding["scope"] == "geometry" else [None]
            for zone_id in zones:
                for hazard in ("fire", "smoke"):
                    positives = [item for item in packet.detections if item.model_name == "smoke_fire"
                                 and item.class_name == hazard and item.confidence >= self.hazard_min_confidence and
                                 (zone_id is None or inside(((item.bbox[0] + item.bbox[2]) / (2 * packet.frame.shape[1]),
                                                              (item.bbox[1] + item.bbox[3]) / (2 * packet.frame.shape[0])),
                                                             self.zones[zone_id]["poly"]))]
                    key = (binding["use_case_id"], zone_id, hazard)
                    state = self.hazards.setdefault(key, {"positive": 0, "positive_started": None,
                                                          "negative_started": None, "episode": None})
                    if positives:
                        if state["positive_started"] is None or now - state["positive_started"] > 2:
                            state["positive_started"], state["positive"] = now, 1
                        else:
                            state["positive"] += 1
                        state["negative_started"] = None
                    else:
                        state["positive"], state["positive_started"] = 0, None
                        if state["negative_started"] is None:
                            state["negative_started"] = now
                    if state["episode"] is None and state["positive"] >= 3:
                        state["episode"] = str(uuid.uuid4())
                        event = self._event("fire_smoke_suspected", now, frame_id, binding["use_case_id"],
                                            hazard=hazard, zone_id=zone_id,
                                            score=max(0.0, min(1.0, float(max(item.confidence for item in positives)))),
                                            hazard_episode_id=state["episode"])
                        media = self._evidence(event, frame_id, packet.frame, "frame", None, packet.frame.shape, now)
                        event["evidence_ids"] = [media[0]["evidence_id"]]
                        self._persist(event, [media])
                    if (state["episode"] is not None and state["negative_started"] is not None
                            and now - state["negative_started"] >= 10):
                        self._persist(self._event("fire_smoke_cleared", now, frame_id, binding["use_case_id"],
                                                  hazard=hazard, zone_id=zone_id,
                                                  hazard_episode_id=state["episode"]))
                        state["episode"] = None

    def _scenes(self, packet, now, frame_id):
        if self.scene is None:
            return
        for binding in self.bindings:
            if binding["app"] != "scene_search":
                continue
            areas = binding["geometry_ids"] if binding["scope"] == "geometry" else [None]
            for zone_id in areas:
                key = (binding["use_case_id"], zone_id)
                if now - self.last_scene.get(key, 0) >= binding["sample_interval_seconds"]:
                    self._scene(packet, binding, zone_id, now, frame_id, "interval")

    def _trigger_scene(self, packet, use_case_id, observation_id, now, frame_id):
        if self.scene is None:
            return
        for binding in self.bindings:
            if binding["app"] != "scene_search" or use_case_id not in binding.get("scene_trigger_use_case_ids", []):
                continue
            for zone_id in binding["geometry_ids"] if binding["scope"] == "geometry" else [None]:
                if now - self.last_scene.get((binding["use_case_id"], zone_id), 0) >= 1:
                    self._scene(packet, binding, zone_id, now, frame_id, "event", observation_id)

    def _scene(self, packet, binding, zone_id, now, frame_id, trigger, triggering_observation_id=None):
        if not self._scene_capacity(now):
            return
        height, width = packet.frame.shape[:2]
        if zone_id is None:
            crop, bounds = packet.frame, None
        else:
            points = self.zones[zone_id]["poly"]
            bounds = (int(min(point[0] for point in points) * width),
                      int(min(point[1] for point in points) * height),
                      int(max(point[0] for point in points) * width),
                      int(max(point[1] for point in points) * height))
            crop, bounds = self._crop(packet.frame, bounds)
        if crop is None or crop.size == 0:
            return
        with self._measure("scene_search.embedding", "GPU"):
            vector = self.scene.embed(crop)
        fields = {"trigger": trigger, "scope": binding["scope"], "zone_id": zone_id}
        if triggering_observation_id is not None:
            fields["triggering_observation_id"] = triggering_observation_id
        event = self._event("scene_sample", now, frame_id, binding["use_case_id"], **fields)
        media = self._evidence(event, frame_id, crop,
                               "frame" if zone_id is None else "scene_region_crop", bounds,
                               packet.frame.shape, now)
        embedding = self._embedding(event, media[0], frame_id, vector,
                                    "siglip2-base-v1", "semantic", now, None)
        event["evidence_ids"] = [media[0]["evidence_id"]]
        event["embedding_ids"] = [embedding["embedding_id"]]
        self._persist(event, [media], [embedding])
        self.last_scene[(binding["use_case_id"], zone_id)] = now

    def _scene_capacity(self, now):
        if now - self.scene_pressure_checked_at >= 5 or not self.scene_pressure_checked_at:
            self.scene_pressure_checked_at = now
            pending_bytes = 0
            for directory in (self.store.evidence, self.store.outbox):
                for path in directory.iterdir():
                    try:
                        if path.is_file():
                            pending_bytes += path.stat().st_size
                    except OSError:
                        continue
            was_pressured = self.scene_pressure
            self.scene_pressure = pending_bytes >= self.scene_pressure_limit
            if was_pressured and not self.scene_pressure:
                self._persist(self._event("camera_health", now, None,
                                          state="healthy", reason="none", as_of=utc(now),
                                          last_reliable_at=utc(now), sampling_gap_seconds=0))
                self.scene_gap_started_at = None
                self.last_health = now
        if not self.scene_pressure:
            return True
        newly_pressured = self.scene_gap_started_at is None
        if self.scene_gap_started_at is None:
            self.scene_gap_started_at = now
        if newly_pressured or now - self.scene_pressure_reported_at >= 60:
            self._persist(self._event("camera_health", now, None,
                                      state="degraded", reason="backpressure", as_of=utc(now),
                                      last_reliable_at=utc(now),
                                      sampling_gap_seconds=max(0.0, now - self.scene_gap_started_at)))
            self.scene_pressure_reported_at = now
        return False
