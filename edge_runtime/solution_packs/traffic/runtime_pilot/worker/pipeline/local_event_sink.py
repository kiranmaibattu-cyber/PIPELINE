from __future__ import annotations

import json
import os
import threading
import uuid
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2


class LocalManagementEventSink:
    """Writes Traffic Pilot analytics events and snapshots to /state/traffic."""

    def __init__(self, state_dir: str | Path, solution_pack: str = "traffic") -> None:
        self.state_dir = Path(state_dir)
        self.solution_pack = solution_pack
        self.events_path = self.state_dir / "events.jsonl"
        self.snapshot_dir = self.state_dir / "snapshots"
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.session_id = os.getenv("EDGE_RUNTIME_SESSION_ID") or uuid.uuid4().hex
        self._vehicle_evidence: dict[str, dict[str, Any]] = {}
        self._parking_plates = OrderedDict()

    @classmethod
    def from_env(cls) -> "LocalManagementEventSink | None":
        state_dir = os.getenv("MANAGEMENT_STATE_DIR") or os.getenv("STATE_DIR")
        if not state_dir:
            return None
        return cls(state_dir)

    def publish_packet(self, packet, events: list[dict[str, Any]], *, retain_parking_plates=True) -> None:
        if retain_parking_plates:
            self._observe_plates(packet)
        if not events:
            return
        for index, event in enumerate(events):
            row = dict(event)
            row.setdefault("event_id", str(uuid.uuid4()))
            row.setdefault("runtime_session_id", self.session_id)
            row.setdefault("solution_pack", self.solution_pack)
            row.setdefault("camera_id", (row.get("camera") or {}).get("id") or packet.name)
            row.setdefault("app_id", row.get("use_case"))
            row["event_type"] = row.get("event_type") or row.get("type")
            row.setdefault(
                "timestamp_utc",
                row.get("observed_at") or row.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            )
            vehicle_track_id = _vehicle_track_id(row)
            vehicle_ref = self._vehicle_ref(row["camera_id"], vehicle_track_id)
            if vehicle_ref:
                row["vehicle_ref"] = vehicle_ref
                row["vehicle_track_id"] = vehicle_track_id
            snapshot_refs = self._save_snapshots(packet, row, index, vehicle_track_id)
            plate = _plate_evidence(packet, vehicle_track_id)
            if row["event_type"] in {"parking_violation", "illegal_parking", "illegal_parking_event"}:
                cached = self._parking_plates.get(vehicle_ref)
                if "plate_crop" in snapshot_refs:
                    row["plate_evidence"] = {
                        "basis": "event_frame", "observed_at": row["timestamp_utc"],
                        "frame_id": packet.index, "vehicle_ref": vehicle_ref,
                    }
                elif cached:
                    image = cv2.imdecode(cached["jpeg"], cv2.IMREAD_COLOR)
                    ref = self._save_image(image, packet, row, index, "earlier_plate")
                    if ref:
                        snapshot_refs["plate_crop"] = ref
                        row["plate_evidence"] = {
                            "basis": "earlier_track_frame", "observed_at": cached["observed_at"],
                            "frame_id": cached["frame_id"], "vehicle_ref": vehicle_ref,
                        }
                if plate is None and cached and cached.get("plate"):
                    plate = cached["plate"]
                row["plate_status"] = (
                    "recognized" if plate else "detected_unreadable"
                    if "plate_crop" in snapshot_refs else "not_visible"
                )
            if isinstance(row.get("plate"), dict):
                plate = {**(plate or {}), **row["plate"]}
            if vehicle_ref:
                evidence = self._remember_vehicle_evidence(vehicle_ref, plate, snapshot_refs)
                if row["event_type"] in {"parking_violation", "illegal_parking", "illegal_parking_event"}:
                    # Parking may reuse only the bounded, timestamped evidence above.
                    evidence = {"plate": plate} if plate else {}
                if evidence.get("plate"):
                    row["plate"] = evidence["plate"]
                if evidence.get("plate_crop") and "plate_crop" not in snapshot_refs:
                    snapshot_refs["plate_crop"] = evidence["plate_crop"]
                row["vehicle"] = {
                    "ref": vehicle_ref,
                    "track_id": vehicle_track_id,
                    "camera_id": row["camera_id"],
                    **({"plate": evidence["plate"]} if evidence.get("plate") else {}),
                }
            if snapshot_refs:
                row["snapshot_refs"] = snapshot_refs
                row["snapshot_ref"] = (
                    snapshot_refs.get("vehicle_crop")
                    or snapshot_refs.get("plate_crop")
                    or snapshot_refs.get("event_frame")
                )
            self._write(row)

    def _observe_plates(self, packet) -> None:
        """Retain bounded evidence while waiting for a parking dwell alert."""
        now = time.monotonic()
        for key, value in list(self._parking_plates.items()):
            if now - value["last_seen"] > 300:
                del self._parking_plates[key]
        if packet.frame is None:
            return
        for detection in getattr(packet, "detections", []) or []:
            if detection.model_name != "license_plate" or detection.parent_id is None:
                continue
            ref = self._vehicle_ref(packet.name, detection.parent_id)
            previous = self._parking_plates.get(ref)
            plate = _plate_evidence(packet, detection.parent_id)
            if previous:
                previous["last_seen"] = now
                self._parking_plates.move_to_end(ref)
                if not plate or previous.get("plate") == plate:
                    continue
            crop = _crop(packet.frame, detection.bbox)
            if crop is None:
                continue
            ok, jpeg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok or jpeg.nbytes > 256 * 1024:
                continue
            self._parking_plates[ref] = dict(
                jpeg=jpeg, plate=plate, frame_id=packet.index, last_seen=now,
                observed_at=datetime.now(timezone.utc).isoformat(),
            )
            while len(self._parking_plates) > 128:
                self._parking_plates.popitem(last=False)

    def _save_snapshots(
        self,
        packet,
        event: dict[str, Any],
        index: int,
        vehicle_track_id: Any = None,
    ) -> dict[str, str]:
        refs: dict[str, str] = {}
        if packet.frame is None:
            return refs
        event_frame = self._save_image(packet.frame, packet, event, index, "frame")
        if event_frame:
            refs["event_frame"] = event_frame

        subject = event.get("subject") if isinstance(event.get("subject"), dict) else {}
        subject_bbox = _bbox_from_payload(subject.get("bbox"))
        event_type = str(event.get("event_type") or "")
        if event_type in {"plate_read", "plate_read_event"}:
            plate_crop = _crop(packet.frame, subject_bbox)
            if plate_crop is not None:
                ref = self._save_image(plate_crop, packet, event, index, "plate")
                if ref:
                    refs["plate_crop"] = ref
            vehicle_bbox = _vehicle_bbox_for_track(packet, vehicle_track_id)
            vehicle_crop = _crop(packet.frame, vehicle_bbox)
            if vehicle_crop is not None:
                ref = self._save_image(vehicle_crop, packet, event, index, "vehicle")
                if ref:
                    refs["vehicle_crop"] = ref
        elif _is_vehicle_event(event):
            vehicle_crop = _crop(packet.frame, subject_bbox)
            if vehicle_crop is not None:
                ref = self._save_image(vehicle_crop, packet, event, index, "vehicle")
                if ref:
                    refs["vehicle_crop"] = ref
            plate_detection = _plate_detection_for_track(packet, vehicle_track_id)
            plate_crop = _crop(packet.frame, getattr(plate_detection, "bbox", None))
            if plate_crop is not None:
                ref = self._save_image(plate_crop, packet, event, index, "plate")
                if ref:
                    refs["plate_crop"] = ref
        else:
            object_crop = _crop(packet.frame, subject_bbox)
            if object_crop is not None:
                ref = self._save_image(object_crop, packet, event, index, "object")
                if ref:
                    refs["object_crop"] = ref
        return refs

    def _vehicle_ref(self, camera_id: str, track_id: Any) -> str | None:
        if track_id is None:
            return None
        return f"{camera_id}:{self.session_id}:{track_id}"

    def _remember_vehicle_evidence(
        self,
        vehicle_ref: str,
        plate: dict[str, Any] | None,
        snapshot_refs: dict[str, str],
    ) -> dict[str, Any]:
        evidence = self._vehicle_evidence.setdefault(vehicle_ref, {})
        if plate:
            evidence["plate"] = plate
        if snapshot_refs.get("plate_crop"):
            evidence["plate_crop"] = snapshot_refs["plate_crop"]
        if len(self._vehicle_evidence) > 10000:
            self._vehicle_evidence.pop(next(iter(self._vehicle_evidence)))
        return evidence

    def _save_image(self, image, packet, event: dict[str, Any], index: int, kind: str) -> str | None:
        observed = str(
            event.get("observed_at")
            or event.get("timestamp_utc")
            or event.get("timestamp")
            or datetime.now(timezone.utc).isoformat()
        )
        safe_ts = "".join(ch if ch.isalnum() else "_" for ch in observed)[:40]
        safe_type = "".join(ch if ch.isalnum() else "_" for ch in str(event.get("event_type") or "event"))[:40]
        safe_event_id = "".join(ch if ch.isalnum() else "_" for ch in str(event["event_id"]))[:40]
        filename = f"{packet.name}_{packet.index}_{safe_type}_{kind}_{index}_{safe_ts}_{safe_event_id}.jpg"
        rel = Path("snapshots") / str(event.get("event_type") or "event") / filename
        path = self.state_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.stem + ".tmp" + path.suffix)
        ok = cv2.imwrite(str(temporary), image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            temporary.unlink(missing_ok=True)
            return None
        temporary.replace(path)
        return str(rel)

    def _write(self, row: dict[str, Any]) -> None:
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")


def _bbox_from_payload(payload: Any) -> list[int] | None:
    if not isinstance(payload, dict):
        return None
    try:
        x1 = int(payload["x1"])
        y1 = int(payload["y1"])
        x2 = int(payload["x2"])
        y2 = int(payload["y2"])
    except (KeyError, TypeError, ValueError):
        return None
    return [x1, y1, x2, y2]


def _crop(frame, bbox: list[int] | None):
    if bbox is None:
        return None
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(width - 1, int(x1)))
    y1 = max(0, min(height - 1, int(y1)))
    x2 = max(0, min(width, int(x2)))
    y2 = max(0, min(height, int(y2)))
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


def _vehicle_bbox_for_track(packet, track_id: Any) -> list[int] | None:
    if track_id is None:
        return None
    try:
        wanted = int(track_id)
    except (TypeError, ValueError):
        wanted = str(track_id)
    for detection in getattr(packet, "detections", []) or []:
        if getattr(detection, "model_name", None) != "vehicle":
            continue
        candidate = getattr(detection, "metadata", {}).get("track_id")
        if candidate == wanted or str(candidate) == str(wanted):
            bbox = getattr(detection, "bbox", None)
            return [int(value) for value in bbox[:4]] if bbox and len(bbox) >= 4 else None
    return None


def _vehicle_track_id(event: dict[str, Any]) -> Any:
    if not _is_vehicle_event(event):
        return None
    subject = event.get("subject") if isinstance(event.get("subject"), dict) else {}
    return subject.get("parent_track_id") or event.get("object_id") or subject.get("track_id")


def _is_vehicle_event(event: dict[str, Any]) -> bool:
    return str(event.get("event_type") or "") in {
        "plate_read",
        "plate_read_event",
        "wrong_way",
        "wrong_way_event",
        "illegal_parking",
        "illegal_parking_event",
        "parking_violation",
        "stopped_vehicle",
        "vehicle_count",
        "vehicle_count_event",
    }


def _plate_detection_for_track(packet, track_id: Any):
    if track_id is None:
        return None
    for detection in getattr(packet, "detections", []) or []:
        if getattr(detection, "model_name", None) != "license_plate":
            continue
        if str(getattr(detection, "parent_id", None)) == str(track_id):
            return detection
    return None


def _plate_evidence(packet, track_id: Any) -> dict[str, Any] | None:
    detection = _plate_detection_for_track(packet, track_id)
    if detection is None:
        return None
    text = str(getattr(detection, "metadata", {}).get("ocr_text") or "").strip()
    if not text:
        return None
    return {
        "text": text,
        "confidence": round(float(getattr(detection, "confidence", 0.0)), 4),
    }
