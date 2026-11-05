"""Bounded per-camera production-policy resolver for Sentinel V7.

Collection gates may adapt to the observed camera view. Confirmation and
emission remain conservative: poor quality increases observation time or
suppresses a result; it never turns weak evidence into a Management fact.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
import statistics
import time
from typing import Any, Iterable

import cv2


def _median(values: Iterable[float], default: float = 0.0) -> float:
    values = list(values)
    return float(statistics.median(values)) if values else float(default)


def _ratio(numerator: float, denominator: float) -> float:
    return max(0.0, min(1.0, float(numerator) / max(1.0, float(denominator))))


class AdaptivePolicyResolver:
    """Resolve bounded gates from recent camera and use-case measurements."""

    _PROFILES: dict[str, dict[str, dict[str, Any]]] = {
        "strict": {
            "presence": {"stable_hits": 3, "unobserved_after_seconds": 2.0,
                         "ended_unknown_after_seconds": 8.0},
            "zone": {"boundary_margin": 0.008, "transition_votes": 2,
                     "transition_hold_seconds": 0.25},
            "count": {"debounce_observations": 2, "debounce_seconds": 0.25,
                      "heartbeat_seconds": 20.0},
            "anpr": {"candidate_window_seconds": 1.5, "ocr_emit_min_confidence": 0.40,
                     "confirm_min_reads": 3, "min_plate_width_px": 32.0,
                     "min_plate_height_px": 8.0, "min_sharpness": 20.0,
                     "single_read_confidence": 0.88},
            "identity": {"face_min_quality": 0.40, "face_emit_min_width_px": 48.0,
                         "face_selection_window_seconds": 1.5,
                         "body_quality_floor": 0.25, "body_min_width_px": 40.0,
                         "body_min_height_px": 80.0, "gait_min_silhouettes": 20,
                         "feature_cooldown_seconds": 5.0},
            "fire_smoke": {"event_min_confidence": 0.62,
                           "positive_confirmations": 4,
                           "positive_window_seconds": 3.0,
                           "clear_seconds": 10.0, "clear_confirmations": 5,
                           "clear_max_evaluation_gap_seconds": 2.5},
        },
        "balanced": {
            "presence": {"stable_hits": 3, "unobserved_after_seconds": 5.0,
                         "ended_unknown_after_seconds": 18.0},
            "zone": {"boundary_margin": 0.012, "transition_votes": 2,
                     "transition_hold_seconds": 0.50},
            "count": {"debounce_observations": 2, "debounce_seconds": 0.25,
                      "heartbeat_seconds": 20.0},
            "anpr": {"candidate_window_seconds": 2.5, "ocr_emit_min_confidence": 0.35,
                     "confirm_min_reads": 2, "min_plate_width_px": 22.0,
                     "min_plate_height_px": 7.0, "min_sharpness": 8.0,
                     "single_read_confidence": 0.82},
            "identity": {"face_min_quality": 0.30, "face_emit_min_width_px": 32.0,
                         "face_selection_window_seconds": 1.5,
                         "body_quality_floor": 0.20, "body_min_width_px": 32.0,
                         "body_min_height_px": 64.0, "gait_min_silhouettes": 20,
                         "feature_cooldown_seconds": 5.0},
            "fire_smoke": {"event_min_confidence": 0.65,
                           "positive_confirmations": 5,
                           "positive_window_seconds": 3.0,
                           "clear_seconds": 10.0, "clear_confirmations": 5,
                           "clear_max_evaluation_gap_seconds": 2.5},
        },
        "tolerant_collection": {
            "presence": {"stable_hits": 4, "unobserved_after_seconds": 5.0,
                         "ended_unknown_after_seconds": 20.0},
            "zone": {"boundary_margin": 0.018, "transition_votes": 4,
                     "transition_hold_seconds": 0.80},
            "count": {"debounce_observations": 4, "debounce_seconds": 0.80,
                      "heartbeat_seconds": 20.0},
            # Collection gets longer/more permissive; final truth floors do not.
            "anpr": {"candidate_window_seconds": 3.5, "ocr_emit_min_confidence": 0.35,
                     "confirm_min_reads": 2, "min_plate_width_px": 16.0,
                     "min_plate_height_px": 5.0, "min_sharpness": 5.0,
                     "single_read_confidence": 0.82},
            "identity": {"face_min_quality": 0.30, "face_emit_min_width_px": 28.0,
                         "face_selection_window_seconds": 2.5,
                         "body_quality_floor": 0.20, "body_min_width_px": 28.0,
                         "body_min_height_px": 56.0, "gait_min_silhouettes": 24,
                         "feature_cooldown_seconds": 5.0},
            "fire_smoke": {"event_min_confidence": 0.70,
                           "positive_confirmations": 6,
                           "positive_window_seconds": 4.0,
                           "clear_seconds": 10.0, "clear_confirmations": 5,
                           "clear_max_evaluation_gap_seconds": 2.5},
        },
    }

    _OBJECT_SECTIONS = {"presence", "zone", "count"}

    def __init__(self, camera_id: str, *, target_fps: float) -> None:
        self.camera_id = str(camera_id)
        self.target_fps = max(1.0, float(target_fps))
        self._object_scales = {"person": deque(maxlen=600), "vehicle": deque(maxlen=600)}
        self._object_width_ratios = {"person": deque(maxlen=600), "vehicle": deque(maxlen=600)}
        self._object_height_ratios = {"person": deque(maxlen=600), "vehicle": deque(maxlen=600)}
        self._dropout_ratios: deque[float] = deque(maxlen=120)
        self._frame_times: deque[float] = deque(maxlen=300)
        self._frame_sharpness: deque[float] = deque(maxlen=60)
        self._frame_exposure: deque[float] = deque(maxlen=60)
        self._plate_widths: deque[float] = deque(maxlen=120)
        self._plate_heights: deque[float] = deque(maxlen=120)
        self._plate_width_ratios: deque[float] = deque(maxlen=120)
        self._plate_sharpness: deque[float] = deque(maxlen=120)
        self._ocr_confidence: deque[float] = deque(maxlen=120)
        self._ocr_texts: deque[str] = deque(maxlen=120)
        self._anpr_outcomes: deque[int] = deque(maxlen=60)
        self._face_widths: deque[float] = deque(maxlen=120)
        self._face_width_ratios: deque[float] = deque(maxlen=120)
        self._face_qualities: deque[float] = deque(maxlen=120)
        self._face_pose_qualities: deque[float] = deque(maxlen=120)
        self._face_detection_ratios: deque[float] = deque(maxlen=60)
        self._fragmentation_times: deque[float] = deque(maxlen=300)
        self._frames = 0
        self._frames_with_objects = 0
        self._last_image_sample: float | None = None
        self._last_evaluation: float | None = None
        self._modes = {name: "balanced" for name in
                       ("tracking", "presence", "zone", "count", "anpr", "identity", "fire_smoke")}
        self._object_modes = {
            section: {kind: "balanced" for kind in ("person", "vehicle")}
            for section in self._OBJECT_SECTIONS
        }
        self._pending: dict[str, tuple[str, int]] = {}
        self._tracker_effective = {"max_distance_px": 320.0, "max_disappeared_seconds": 9.0}
        self._outbox_pressure_ratio = 0.0
        self._model_queue_wait_seconds = 0.0
        self._pressure_mode = "normal"
        self._reasons: dict[str, list[str]] = {}
        self._revision = 1
        self._changed_at = time.time()
        self._last_now = self._changed_at

    def image_sample_due(self, now: float) -> bool:
        """Return whether the unchanged one-second image-quality sample is due."""
        return (
            self._last_image_sample is None
            or float(now) - self._last_image_sample >= 1.0
        )

    def observe_frame(self, frame, detections, now: float,
                      tracker_policy: dict[str, Any] | None = None,
                      tracker_observation: dict[str, Any] | None = None,
                      model_queue_wait_seconds: float | None = None,
                      frame_shape=None) -> bool:
        self._last_now = float(now)
        self._frames += 1
        self._frame_times.append(float(now))
        shape = frame.shape if frame is not None else frame_shape
        if shape is None or len(shape) < 2:
            raise ValueError("adaptive policy requires source frame geometry")
        height, width = shape[:2]
        objects = [item for item in detections if getattr(item, "model_name", None) == "vehicle"
                   and not getattr(item, "metadata", {}).get("predicted")]
        if objects:
            self._frames_with_objects += 1
        frame_area = max(1.0, float(width * height))
        for item in objects:
            x1, y1, x2, y2 = item.bbox
            box_width = max(1.0, float(x2 - x1))
            box_height = max(1.0, float(y2 - y1))
            kind = "person" if getattr(item, "class_name", "") == "pedestrian" else "vehicle"
            self._object_scales[kind].append(math.sqrt(box_width * box_height / frame_area))
            self._object_width_ratios[kind].append(box_width / max(1.0, width))
            self._object_height_ratios[kind].append(box_height / max(1.0, height))
        if tracker_observation and tracker_observation.get("detector_evaluated", True):
            active = max(0, int(tracker_observation.get("active_tracks", 0)))
            matched = max(0, int(tracker_observation.get("matched_tracks", 0)))
            if active:
                self._dropout_ratios.append(_ratio(active - min(active, matched), active))
        if model_queue_wait_seconds is not None and math.isfinite(float(model_queue_wait_seconds)):
            self._model_queue_wait_seconds = max(0.0, float(model_queue_wait_seconds))
            self._resolve_pressure()
        has_pixels = (
            frame is not None
            and getattr(frame, "size", 0) > 0
            and getattr(frame, "ndim", 0) == 3
            and frame.shape[2] >= 3
        )
        if self.image_sample_due(now) and has_pixels:
            self._last_image_sample = now
            sample = frame
            if width > 640:
                sample = cv2.resize(frame, (640, max(1, round(height * 640 / width))))
            gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
            self._frame_sharpness.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
            self._frame_exposure.append(max(0.0, 1.0 - abs(float(gray.mean()) - 127.5) / 127.5))
        if tracker_policy:
            for key in ("max_distance_px", "max_disappeared_seconds"):
                value = tracker_policy.get(key)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    self._tracker_effective[key] = round(float(value), 3)
        if self._last_evaluation is None:
            self._last_evaluation = now
            return False
        if now - self._last_evaluation < 5.0:
            return False
        self._last_evaluation = now
        return self._resolve(now)

    def observe_fragmentation(self, now: float) -> None:
        self._last_now = float(now)
        self._fragmentation_times.append(float(now))

    def observe_plate(self, *, width: float, height: float = 0.0,
                      frame_width: float = 0.0, sharpness: float,
                      ocr_confidence: float | None = None,
                      text: str | None = None) -> None:
        width = max(0.0, float(width))
        self._plate_widths.append(width)
        self._plate_heights.append(max(0.0, float(height)))
        if frame_width > 0:
            self._plate_width_ratios.append(width / float(frame_width))
        self._plate_sharpness.append(max(0.0, float(sharpness)))
        if ocr_confidence is not None:
            self._ocr_confidence.append(max(0.0, min(1.0, float(ocr_confidence))))
        if text:
            self._ocr_texts.append(str(text))

    def observe_anpr_outcome(self, readable: bool) -> None:
        self._anpr_outcomes.append(1 if readable else 0)

    def observe_face(self, face, frame_shape) -> None:
        _frame_height, frame_width = frame_shape[:2]
        x1, _y1, x2, _y2 = face.bbox
        width = max(0.0, float(x2 - x1))
        self._face_widths.append(width)
        self._face_width_ratios.append(width / max(1.0, float(frame_width)))
        self._face_qualities.append(max(0.0, min(1.0, float(face.quality))))
        pose_quality = getattr(face, "pose_quality", 1.0)
        self._face_pose_qualities.append(max(0.0, min(1.0, float(pose_quality))))

    def observe_face_pipeline(self, observation: dict[str, Any] | None) -> None:
        if not observation or not observation.get("evaluated"):
            return
        due = max(0, int(observation.get("due_tracks", 0)))
        detected = max(0, int(observation.get("detected_faces", 0)))
        if due:
            self._face_detection_ratios.append(_ratio(detected, due))

    def observe_pressure(self, *, outbox_pending_bytes: int,
                         outbox_limit_bytes: int,
                         model_queue_wait_seconds: float | None = None) -> None:
        self._outbox_pressure_ratio = max(
            0.0, float(outbox_pending_bytes) / max(1.0, float(outbox_limit_bytes))
        )
        if model_queue_wait_seconds is not None:
            self._model_queue_wait_seconds = max(0.0, float(model_queue_wait_seconds))
        self._resolve_pressure()

    def _resolve_pressure(self) -> None:
        frame_period = 1.0 / self.target_fps
        pressure = max(self._outbox_pressure_ratio,
                       self._model_queue_wait_seconds / max(frame_period, 1e-6))
        self._pressure_mode = "critical" if pressure >= 1.0 else "elevated" if pressure >= 0.5 else "normal"

    def section(self, name: str, object_type: str | None = None) -> dict[str, Any]:
        mode = self._modes.get(name, "balanced")
        if name in self._OBJECT_SECTIONS and object_type in {"person", "vehicle"}:
            mode = self._object_modes[name][str(object_type)]
        source = self._PROFILES[mode].get(name)
        if source is None and name == "tracking":
            return {"mode": mode, **self._tracker_effective}
        return {"mode": mode, **deepcopy(source or {})}

    def snapshot(self) -> dict[str, Any]:
        now = self._last_now
        recent_fragments = sum(1 for value in self._fragmentation_times if now - value <= 60.0)
        person_scale = _median(self._object_scales["person"])
        vehicle_scale = _median(self._object_scales["vehicle"])
        combined = list(self._object_scales["person"]) + list(self._object_scales["vehicle"])
        return {
            "schema_version": "1.1", "camera_id": self.camera_id,
            "policy_revision": self._revision, "changed_at": self._utc(self._changed_at),
            "bounded_adaptation": True, "modes": dict(self._modes),
            "object_modes": deepcopy(self._object_modes),
            "decision_reasons": deepcopy(self._reasons),
            "measurements": {
                "frames_observed": self._frames,
                "observed_processing_fps": round(self._observed_fps(), 3),
                "target_processing_fps": self.target_fps,
                "frames_with_objects_ratio": round(self._frames_with_objects / max(1, self._frames), 4),
                "median_object_scale": round(_median(combined), 5),
                "median_person_scale": round(person_scale, 5),
                "median_vehicle_scale": round(vehicle_scale, 5),
                "median_person_width_ratio": round(_median(self._object_width_ratios["person"]), 5),
                "median_person_height_ratio": round(_median(self._object_height_ratios["person"]), 5),
                "median_vehicle_width_ratio": round(_median(self._object_width_ratios["vehicle"]), 5),
                "median_vehicle_height_ratio": round(_median(self._object_height_ratios["vehicle"]), 5),
                "real_detection_dropout_ratio": round(_median(self._dropout_ratios), 4),
                "median_frame_sharpness": round(_median(self._frame_sharpness), 3),
                "median_exposure_quality": round(_median(self._frame_exposure), 4),
                "median_plate_width_px": round(_median(self._plate_widths), 2),
                "median_plate_height_px": round(_median(self._plate_heights), 2),
                "median_plate_width_ratio": round(_median(self._plate_width_ratios), 6),
                "median_plate_sharpness": round(_median(self._plate_sharpness), 3),
                "median_ocr_confidence": round(_median(self._ocr_confidence), 4),
                "ocr_text_stability_ratio": round(self._ocr_stability(), 4),
                "anpr_unreadable_ratio": round(self._unreadable_ratio(), 4),
                "median_face_width_px": round(_median(self._face_widths), 2),
                "median_face_width_ratio": round(_median(self._face_width_ratios), 6),
                "median_face_quality": round(_median(self._face_qualities), 4),
                "median_face_pose_quality": round(_median(self._face_pose_qualities), 4),
                "face_detection_success_ratio": round(_median(self._face_detection_ratios, 1.0), 4),
                "track_fragmentations_last_60s": recent_fragments,
                "outbox_pressure_ratio": round(self._outbox_pressure_ratio, 4),
                "model_queue_wait_seconds": round(self._model_queue_wait_seconds, 6),
            },
            "tracking": self.section("tracking"),
            "presence": self.section("presence"), "zone": self.section("zone"),
            "count": self.section("count"), "anpr": self.section("anpr"),
            "identity": self.section("identity"), "fire_smoke": self.section("fire_smoke"),
            "object_profiles": {
                kind: {section: self.section(section, kind) for section in self._OBJECT_SECTIONS}
                for kind in ("person", "vehicle")
            },
            "pressure": {"mode": self._pressure_mode,
                         "outbox_ratio": round(self._outbox_pressure_ratio, 4),
                         "model_queue_wait_seconds": round(self._model_queue_wait_seconds, 6)},
            "scene": {
                "interval_source": "desired_state",
                "pressure_interval_multiplier": (
                    4.0 if self._pressure_mode == "critical"
                    else 2.0 if self._pressure_mode == "elevated" else 1.0
                ),
                "optional_sampling_allowed": self._pressure_mode != "critical",
                "event_trigger_cooldown_seconds": 1.0,
                "outbox_pressure_limit_bytes": 128 * 1024 ** 2,
            },
        }

    def _observed_fps(self) -> float:
        if len(self._frame_times) < 2:
            return 0.0
        elapsed = self._frame_times[-1] - self._frame_times[0]
        return (len(self._frame_times) - 1) / elapsed if elapsed > 0 else 0.0

    def _ocr_stability(self) -> float:
        if not self._ocr_texts:
            return 0.0
        counts: dict[str, int] = {}
        for text in self._ocr_texts:
            counts[text] = counts.get(text, 0) + 1
        return max(counts.values()) / len(self._ocr_texts)

    def _unreadable_ratio(self) -> float:
        if not self._anpr_outcomes:
            return 0.0
        return 1.0 - sum(self._anpr_outcomes) / len(self._anpr_outcomes)

    def _scale_mode(self, scale: float, dropout: float, fragments: int) -> tuple[str, list[str]]:
        reasons = []
        if scale and scale < 0.075:
            reasons.append("distant_objects")
        if dropout >= 0.35:
            reasons.append("high_detection_dropout")
        if fragments >= 8:
            reasons.append("frequent_track_fragmentation")
        if reasons:
            return "tolerant_collection", reasons
        if scale >= 0.18 and dropout <= 0.08 and fragments <= 1:
            return "strict", ["large_stable_objects"]
        return "balanced", ["normal_object_geometry"]

    def _request_mode(self, key: str, desired: str) -> bool:
        if key in self._modes:
            current = self._modes[key]
        else:
            section, kind = key.rsplit("_", 1)
            current = self._object_modes[section][kind]
        if desired == current:
            self._pending.pop(key, None)
            return False
        previous, votes = self._pending.get(key, (desired, 0))
        votes = votes + 1 if previous == desired else 1
        self._pending[key] = (desired, votes)
        if votes < 2:
            return False
        if key in self._modes:
            self._modes[key] = desired
        else:
            section, kind = key.rsplit("_", 1)
            self._object_modes[section][kind] = desired
        self._pending.pop(key, None)
        return True

    def _resolve(self, now: float) -> bool:
        while self._fragmentation_times and now - self._fragmentation_times[0] > 60.0:
            self._fragmentation_times.popleft()
        fragments = len(self._fragmentation_times)
        dropout = _median(self._dropout_ratios)
        person_scale = _median(self._object_scales["person"])
        vehicle_scale = _median(self._object_scales["vehicle"])
        combined_scale = _median(list(self._object_scales["person"]) + list(self._object_scales["vehicle"]))
        tracking, tracking_reasons = self._scale_mode(combined_scale, dropout, fragments)
        requested: dict[str, tuple[str, list[str]]] = {
            "tracking": (tracking, tracking_reasons), "presence": (tracking, tracking_reasons),
            "zone": (tracking, tracking_reasons), "count": (tracking, tracking_reasons),
        }
        for section in self._OBJECT_SECTIONS:
            for kind, scale in (("person", person_scale), ("vehicle", vehicle_scale)):
                requested[f"{section}_{kind}"] = self._scale_mode(
                    scale or combined_scale, dropout, fragments
                )

        plate_width, plate_height = _median(self._plate_widths), _median(self._plate_heights)
        plate_width_ratio = _median(self._plate_width_ratios)
        plate_sharpness = _median(self._plate_sharpness)
        ocr_confidence = _median(self._ocr_confidence, 1.0)
        stability = self._ocr_stability()
        unreadable = self._unreadable_ratio()
        if self._plate_widths:
            if (plate_width >= 48 and plate_height >= 12 and plate_sharpness >= 40
                    and (not self._plate_width_ratios or plate_width_ratio >= .015)
                    and ocr_confidence >= .55 and (len(self._ocr_texts) < 3 or stability >= .75)
                    and unreadable <= .20):
                anpr, anpr_reasons = "strict", ["large_clear_stable_plate"]
            elif (plate_width < 24 or plate_height < 7
                  or (plate_width_ratio and plate_width_ratio < .006)
                  or plate_sharpness < 10
                  or ocr_confidence < .35 or (len(self._ocr_texts) >= 4 and stability < .45)
                  or unreadable >= .60):
                anpr, anpr_reasons = "tolerant_collection", ["plate_observations_need_longer_collection"]
            else:
                anpr, anpr_reasons = "balanced", ["normal_plate_quality"]
        else:
            anpr, anpr_reasons = tracking, ["no_plate_history", *tracking_reasons]
        requested["anpr"] = (anpr, anpr_reasons)

        sharpness = _median(self._frame_sharpness)
        exposure = _median(self._frame_exposure, 0.5)
        face_width = _median(self._face_widths)
        face_width_ratio = _median(self._face_width_ratios)
        face_quality = _median(self._face_qualities, 1.0)
        pose_quality = _median(self._face_pose_qualities, 1.0)
        face_success = _median(self._face_detection_ratios, 1.0)
        if ((person_scale and person_scale < .08) or sharpness < 20 or exposure < .25
                or (self._face_widths and (face_width < 36 or face_quality < .40
                                           or face_width_ratio < .018
                                           or pose_quality < .45 or face_success < .40))):
            identity, identity_reasons = "tolerant_collection", ["identity_view_needs_longer_selection"]
        elif (person_scale >= .16 and sharpness >= 60 and exposure >= .45
              and (not self._face_widths or (face_width >= 72 and face_width_ratio >= .035
                                             and face_quality >= .65
                                             and pose_quality >= .75))):
            identity, identity_reasons = "strict", ["large_clear_identity_samples"]
        else:
            identity, identity_reasons = "balanced", ["normal_identity_quality"]
        requested["identity"] = (identity, identity_reasons)

        hazard = "tolerant_collection" if sharpness < 25 or exposure < .25 else "balanced"
        requested["fire_smoke"] = (
            hazard, ["poor_scene_visibility"] if hazard != "balanced" else ["normal_scene_visibility"]
        )
        changed = False
        for key, (desired, reasons) in requested.items():
            self._reasons[key] = reasons
            changed = self._request_mode(key, desired) or changed
        if changed:
            self._revision += 1
            self._changed_at = now
        return changed

    @staticmethod
    def _utc(value: float) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
