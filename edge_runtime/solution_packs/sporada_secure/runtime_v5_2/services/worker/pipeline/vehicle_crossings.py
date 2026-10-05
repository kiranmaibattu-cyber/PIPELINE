"""Directional vehicle crossing evaluation and durable management delivery."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import random
import ssl
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
import uuid

import cv2


logger = logging.getLogger(__name__)
SUPPORTED_VEHICLE_CLASSES = frozenset({"car", "motorcycle", "bus", "truck"})
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_SNAPSHOT_BYTES = 20_971_520
MAX_REQUEST_BYTES = 22_020_096


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class VehicleCrossingUploader(threading.Thread):
    """Retries immutable crossing metadata/evidence until management acknowledges it."""

    def __init__(self, outbox: Path, endpoint: str, token: str, metrics) -> None:
        super().__init__(name="vehicle-crossing-uploader", daemon=True)
        self.outbox = outbox
        self.endpoint = endpoint
        self.token = token
        self.metrics = metrics
        self.stop_event = threading.Event()
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def run(self) -> None:
        while not self.stop_event.is_set():
            delay = 0.25
            for record_path in sorted(self.outbox.glob("*.json")):
                if self.stop_event.is_set():
                    return
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    delivery = record.get("delivery") or {}
                    if delivery.get("blocked"):
                        continue
                    next_attempt = float(delivery.get("next_attempt_at", 0))
                    if next_attempt > time.time():
                        continue
                    self._submit(record_path, record)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("vehicle crossing upload deferred: %s", exc)
                    self.metrics.failed()
            self.stop_event.wait(delay)

    def _submit(self, record_path: Path, record: dict[str, Any]) -> None:
        event = record["event"]
        snapshot = record["snapshot"]
        snapshot_path = Path(snapshot["path"])
        try:
            image = snapshot_path.read_bytes()
        except OSError as exc:
            self._defer(record_path, record, exc)
            return
        digest = hashlib.sha256(image).hexdigest()
        if digest != snapshot["sha256"] or len(image) != snapshot["size_bytes"]:
            self._block(record_path, record, ValueError("vehicle crossing evidence changed after persistence"))
            return
        if len(image) > MAX_SNAPSHOT_BYTES:
            self._block(record_path, record, ValueError("vehicle crossing evidence exceeds maximumBytes"))
            return
        event_bytes = json.dumps(event, separators=(",", ":"), allow_nan=False).encode("utf-8")
        boundary = "apexfabric-" + hashlib.sha256(event["event_id"].encode("utf-8")).hexdigest()
        body = self._multipart(boundary, event_bytes, image)
        if len(body) > MAX_REQUEST_BYTES:
            self._block(record_path, record, ValueError("vehicle crossing request exceeds maximumRequestBytes"))
            return
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                status = response.status
                acknowledgement = json.load(response)
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_STATUSES:
                self._block(record_path, record, exc)
                return
            self._defer(record_path, record, exc)
            return
        except (OSError, ValueError) as exc:
            self._defer(record_path, record, exc)
            return
        expected_sha = "sha256:" + digest
        expected_id = "artifact-sha256-" + digest
        valid = (
            status in (200, 201)
            and acknowledgement.get("event_id") == event["event_id"]
            and acknowledgement.get("status") in {"created", "existing"}
            and acknowledgement.get("artifact_id") == expected_id
            and acknowledgement.get("artifact_sha256") == expected_sha
        )
        if not valid:
            self._defer(record_path, record, ValueError("invalid vehicle crossing acknowledgement"))
            return
        snapshot_path.unlink(missing_ok=True)
        record_path.unlink(missing_ok=True)
        self.metrics.acknowledged()
        self.metrics.outbox(self._outbox_count())

    @staticmethod
    def _multipart(boundary: str, event: bytes, image: bytes) -> bytes:
        marker = boundary.encode("ascii")
        return b"".join([
            b"--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="event"; filename="event.json"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            event,
            b"\r\n--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="snapshot"; filename="crossing.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            image,
            b"\r\n--" + marker + b"--\r\n",
        ])

    def _defer(self, path: Path, record: dict[str, Any], exc: Exception) -> None:
        delivery = record.setdefault("delivery", {})
        attempts = int(delivery.get("attempts", 0)) + 1
        delay = 0.25 * (2 ** min(attempts - 1, 8))
        delay = min(60.0, delay * random.uniform(0.5, 1.5))
        delivery.update({
            "attempts": attempts,
            "last_error": f"{type(exc).__name__}: {exc}"[:200],
            "last_attempt_at": time.time(),
            "next_attempt_at": time.time() + delay,
            "blocked": False,
        })
        _atomic_json(path, record)
        self.metrics.failed()

    def _block(self, path: Path, record: dict[str, Any], exc: Exception) -> None:
        delivery = record.setdefault("delivery", {})
        delivery.update({
            "attempts": int(delivery.get("attempts", 0)) + 1,
            "last_error": f"{type(exc).__name__}: {exc}"[:200],
            "last_attempt_at": time.time(),
            "next_attempt_at": 0,
            "blocked": True,
        })
        _atomic_json(path, record)
        self.metrics.failed()

    def _outbox_count(self) -> int:
        return sum(1 for _ in self.outbox.glob("*.json"))


class VehicleCrossingPipeline:
    """Evaluates ordered A-to-B lines without publishing duplicate SSE events."""

    def __init__(self, camera_id: str, camera_config: dict[str, Any]) -> None:
        self.camera_id = camera_id
        self.camera_config = camera_config
        state_root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/state"))
        self.snapshot_root = Path(os.getenv("SNAPSHOT_ROOT", "/state/snapshots"))
        self.outbox = state_root / "vehicle_crossings" / "outbox" / camera_id
        self.outbox.mkdir(parents=True, exist_ok=True)
        from .vehicle_crossing_metrics import VehicleCrossingMetrics
        self.metrics = VehicleCrossingMetrics(camera_id)
        self.metrics.outbox(sum(1 for _ in self.outbox.glob("*.json")))
        self.states: dict[tuple[str, int], dict[str, Any]] = {}
        self.deadband = max(0.0, float(os.getenv("VEHICLE_CROSSING_DEADBAND", "0.005")))
        self.uploader = None
        token = os.getenv("APEXFABRIC_FACE_IDENTITY_TOKEN", "").strip()
        if token:
            self.uploader = VehicleCrossingUploader(
                self.outbox,
                os.getenv(
                    "APEXFABRIC_VEHICLE_CROSSING_URL",
                    "http://apexfabric-ui.apexfabric.svc/internal/vehicle-crossings",
                ),
                token,
                self.metrics,
            )
            self.uploader.start()
        else:
            logger.warning("management token is absent; vehicle crossings will remain in %s", self.outbox)

    def process(self, packet) -> None:
        config = (self.camera_config.get("runtime_analytics") or {}).get(
            "vehicle_entry_exit_counts"
        ) or {}
        lines = config.get("lines") or []
        if not lines:
            return
        for detection in packet.detections:
            if (
                detection.model_name != "vehicle"
                or detection.class_name not in SUPPORTED_VEHICLE_CLASSES
                or detection.metadata.get("track_id") is None
            ):
                continue
            point = self._bottom_center(detection.bbox, packet.frame.shape)
            for line in lines:
                crossing = self._observe(line, detection, point)
                if crossing:
                    self._persist(packet, detection, line, point, crossing)

    def _observe(self, line, detection, point) -> str | None:
        points = line.get("points") or []
        if len(points) != 2:
            return None
        a = (float(points[0]["x"]), float(points[0]["y"]))
        b = (float(points[1]["x"]), float(points[1]["y"]))
        length = math.dist(a, b)
        if length <= 1e-12:
            return None
        signed_distance = self._side(point, a, b) / length
        if abs(signed_distance) <= self.deadband:
            return None
        side = "left" if signed_distance > 0 else "right"
        track_id = int(detection.metadata["track_id"])
        key = (str(line["id"]), track_id)
        signature = (a, b)
        state = self.states.get(key)
        if state is None or state["signature"] != signature:
            self.states[key] = {
                "signature": signature,
                "side": side,
                "point": point,
                "last_emitted": 0.0,
            }
            return None
        previous_side = state["side"]
        previous_point = state["point"]
        if previous_side == side:
            state["point"] = point
            return None
        state["side"] = side
        state["point"] = point
        minimum_age = int(line.get("minimum_track_age_frames", 3))
        if int(detection.metadata.get("track_hits") or 1) < minimum_age:
            return None
        if math.dist(previous_point, point) < float(line.get("minimum_crossing_displacement", 0.02)):
            return None
        if not self._segments_intersect(previous_point, point, a, b):
            return None
        now = time.time()
        if now - state["last_emitted"] < float(line.get("crossing_cooldown_seconds", 10)):
            return None
        state["last_emitted"] = now
        return "in" if previous_side == "right" and side == "left" else "out"

    def _persist(self, packet, detection, line, point, direction: str) -> None:
        observed_at = _utc_now()
        event_id = (
            f"{self.camera_id}:{line['id']}:{detection.metadata['track_id']}:"
            f"{observed_at}:{uuid.uuid4().hex[:12]}"
        )
        event = {
            "schema_version": "1.0",
            "event_id": event_id,
            "observed_at": observed_at,
            "camera_id": self.camera_id,
            "line_id": str(line["id"]),
            "line_name": str(line.get("name") or line["id"]),
            "direction": direction,
            "track_id": str(detection.metadata["track_id"]),
            "crossing_point": {"x": round(point[0], 6), "y": round(point[1], 6)},
            "vehicle": {
                "class": detection.class_name,
                "confidence": round(float(detection.confidence), 6),
                "bbox": dict(zip(("x1", "y1", "x2", "y2"), map(int, detection.bbox))),
            },
        }
        evidence = self._render(packet.frame, detection.bbox, line, direction)
        basename = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        evidence_folder = self.outbox / "evidence"
        evidence_folder.mkdir(parents=True, exist_ok=True)
        path = evidence_folder / f"{basename}.jpg"
        temporary = path.with_name(path.name + ".tmp.jpg")
        if not cv2.imwrite(str(temporary), evidence, [int(cv2.IMWRITE_JPEG_QUALITY), 88]):
            raise RuntimeError("could not write vehicle crossing evidence")
        os.replace(temporary, path)
        retained_folder = self.snapshot_root / "vehicle_crossings" / self.camera_id
        retained_folder.mkdir(parents=True, exist_ok=True)
        retained_path = retained_folder / path.name
        try:
            os.link(path, retained_path)
        except FileExistsError:
            pass
        except OSError:
            retained_temporary = retained_path.with_name(retained_path.name + ".tmp")
            retained_temporary.write_bytes(path.read_bytes())
            os.replace(retained_temporary, retained_path)
        image = path.read_bytes()
        _atomic_json(self.outbox / f"{basename}.json", {
            "schema_version": 1,
            "created_at": time.time(),
            "event": event,
            "snapshot": {
                "path": str(path),
                "retained_path": str(retained_path),
                "content_type": "image/jpeg",
                "sha256": hashlib.sha256(image).hexdigest(),
                "size_bytes": len(image),
            },
            "delivery": {"attempts": 0, "next_attempt_at": 0, "last_error": None},
        })
        self.metrics.outbox(sum(1 for _ in self.outbox.glob("*.json")))

    @staticmethod
    def _render(frame, bbox, line, direction):
        image = frame.copy()
        height, width = image.shape[:2]
        points = line["points"]
        a = (int(points[0]["x"] * width), int(points[0]["y"] * height))
        b = (int(points[1]["x"] * width), int(points[1]["y"] * height))
        x1, y1, x2, y2 = map(int, bbox)
        cv2.line(image, a, b, (0, 255, 0), 3)
        cv2.circle(image, a, 6, (255, 255, 255), -1)
        cv2.putText(image, "A", (a[0] + 5, a[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(image, "B", (b[0] + 5, b[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 255), 3)
        cv2.putText(image, direction.upper(), (x1, max(25, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 3)
        return image

    @staticmethod
    def _bottom_center(bbox, shape) -> tuple[float, float]:
        height, width = shape[:2]
        x1, _, x2, y2 = bbox
        return ((float(x1) + float(x2)) / (2.0 * width), float(y2) / height)

    @staticmethod
    def _side(point, a, b) -> float:
        return (b[1] - a[1]) * (point[0] - a[0]) - (b[0] - a[0]) * (point[1] - a[1])

    @staticmethod
    def _segments_intersect(a, b, c, d) -> bool:
        def orientation(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

        return orientation(a, b, c) * orientation(a, b, d) <= 0 and orientation(c, d, a) * orientation(c, d, b) <= 0
