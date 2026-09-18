"""Face sample persistence, analytics events, and management delivery."""
from __future__ import annotations

import hashlib
import json
import logging
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
import numpy as np

from .face_metrics import FaceMetrics

logger = logging.getLogger(__name__)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _RetryLater(RuntimeError):
    def __init__(self, message: str, delay: float) -> None:
        super().__init__(message)
        self.delay = delay


class _PermanentRejection(RuntimeError):
    pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _write_jpeg(path: Path, frame, quality: int = 88) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp.jpg")
    if not cv2.imwrite(str(temporary), frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
        raise RuntimeError(f"could not write face evidence: {path}")
    os.replace(temporary, path)
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


class FaceManagementUploader(threading.Thread):
    RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

    def __init__(self, outbox: Path, url: str, token: str, metrics: FaceMetrics) -> None:
        super().__init__(name="face-management-uploader", daemon=True)
        self.outbox = outbox
        self.stop_event = threading.Event()
        self.url = url
        self.token = token
        self.metrics = metrics
        context = ssl.create_default_context()
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))
        self.timeout = 10.0
        self.poll_seconds = 2.0

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._cycle()
            except _RetryLater as exc:
                logger.warning("face management upload deferred: %s", exc)
                self.stop_event.wait(exc.delay)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("face management upload failed: %s", exc)
            self.stop_event.wait(self.poll_seconds)

    def _cycle(self) -> None:
        for record_path in sorted(self.outbox.glob("*.json")):
            if self.stop_event.is_set():
                return
            record = json.loads(record_path.read_text(encoding="utf-8"))
            body = json.dumps(record["sample"], separators=(",", ":")).encode("utf-8")
            if len(body) > 1024 * 1024:
                logger.error("dropping oversized face sample %s", record["sample"].get("sample_id"))
                record_path.unlink()
                continue
            try:
                started = time.monotonic()
                response = self._submit(body)
            except _PermanentRejection as exc:
                self.metrics.submitted("rejected", time.monotonic() - started)
                logger.error("dropping permanently rejected face sample %s: %s", record["sample"].get("sample_id"), exc)
                record_path.unlink()
                continue
            except Exception:
                self.metrics.submitted("retrying", time.monotonic() - started)
                raise
            if response.get("sample_id") != record["sample"]["sample_id"]:
                raise ValueError("management returned an invalid face-sample acknowledgement")
            self.metrics.submitted("success", time.monotonic() - started)
            record_path.unlink()

    def _submit(self, body: bytes) -> dict[str, Any]:
        for attempt in range(5):
            try:
                return self._request(body)
            except HTTPError as exc:
                if exc.code == 401:
                    raise _RetryLater("identity service rejected credentials", 60.0) from exc
                if exc.code not in self.RETRY_STATUSES:
                    raise _PermanentRejection(
                        f"identity service permanently rejected sample with HTTP {exc.code}"
                    ) from exc
                if attempt == 4:
                    raise _RetryLater(f"identity service unavailable after five attempts (HTTP {exc.code})", 10.0) from exc
            except OSError as exc:
                if attempt == 4:
                    raise _RetryLater("identity service transport unavailable after five attempts", 10.0) from exc
            delay = min(10.0, 0.25 * (2 ** attempt))
            self.stop_event.wait(delay * random.uniform(0.75, 1.25))
        raise AssertionError("unreachable")

    def _request(self, body: bytes) -> dict[str, Any]:
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.load(response)


class FaceSamplePipeline:
    def __init__(self, camera_id: str, edge_id: str, extractor, camera_config: dict) -> None:
        self.camera_id = camera_id
        self.edge_id = edge_id
        self.extractor = extractor
        self.camera_config = camera_config
        self.state_root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/state"))
        self.snapshot_root = Path(os.getenv("SNAPSHOT_ROOT", "/state/snapshots"))
        self.outbox = self.state_root / "face_samples" / "outbox" / camera_id
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.metrics = FaceMetrics(camera_id)
        self.stream_session_id = uuid.uuid4().hex
        face_config = (self.camera_config.get("analytics") or {}).get("face_recognition") or {}
        emission = face_config.get("emission") or {}
        embedding = face_config.get("embedding") or {}
        self.cooldown = float(emission.get("cooldown_seconds", os.getenv("FACE_SAMPLE_COOLDOWN_SECONDS", "5")))
        self.min_quality = float(emission.get("minimum_quality", os.getenv("FACE_MIN_QUALITY", "0.20")))
        self.material_change_threshold = float(emission.get("material_change_threshold", "0.15"))
        expected_model = embedding.get("model_id")
        expected_dimensions = embedding.get("dimensions")
        if expected_model and expected_model != self.extractor.model_id:
            raise ValueError(
                f"configured face model {expected_model} does not match runtime model {self.extractor.model_id}"
            )
        if expected_dimensions and int(expected_dimensions) != int(self.extractor.dimension):
            raise ValueError("configured face embedding dimensions do not match the runtime model")
        self.interval = max(1, int(os.getenv("FACE_PROCESS_INTERVAL", "3")))
        self.last_emitted: dict[int, float] = {}
        self.last_embeddings: dict[int, np.ndarray] = {}
        self.uploader = None
        self.max_pending = max(1, int(os.getenv("FACE_SAMPLE_OUTBOX_MAX_RECORDS", "1000")))
        token = os.getenv("APEXFABRIC_FACE_IDENTITY_TOKEN", "").strip()
        if token:
            self.uploader = FaceManagementUploader(
                self.outbox,
                os.getenv(
                    "APEXFABRIC_FACE_IDENTITY_URL",
                    "http://apexfabric-ui.apexfabric.svc/internal/face-samples",
                ),
                token,
                self.metrics,
            )
            self.uploader.start()
        else:
            self.metrics.submitted("configuration_error", 0.0)
            logger.warning("face identity token is absent; samples will remain in bounded outbox %s", self.outbox)

    def process(self, packet, people: list) -> None:
        if packet.index % self.interval:
            return
        now = time.time()
        eligible = [person for person in people if self._eligible(person, packet.frame.shape, now)]
        faces = self.extractor.extract(packet.frame, eligible)
        self.metrics.tracks(len(faces))
        for face in faces:
            self._refresh_policy()
            if face.quality < self.min_quality:
                self.metrics.suppressed("quality")
                continue
            embedding = np.asarray(face.embedding, dtype=np.float32).reshape(-1)
            if (
                embedding.size != int(self.extractor.dimension)
                or not np.isfinite(embedding).all()
                or float(np.linalg.norm(embedding)) <= 1e-12
            ):
                self.metrics.suppressed("invalid_embedding")
                continue
            if not self._should_emit(face.track_id, embedding, now):
                self.metrics.suppressed("duplicate")
                continue
            if len(list(self.outbox.glob("*.json"))) >= self.max_pending:
                self.metrics.suppressed("outbox_full")
                logger.warning("face sample outbox is full; suppressing camera=%s track=%s", self.camera_id, face.track_id)
                continue
            sample_id = "face-" + uuid.uuid4().hex
            observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            event_id = f"{self.camera_id}:face_recognition:face_seen:{sample_id}"
            try:
                assets = self._save_assets(packet, face, sample_id)
            except Exception:  # noqa: BLE001
                self.metrics.snapshot_failure("event_frame_or_face_crop")
                logger.exception("could not persist face evidence for camera=%s", self.camera_id)
                continue
            sample = {
                "sample_id": sample_id,
                "event_id": event_id,
                "camera_id": self.camera_id,
                "observed_at": observed_at,
                "model_id": self.extractor.model_id,
                "dimensions": self.extractor.dimension,
                "embedding": [float(value) for value in embedding],
                "quality": round(float(face.quality), 6),
                "face_crop_url": self._event_asset(assets["face_crop"])["url"],
            }
            _atomic_json(self.outbox / f"{sample_id}.json", {
                "sample": sample,
                "artifacts": assets,
            })
            packet.add_event({
                "observation_id": event_id,
                "observed_at": observed_at,
                "use_case": "face_recognition",
                "type": "face_seen",
                "sample_id": sample_id,
                "person_id": None,
                "track_id": str(face.track_id),
                "match_confidence": None,
                "model_id": self.extractor.model_id,
                "face_quality": round(float(face.quality), 6),
                "subject": {
                    "type": "face",
                    "track_id": face.track_id,
                    "confidence": round(float(face.detector_confidence), 6),
                    "bbox": self._normalized_bbox(face.bbox, packet.frame.shape),
                },
                "snapshot": self._event_asset(assets["event_frame"]),
                "snapshots": {"face_crop": self._event_asset(assets["face_crop"])},
            })
            self.last_emitted[face.track_id] = now
            self.last_embeddings[face.track_id] = embedding.copy()
            self.metrics.emitted()

    def _eligible(self, person, frame_shape, now: float) -> bool:
        if person.model_name != "vehicle" or person.class_name != "pedestrian":
            return False
        track_id = person.metadata.get("track_id")
        if track_id is None:
            return False
        config = (self.camera_config.get("runtime_analytics") or {}).get("face_recognition") or {}
        zones = config.get("zones") or []
        if not zones:
            return True
        height, width = frame_shape[:2]
        x1, y1, x2, y2 = person.bbox
        point = ((x1 + x2) / 2.0 / width, (y1 + y2) / 2.0 / height)
        return any(self._inside(point, zone.get("points") or []) for zone in zones)

    def _refresh_policy(self) -> None:
        face_config = (self.camera_config.get("analytics") or {}).get("face_recognition") or {}
        emission = face_config.get("emission") or {}
        self.cooldown = float(emission.get("cooldown_seconds", self.cooldown))
        self.min_quality = float(emission.get("minimum_quality", self.min_quality))
        self.material_change_threshold = float(
            emission.get("material_change_threshold", self.material_change_threshold)
        )

    def _should_emit(self, track_id: int, embedding, now: float) -> bool:
        previous = self.last_embeddings.get(track_id)
        if previous is None or now - self.last_emitted.get(track_id, 0.0) >= self.cooldown:
            return True
        current = np.asarray(embedding, dtype=np.float32)
        denominator = float(np.linalg.norm(previous) * np.linalg.norm(current))
        if denominator <= 0:
            return False
        cosine_distance = 1.0 - float(np.dot(previous, current) / denominator)
        return cosine_distance >= self.material_change_threshold

    @staticmethod
    def _normalized_bbox(bbox, frame_shape) -> dict[str, float]:
        height, width = frame_shape[:2]
        x1, y1, x2, y2 = bbox
        return {
            "x1": max(0.0, min(1.0, float(x1) / width)),
            "y1": max(0.0, min(1.0, float(y1) / height)),
            "x2": max(0.0, min(1.0, float(x2) / width)),
            "y2": max(0.0, min(1.0, float(y2) / height)),
        }

    @staticmethod
    def _inside(point, points) -> bool:
        polygon = [(float(item.get("x", 0)), float(item.get("y", 0))) for item in points]
        if len(polygon) < 3:
            return False
        x, y = point
        inside = False
        previous = polygon[-1]
        for current in polygon:
            xi, yi = current
            xj, yj = previous
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
                inside = not inside
            previous = current
        return inside

    def _save_assets(self, packet, face, sample_id: str) -> dict[str, dict[str, Any]]:
        camera_dir = self.snapshot_root / self.camera_id / "faces"
        frame_path = camera_dir / f"{sample_id}-frame.jpg"
        crop_path = camera_dir / f"{sample_id}-face.jpg"
        annotated = packet.frame.copy()
        cv2.rectangle(annotated, (face.bbox[0], face.bbox[1]),
                      (face.bbox[2], face.bbox[3]), (0, 255, 255), 2)
        frame_digest, frame_size = _write_jpeg(frame_path, annotated)
        crop_digest, crop_size = _write_jpeg(crop_path, face.chip)
        return {
            "event_frame": self._artifact(frame_path, frame_digest, frame_size),
            "face_crop": self._artifact(crop_path, crop_digest, crop_size),
        }

    def _artifact(self, path: Path, digest: str, size: int) -> dict[str, Any]:
        return {
            "artifact_id": digest,
            "path": str(path),
            "ref": str(path.resolve().relative_to(self.state_root.resolve())).replace(os.sep, "/"),
            "size": size,
        }

    @staticmethod
    def _event_asset(asset: dict[str, Any]) -> dict[str, Any]:
        return {
            "ref": asset["ref"],
            "url": "/snapshots/" + asset["ref"].removeprefix("snapshots/"),
            "content_type": "image/jpeg",
        }
