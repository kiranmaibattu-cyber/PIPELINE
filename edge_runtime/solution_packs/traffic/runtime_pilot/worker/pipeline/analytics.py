from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .stages import InferenceStage
from .types import Detection, FramePacket

Point = Tuple[float, float]

VEHICLE_CLASSES = {"bicycle", "car", "motorcycle", "bus", "truck", "rickshaw", "minivan"}
PEDESTRIAN_CLASSES = {"pedestrian", "person"}


def geometry_id(item: Mapping[str, Any], prefix: str) -> str:
    return str(item.get("id") or f"{prefix}:{json.dumps(item.get('points', []), sort_keys=True)}")


def geometry_ref(item: Mapping[str, Any], prefix: str, index: int | None = None) -> dict:
    label_base = "zone" if prefix == "zone" else "line"
    label = f"{label_base}_{index + 1}" if index is not None else label_base
    return {
        "id": geometry_id(item, prefix),
        "label": label,
        "index": index,
        "name": item.get("name"),
        "type": prefix,
        "purpose": item.get("purpose"),
    }


def source_size(camera_config: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    processing = camera_config.get("processing") or {}
    source = camera_config.get("source") or {}
    width = processing.get("width") or source.get("width")
    height = processing.get("height") or source.get("height")
    return float(width) if width else None, float(height) if height else None


def geometry_points(
    item: Mapping[str, Any],
    frame_shape: Tuple[int, int, int],
    camera_config: Mapping[str, Any],
) -> List[Point]:
    points = item.get("points") or []
    parsed = [
        (float(point.get("x", 0)), float(point.get("y", 0)))
        for point in points
        if isinstance(point, dict)
    ]
    if item.get("shape") == "rectangle" and len(parsed) >= 2:
        (x1, y1), (x2, y2) = parsed[:2]
        parsed = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

    src_width, src_height = source_size(camera_config)
    frame_height, frame_width = frame_shape[:2]
    if parsed and all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in parsed):
        return [(x * frame_width, y * frame_height) for x, y in parsed]
    if src_width and src_height and (src_width != frame_width or src_height != frame_height):
        scale_x = frame_width / src_width
        scale_y = frame_height / src_height
        return [(x * scale_x, y * scale_y) for x, y in parsed]
    return parsed


def anchor(detection: Detection) -> Point:
    x1, y1, x2, y2 = detection.bbox
    return ((x1 + x2) / 2.0, y2)


def center(detection: Detection) -> Point:
    x1, y1, x2, y2 = detection.bbox
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        xi, yi = current
        xj, yj = previous
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        ):
            inside = not inside
        previous = current
    return inside


def line_side(point: Point, line_points: Sequence[Point]) -> float:
    if len(line_points) < 2:
        return 0.0
    (x1, y1), (x2, y2) = line_points[:2]
    return (x2 - x1) * (point[1] - y1) - (y2 - y1) * (point[0] - x1)


def side_label(point: Point, line_points: Sequence[Point]) -> str:
    side = line_side(point, line_points)
    return "side_a" if side < 0 else "side_b"


def endpoint_direction(previous: Point, current: Point, line_points: Sequence[Point]) -> str:
    if len(line_points) < 2:
        return "unknown"
    (x1, y1), (x2, y2) = line_points[:2]
    line_vector = (x2 - x1, y2 - y1)
    movement = (current[0] - previous[0], current[1] - previous[1])
    dot = movement[0] * line_vector[0] + movement[1] * line_vector[1]
    return "a_to_b" if dot >= 0 else "b_to_a"


def movement_direction(previous: Point, current: Point) -> str:
    movement = (current[0] - previous[0], current[1] - previous[1])
    angle = (math.degrees(math.atan2(movement[1], movement[0])) + 360.0) % 360.0
    directions = [
        "east",
        "south_east",
        "south",
        "south_west",
        "west",
        "north_west",
        "north",
        "north_east",
    ]
    return directions[int((angle + 22.5) // 45) % 8]


def crossing_side_direction(previous: Point, current: Point, line_points: Sequence[Point]) -> str:
    previous_side = side_label(previous, line_points)
    current_side = side_label(current, line_points)
    return f"{previous_side}_to_{current_side}"


def crossed_line(previous: Point, current: Point, line_points: Sequence[Point]) -> bool:
    previous_side = line_side(previous, line_points)
    current_side = line_side(current, line_points)
    return previous_side != 0 and current_side != 0 and previous_side * current_side < 0


def in_any_zone(
    point: Point,
    zones: Iterable[Mapping[str, Any]],
    frame_shape: Tuple[int, int, int],
    camera_config: Mapping[str, Any],
) -> bool:
    return any(
        point_in_polygon(point, geometry_points(zone, frame_shape, camera_config))
        for zone in zones
    )


def class_matches(detection: Detection, wanted: str) -> bool:
    if wanted == "vehicle":
        return detection.class_name in VEHICLE_CLASSES
    if wanted == "pedestrian":
        return detection.class_name in PEDESTRIAN_CLASSES
    return True


class DetectionGeometryFilterStage(InferenceStage):
    def __init__(self, camera_configs: Mapping[str, Mapping[str, Any]], model_name: str = "vehicle"):
        self.camera_configs = camera_configs
        self.model_name = model_name

    def process(self, packets: Sequence[FramePacket]) -> None:
        for packet in packets:
            camera_config = self.camera_configs.get(packet.name) or {}
            analytics = (camera_config.get("analytics") or {}).values()
            masks = []
            road_rois = []
            for config in analytics:
                if not config.get("enabled"):
                    continue
                masks.extend(config.get("masks") or [])
                road_rois.extend(
                    zone
                    for zone in config.get("zones") or []
                    if zone.get("type") in {"analysis_roi", "road_roi"}
                )

            kept = []
            for detection in packet.detections:
                if detection.model_name != self.model_name:
                    kept.append(detection)
                    continue

                point = anchor(detection)
                if masks and in_any_zone(point, masks, packet.frame.shape, camera_config):
                    detection.metadata["filtered_reason"] = "mask"
                    continue
                if road_rois and not in_any_zone(point, road_rois, packet.frame.shape, camera_config):
                    detection.metadata["filtered_reason"] = "outside_roi"
                    continue
                kept.append(detection)
            packet.detections = kept


class TrafficAnalyticsStage(InferenceStage):
    def __init__(self, camera_configs: Mapping[str, Mapping[str, Any]]):
        self.camera_configs = camera_configs
        self.previous_centers: Dict[Tuple[str, int], Point] = {}
        self.previous_anchors: Dict[Tuple[str, int], Point] = {}
        # event dedup keyed -> wall-clock time the event fired; entries expire after
        # EVENT_DEDUP_TTL_S so a track that re-stops / re-crosses after the TTL fires
        # again, and the sets don't grow unbounded over the worker's lifetime.
        self.counted: Dict[Tuple[str, str, str, int], float] = {}
        self.alerted: Dict[Tuple, float] = {}
        self.event_ttl_s = float(os.getenv("EVENT_DEDUP_TTL_S", "600"))
        self._last_prune = 0.0
        self.stationary: Dict[Tuple[str, str, int], Tuple[Point, int]] = {}
        self.count_totals: Dict[Tuple[str, str, str], int] = {}
        self.count_directions: Dict[Tuple[str, str, str, str], int] = {}
        self.line_side_state: Dict[Tuple[str, str, str, int], dict] = {}
        self.min_track_hits_for_count = 2
        self.line_deadband_px = 6.0
        self.min_crossing_movement_px = 8.0
        self.centroid_history = 12

    def _expire(self) -> None:
        now = time.time()
        if now - self._last_prune < 30.0:
            return
        self._last_prune = now
        cutoff = now - self.event_ttl_s
        self.counted = {k: t for k, t in self.counted.items() if t > cutoff}
        self.alerted = {k: t for k, t in self.alerted.items() if t > cutoff}

    def process(self, packets: Sequence[FramePacket]) -> None:
        self._expire()
        for packet in packets:
            camera_config = self.camera_configs.get(packet.name) or {}
            runtime = camera_config.get("runtime_analytics") or {}
            detections = [
                detection
                for detection in packet.detections
                if detection.model_name == "vehicle" and detection.metadata.get("track_id") is not None
            ]
            for detection in detections:
                track_id = int(detection.metadata["track_id"])
                current_center = center(detection)
                current_anchor = anchor(detection)
                previous_center = self.previous_centers.get((packet.name, track_id))
                previous_anchor = self.previous_anchors.get((packet.name, track_id))
                self._tag_memberships(packet, camera_config, runtime, detection)
                if previous_center is not None:
                    self._handle_counting(
                        packet,
                        camera_config,
                        runtime,
                        detection,
                        previous_center,
                        current_center,
                        previous_anchor,
                        current_anchor,
                    )
                    self._handle_wrong_way(packet, camera_config, runtime, detection, previous_center, current_center)
                self._handle_zone_alerts(packet, camera_config, runtime, detection, current_center)
                self.previous_centers[(packet.name, track_id)] = current_center
                self.previous_anchors[(packet.name, track_id)] = current_anchor
            self._handle_plate_events(packet, camera_config, runtime)
            packet.analytics_state["use_cases"] = self._use_case_state(packet.name, runtime)

    def _tag_memberships(self, packet, camera_config, runtime, detection):
        membership = {}
        point = anchor(detection)
        for use_case, config in runtime.items():
            zones = config.get("zones") or config.get("constraint_zones") or []
            zone_refs = []
            for index, zone in enumerate(zones):
                if point_in_polygon(point, geometry_points(zone, packet.frame.shape, camera_config)):
                    zone_refs.append(geometry_ref(zone, "zone", index))
            if zone_refs:
                membership[use_case] = zone_refs
        if membership:
            detection.metadata["zones"] = membership

    def _handle_counting(
        self,
        packet,
        camera_config,
        runtime,
        detection,
        previous,
        current,
        previous_anchor,
        current_anchor,
    ):
        cases = (
            ("vehicle_counting", "vehicle", "vehicle_count"),
            ("pedestrian_counting", "pedestrian", "pedestrian_count"),
        )
        for use_case, class_group, event_type in cases:
            if not class_matches(detection, class_group):
                continue
            config = runtime.get(use_case) or {}
            zones = config.get("constraint_zones") or []
            if zones and not (
                in_any_zone(previous, zones, packet.frame.shape, camera_config)
                or in_any_zone(current, zones, packet.frame.shape, camera_config)
            ):
                continue
            lines = config.get("lines") or []
            if not lines:
                self._handle_unbounded_counting(
                    packet, detection, use_case, event_type
                )
                continue
            for line_index, line in enumerate(lines):
                line_points = geometry_points(line, packet.frame.shape, camera_config)
                track_id = int(detection.metadata["track_id"])
                line_id = geometry_id(line, "line")
                count_key = (packet.name, use_case, line_id)
                track_line_key = (*count_key, track_id)
                if track_line_key in self.counted:
                    continue
                if not self._stable_line_crossing(
                    packet.name,
                    use_case,
                    line_id,
                    track_id,
                    detection,
                    previous,
                    current,
                    previous_anchor,
                    current_anchor,
                    line_points,
                ):
                    continue
                self.counted[track_line_key] = time.time()
                self.count_totals[count_key] = self.count_totals.get(count_key, 0) + 1
                state = self.line_side_state[track_line_key]
                direction = state.get("direction")
                if not direction:
                    continue
                direction_key = (*count_key, direction)
                self.count_directions[direction_key] = self.count_directions.get(direction_key, 0) + 1
                event = self._event(
                    packet,
                    detection,
                    use_case,
                    event_type,
                    line=line,
                    line_index=line_index,
                    value=self.count_totals[count_key],
                    direction=direction,
                    direction_count=self.count_directions[direction_key],
                    count_mode="line_crossing",
                )
                self._attach_object_use_case(
                    detection,
                    use_case,
                    "line_crossed",
                    event_type=event_type,
                    geometry=event.get("geometry"),
                    count={
                        "total": self.count_totals[count_key],
                        "direction": {
                            "key": direction,
                            "count": self.count_directions[direction_key],
                        },
                    },
                )
                packet.add_event(event)

    def _handle_unbounded_counting(self, packet, detection, use_case, event_type):
        """Count each stable track once when management supplies no line/ROI."""
        if int(detection.metadata.get("track_hits") or 1) < self.min_track_hits_for_count:
            return
        track_id = int(detection.metadata["track_id"])
        count_key = (packet.name, use_case, "all_tracks")
        track_key = (*count_key, track_id)
        if track_key in self.counted:
            return
        self.counted[track_key] = time.time()
        self.count_totals[count_key] = self.count_totals.get(count_key, 0) + 1
        event = self._event(
            packet,
            detection,
            use_case,
            event_type,
            value=self.count_totals[count_key],
            count_mode="unique_track",
        )
        self._attach_object_use_case(
            detection,
            use_case,
            "track_counted",
            event_type=event_type,
            count={"total": self.count_totals[count_key], "mode": "unique_track"},
        )
        packet.add_event(event)

    def _stable_line_crossing(
        self,
        camera_name,
        use_case,
        line_id,
        track_id,
        detection,
        previous,
        current,
        previous_anchor,
        current_anchor,
        line_points,
    ):
        if len(line_points) < 2:
            return False
        if int(detection.metadata.get("track_hits") or 1) < self.min_track_hits_for_count:
            return False

        previous_distance = self._signed_line_distance(previous, line_points)
        signed_distance = self._signed_line_distance(current, line_points)
        movement = math.hypot(current[0] - previous[0], current[1] - previous[1])

        key = (camera_name, use_case, line_id, track_id)
        state = self.line_side_state.setdefault(
            key,
            {
                "signed_distances": deque(maxlen=self.centroid_history),
                "centers": deque(maxlen=self.centroid_history),
                "first_center": current,
                "direction": None,
                "last_side": None,
                "last_center": None,
            },
        )

        history = state["signed_distances"]
        centers = state["centers"]
        if not history:
            history.append(previous_distance)
            centers.append(previous)
            if abs(previous_distance) >= self.line_deadband_px:
                state["last_side"] = "side_a" if previous_distance < 0 else "side_b"
                state["last_center"] = previous
        history.append(signed_distance)
        centers.append(current)

        direction = self._crossing_direction(previous, current, line_points)
        if direction is None and previous_anchor is not None and current_anchor is not None:
            direction = self._crossing_direction(previous_anchor, current_anchor, line_points)
        if direction is not None:
            current_side = direction.split("_to_", 1)[1]
            state["last_side"] = current_side
            state["last_center"] = current
            state["direction"] = direction
            return True

        if abs(signed_distance) < self.line_deadband_px:
            return False

        current_side = "side_a" if signed_distance < 0 else "side_b"
        last_side = state.get("last_side")
        last_center = state.get("last_center")
        state["last_side"] = current_side
        state["last_center"] = current

        if not last_side or last_side == current_side:
            return False

        movement_from_last_side = (
            math.hypot(current[0] - last_center[0], current[1] - last_center[1])
            if last_center is not None
            else movement
        )
        if movement_from_last_side < self.min_crossing_movement_px:
            return False
        if last_center is not None and not self._segments_intersect(
            last_center,
            current,
            line_points[0],
            line_points[1],
        ):
            return False

        state["direction"] = f"{last_side}_to_{current_side}"
        return True

    def _crossing_direction(self, previous, current, line_points):
        if len(line_points) < 2:
            return None
        previous_distance = self._signed_line_distance(previous, line_points)
        current_distance = self._signed_line_distance(current, line_points)
        movement = math.hypot(current[0] - previous[0], current[1] - previous[1])
        if movement < self.min_crossing_movement_px:
            return None
        if previous_distance * current_distance >= 0:
            return None
        if not self._segments_intersect(previous, current, line_points[0], line_points[1]):
            return None
        previous_side = "side_a" if previous_distance < 0 else "side_b"
        current_side = "side_a" if current_distance < 0 else "side_b"
        return f"{previous_side}_to_{current_side}"

    @staticmethod
    def _segments_intersect(a, b, c, d):
        def orient(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

        def on_segment(p, q, r):
            return (
                min(p[0], r[0]) - 1e-6 <= q[0] <= max(p[0], r[0]) + 1e-6
                and min(p[1], r[1]) - 1e-6 <= q[1] <= max(p[1], r[1]) + 1e-6
            )

        o1 = orient(a, b, c)
        o2 = orient(a, b, d)
        o3 = orient(c, d, a)
        o4 = orient(c, d, b)

        if o1 * o2 < 0 and o3 * o4 < 0:
            return True
        if abs(o1) < 1e-6 and on_segment(a, c, b):
            return True
        if abs(o2) < 1e-6 and on_segment(a, d, b):
            return True
        if abs(o3) < 1e-6 and on_segment(c, a, d):
            return True
        if abs(o4) < 1e-6 and on_segment(c, b, d):
            return True
        return False

    @staticmethod
    def _line_distance(point, line_points):
        if len(line_points) < 2:
            return 0.0
        (x1, y1), (x2, y2) = line_points[:2]
        length = math.hypot(x2 - x1, y2 - y1) or 1.0
        return abs(line_side(point, line_points)) / length

    @staticmethod
    def _signed_line_distance(point, line_points):
        if len(line_points) < 2:
            return 0.0
        (x1, y1), (x2, y2) = line_points[:2]
        length = math.hypot(x2 - x1, y2 - y1) or 1.0
        return line_side(point, line_points) / length

    def _handle_wrong_way(self, packet, camera_config, runtime, detection, previous, current):
        if not class_matches(detection, "vehicle"):
            return
        config = runtime.get("wrong_way_driving_detection") or {}
        movement = (current[0] - previous[0], current[1] - previous[1])
        length = math.hypot(*movement)
        if length < 3:
            return
        point = anchor(detection)
        zones = config.get("constraint_zones") or []
        if zones and not in_any_zone(point, zones, packet.frame.shape, camera_config):
            return

        for line_index, line in enumerate(config.get("lines") or []):
            line_points = geometry_points(line, packet.frame.shape, camera_config)
            if len(line_points) < 2:
                continue
            (x1, y1), (x2, y2) = line_points[:2]
            direction = line.get("direction") or "a_to_b"
            vector = (x2 - x1, y2 - y1)
            if direction == "b_to_a":
                vector = (-vector[0], -vector[1])
            dot = movement[0] * vector[0] + movement[1] * vector[1]
            if direction != "both" and dot <= 0:
                continue
            key = (packet.name, "wrong_way_driving_detection", geometry_id(line, "line"), int(detection.metadata["track_id"]))
            if key in self.alerted:
                continue
            self.alerted[key] = time.time()
            event = self._event(
                packet,
                detection,
                "wrong_way_driving_detection",
                "wrong_way",
                line=line,
                line_index=line_index,
            )
            self._attach_object_use_case(
                detection,
                "wrong_way_driving_detection",
                "violation",
                event_type="wrong_way",
                geometry=event.get("geometry"),
                violation=True,
            )
            packet.add_event(event)

    def _handle_zone_alerts(self, packet, camera_config, runtime, detection, current):
        point = anchor(detection)
        zone_cases = (
            ("vehicle_in_pedestrian_zone_alert", "vehicle", "vehicle_in_pedestrian_zone", 1),
            ("parking_violation_detection", "vehicle", "parking_violation", 150),
            ("stopped_vehicle_detection", "vehicle", "stopped_vehicle", 90),
        )
        for use_case, class_group, event_type, min_frames in zone_cases:
            if not class_matches(detection, class_group):
                continue
            config = runtime.get(use_case) or {}
            now = time.monotonic()
            for zone_index, zone in enumerate(config.get("zones") or []):
                zone_id = geometry_id(zone, "zone")
                if not point_in_polygon(point, geometry_points(zone, packet.frame.shape, camera_config)):
                    continue
                track_id = int(detection.metadata["track_id"])
                state_key = (packet.name, f"{use_case}:{zone_id}", track_id)
                if event_type == "stopped_vehicle":
                    threshold_seconds = float(
                        config.get("threshold_seconds")
                        or config.get("stopped_seconds")
                        or config.get("duration_seconds")
                        or 10.0
                    )
                    state = self.stationary.get(
                        state_key,
                        {
                            "first_seen": now,
                            "last_center": current,
                            "duration": 0.0,
                        },
                    )
                    last_center = state.get("last_center", current)
                    if math.hypot(current[0] - last_center[0], current[1] - last_center[1]) > 8:
                        state["first_seen"] = now
                    state["last_center"] = current
                    state["duration"] = max(0.0, now - float(state.get("first_seen", now)))
                    self.stationary[state_key] = state
                    if state["duration"] < threshold_seconds:
                        continue
                elif event_type == "parking_violation":
                    state = self.stationary.get(state_key, {"first_seen": now, "duration": 0.0})
                    state["duration"] = max(0.0, now - float(state.get("first_seen", now)))
                    self.stationary[state_key] = state
                    frames = int(state["duration"] * 10)
                    if frames < min_frames:
                        continue
                details = {"violation": True}
                state = self.stationary.get(state_key)
                if isinstance(state, dict) and state.get("duration") is not None:
                    details["duration_seconds"] = round(float(state.get("duration") or 0.0), 1)
                event_geometry = geometry_ref(zone, "zone", zone_index)
                self._attach_object_use_case(
                    detection,
                    use_case,
                    "violation",
                    event_type=event_type,
                    geometry=event_geometry,
                    **details,
                )
                alert_key = (packet.name, use_case, zone_id, track_id)
                if alert_key in self.alerted:
                    continue
                self.alerted[alert_key] = time.time()
                event = self._event(
                    packet,
                    detection,
                    use_case,
                    event_type,
                    zone=zone,
                    zone_index=zone_index,
                )
                if details.get("duration_seconds") is not None:
                    event["duration_seconds"] = details["duration_seconds"]
                packet.add_event(event)

    def _handle_plate_events(self, packet, camera_config, runtime):
        plate_config = runtime.get("plate_detection") or {}
        if not plate_config:
            return
        zones = plate_config.get("zones") or []
        for detection in packet.detections:
            if detection.model_name != "license_plate":
                continue
            if zones and not in_any_zone(center(detection), zones, packet.frame.shape, camera_config):
                continue
            stable_text = str(detection.metadata.get("ocr_text") or "").strip()
            if not stable_text:
                continue
            event_key = (
                packet.name,
                "plate_detection",
                str(detection.parent_id or detection.metadata.get("ocr_history_key")),
                stable_text,
            )
            if event_key in self.alerted:
                continue
            self.alerted[event_key] = time.time()
            event = self._event(
                packet,
                detection,
                "plate_detection",
                "plate_read",
            )
            event["plate_text"] = stable_text
            event["subject"]["parent_track_id"] = detection.parent_id
            detection.metadata["reported"] = True
            packet.add_event(event)

    def _use_case_state(self, camera_name, runtime):
        states = {}
        for use_case, config in runtime.items():
            line_states = []
            for line_index, line in enumerate(config.get("lines") or []):
                line_id = geometry_id(line, "line")
                line_state = {
                    "geometry_id": line_id,
                    "geometry": geometry_ref(line, "line", line_index),
                }
                if use_case in {"vehicle_counting", "pedestrian_counting"}:
                    line_state["count"] = self.count_totals.get((camera_name, use_case, line_id), 0)
                    line_state["directions"] = self._direction_counts(camera_name, use_case, line_id)
                line_states.append(line_state)
            zone_states = [
                {
                    "geometry_id": geometry_id(zone, "zone"),
                    "geometry": geometry_ref(zone, "zone", zone_index),
                }
                for zone_index, zone in enumerate(config.get("zones") or config.get("constraint_zones") or [])
            ]
            states[use_case] = {
                "enabled": True,
                "geometry": line_states + zone_states,
            }
        return states

    @staticmethod
    def _attach_object_use_case(
        detection,
        use_case,
        state,
        event_type=None,
        geometry=None,
        violation=False,
        count=None,
        duration_seconds=None,
    ):
        use_cases = detection.metadata.setdefault("use_cases", {})
        payload = {
            "state": state,
            "violation": bool(violation),
        }
        if event_type:
            payload["event_type"] = event_type
        if geometry:
            payload["location"] = geometry
        if count:
            payload["count"] = count
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        use_cases[use_case] = payload

    @staticmethod
    def _event(
        packet,
        detection,
        use_case,
        event_type,
        line=None,
        zone=None,
        value=None,
        direction=None,
        direction_count=None,
        line_index=None,
        zone_index=None,
        count_mode=None,
    ):
        observed_at = datetime.now(timezone.utc).isoformat()
        event = {
            "observation_id": f"{packet.name}:{use_case}:{event_type}:{detection.metadata.get('track_id') or detection.parent_id}:{observed_at}",
            "observed_at": observed_at,
            "use_case": use_case,
            "type": event_type,
            "subject": {
                "track_id": detection.metadata.get("track_id"),
                "parent_track_id": detection.parent_id,
                "class": detection.class_name,
                "confidence": round(float(detection.confidence), 4),
                "bbox": detection.bbox,
            },
            "geometry": {},
        }
        if line is not None:
            event["geometry"] = geometry_ref(line, "line", line_index)
        if zone is not None:
            event["geometry"] = geometry_ref(zone, "zone", zone_index)
        if value is not None:
            event["value"] = value
        if count_mode is not None:
            event["count_mode"] = count_mode
        if direction is not None:
            event["direction"] = direction
            event["direction_count"] = direction_count
        return event

    def _direction_counts(self, camera_name, use_case, line_id):
        prefix = (camera_name, use_case, line_id)
        counts = {}
        for key, value in self.count_directions.items():
            if key[:3] == prefix:
                counts[key[3]] = value
        return counts


def detection_in_plate_roi(
    camera_configs: Mapping[str, Mapping[str, Any]],
):
    def _filter(packet: FramePacket, detection: Detection) -> bool:
        camera_config = camera_configs.get(packet.name) or {}
        runtime = camera_config.get("runtime_analytics") or {}
        plate_config = runtime.get("plate_detection")
        if not plate_config:
            return False
        zones = plate_config.get("zones") or []
        if not zones:
            return True
        return in_any_zone(anchor(detection), zones, packet.frame.shape, camera_config)

    return _filter
