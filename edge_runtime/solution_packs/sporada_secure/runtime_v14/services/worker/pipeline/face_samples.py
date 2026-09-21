"""Face sample persistence, analytics events, and management delivery."""
from __future__ import annotations

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
from urllib.parse import quote
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
    def __init__(self, stage: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.status = status


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
    RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
    PERMANENT_STATUSES = {400, 404, 409, 413, 415, 422}

    def __init__(self, outbox: Path, artifact_url: str, sample_url: str,
                 token: str, metrics: FaceMetrics) -> None:
        super().__init__(name="face-management-uploader", daemon=True)
        self.outbox = outbox
        self.stop_event = threading.Event()
        self.artifact_url = artifact_url
        self.sample_url = sample_url
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
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if (record.get("delivery") or {}).get("permanent_error"):
                    continue
                self._validate_record(record)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                self._mark_permanent(record_path, {}, _PermanentRejection(
                    "record", f"invalid v14 outbox record: {type(exc).__name__}"
                ))
                continue
            try:
                if not record["delivery"].get("artifact_ack"):
                    self._upload_artifact(record_path, record)
                if not record_path.exists():
                    continue
                if not record["delivery"].get("embedding_ack"):
                    self._submit_embedding(record_path, record)
            except _PermanentRejection as exc:
                self._mark_permanent(record_path, record, exc)
                continue
            self._complete(record_path, record)

    def _upload_artifact(self, record_path: Path, record: dict[str, Any]) -> None:
        sample_id = record["sample"]["sample_id"]
        artifact = record["artifact"]
        path = Path(artifact["path"])
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise _PermanentRejection("artifact", "face crop is missing") from exc
        if not 0 < len(body) <= 20 * 1024 * 1024:
            raise _PermanentRejection("artifact", "face crop size violates contract")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if digest != artifact["sha256"] or len(body) != artifact["size_bytes"]:
            raise _PermanentRejection("artifact", "face crop checksum or size changed")
        attempt = self._record_attempt(record_path, record, "artifact")
        url = self.artifact_url.replace("<sample-id>", quote(sample_id, safe=""))
        started = time.monotonic()
        try:
            status, response = self._request(
                url, body, artifact["content_type"],
                {"X-ApexFabric-Artifact-SHA256": digest}, 15.0,
            )
        except HTTPError as exc:
            self._raise_http_failure("artifact", exc, attempt, 60.0)
        except OSError as exc:
            self.metrics.submitted("artifact_retrying", time.monotonic() - started)
            raise _RetryLater("face artifact transport unavailable", self._delay(attempt, 60.0)) from exc
        if status not in (200, 201):
            raise _PermanentRejection("artifact", f"unexpected artifact HTTP {status}", status)
        self._validate_artifact_ack(record, response)
        record["delivery"]["artifact_ack"] = response
        _atomic_json(record_path, record)
        self.metrics.submitted("artifact_success", time.monotonic() - started)

    def _submit_embedding(self, record_path: Path, record: dict[str, Any]) -> None:
        ack = record["delivery"]["artifact_ack"]
        sample = dict(record["sample"])
        sample["artifact_id"] = ack["artifact_id"]
        sample["artifact_sha256"] = ack["sha256"]
        body = json.dumps(sample, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > 1024 * 1024:
            raise _PermanentRejection("embedding", "face sample exceeds one MiB")
        attempt = self._record_attempt(record_path, record, "embedding")
        started = time.monotonic()
        try:
            status, response = self._request(
                self.sample_url, body, "application/json", {}, 10.0,
            )
        except HTTPError as exc:
            self._raise_http_failure("embedding", exc, attempt, 10.0)
        except OSError as exc:
            self.metrics.submitted("embedding_retrying", time.monotonic() - started)
            raise _RetryLater("face embedding transport unavailable", self._delay(attempt, 10.0)) from exc
        if status not in (200, 201) or response.get("sample_id") != sample["sample_id"]:
            raise _PermanentRejection("embedding", "invalid face-sample acknowledgement", status)
        record["delivery"]["embedding_ack"] = {
            "sample_id": response["sample_id"], "status": status,
        }
        _atomic_json(record_path, record)
        self.metrics.submitted("embedding_success", time.monotonic() - started)

    def _request(self, url: str, body: bytes, content_type: str,
                 headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any]]:
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": content_type,
                "Accept": "application/json",
                **headers,
            },
        )
        with self.opener.open(request, timeout=timeout) as response:
            return response.status, json.load(response)

    def _record_attempt(self, path: Path, record: dict[str, Any], stage: str) -> int:
        field = stage + "_attempts"
        attempt = int(record["delivery"].get(field, 0)) + 1
        record["delivery"][field] = attempt
        record["delivery"]["last_attempt_at"] = time.time()
        _atomic_json(path, record)
        return attempt

    def _raise_http_failure(self, stage: str, exc: HTTPError,
                            attempt: int, maximum_delay: float) -> None:
        if exc.code in (401, 403):
            self.metrics.submitted(stage + "_auth_error", 0.0)
            raise _RetryLater(f"{stage} credentials rejected", 60.0) from exc
        if exc.code in self.RETRY_STATUSES:
            self.metrics.submitted(stage + "_retrying", 0.0)
            raise _RetryLater(
                f"{stage} service returned retryable HTTP {exc.code}",
                self._delay(attempt, maximum_delay),
            ) from exc
        raise _PermanentRejection(stage, f"management rejected {stage} with HTTP {exc.code}", exc.code) from exc

    @staticmethod
    def _delay(attempt: int, maximum: float) -> float:
        return min(maximum, 0.25 * (2 ** min(max(0, attempt - 1), 16))) * random.uniform(0.75, 1.25)

    @staticmethod
    def _validate_record(record: dict[str, Any]) -> None:
        if record.get("schema_version") != 2:
            raise ValueError("unsupported outbox schema")
        sample = record["sample"]
        required = {"sample_id", "event_id", "camera_id", "track_id", "observed_at",
                    "model_id", "dimensions", "embedding", "quality"}
        if not required <= set(sample):
            raise ValueError("outbox sample is incomplete")
        if sample["model_id"] != "face-embedding-model-v1" or sample["dimensions"] != 512:
            raise ValueError("outbox embedding space is incompatible")
        embedding = sample["embedding"]
        if (
            not isinstance(embedding, list)
            or len(embedding) != 512
            or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in embedding)
        ):
            raise ValueError("outbox embedding is invalid")
        quality = sample["quality"]
        if not isinstance(quality, (int, float)) or not math.isfinite(quality) or not 0 <= quality <= 1:
            raise ValueError("outbox quality is invalid")
        if not isinstance(record.get("artifact"), dict) or not isinstance(record.get("delivery"), dict):
            raise ValueError("outbox delivery state is incomplete")
        artifact = record["artifact"]
        if artifact.get("content_type") not in {"image/jpeg", "image/png"}:
            raise ValueError("outbox artifact content type is invalid")
        digest = artifact.get("sha256")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or len(digest) != 71
            or any(character not in "0123456789abcdef" for character in digest[7:])
        ):
            raise ValueError("outbox artifact checksum is invalid")

    @staticmethod
    def _validate_artifact_ack(record: dict[str, Any], response: dict[str, Any]) -> None:
        artifact = record["artifact"]
        sample_id = record["sample"]["sample_id"]
        expected_id = "artifact-sha256-" + artifact["sha256"].removeprefix("sha256:")
        expected = {
            "artifact_id": expected_id,
            "sample_id": sample_id,
            "sha256": artifact["sha256"],
            "content_type": artifact["content_type"],
            "size_bytes": artifact["size_bytes"],
        }
        if any(response.get(key) != value for key, value in expected.items()):
            raise _PermanentRejection("artifact", "artifact acknowledgement does not match request")
        if response.get("status") not in {"created", "existing"}:
            raise _PermanentRejection("artifact", "artifact acknowledgement has invalid status")

    def _mark_permanent(self, path: Path, record: dict[str, Any], exc: _PermanentRejection) -> None:
        if not record:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                record = {"schema_version": 0, "delivery": {}}
        artifact = record.get("artifact") or {}
        artifact_path = Path(artifact.get("path", ""))
        if artifact_path.is_file():
            artifact_path.unlink(missing_ok=True)
        sample = record.get("sample") or {}
        sample.pop("embedding", None)
        artifact.pop("path", None)
        delivery = record.setdefault("delivery", {})
        delivery.pop("artifact_ack", None)
        delivery.pop("embedding_ack", None)
        delivery["permanent_error"] = {
            "stage": exc.stage, "status": exc.status,
            "reason": str(exc)[:200], "recorded_at": time.time(),
        }
        _atomic_json(path, record)
        self.metrics.submitted(exc.stage + "_rejected", 0.0)
        logger.error("face delivery permanently rejected stage=%s status=%s", exc.stage, exc.status)

    @staticmethod
    def _complete(record_path: Path, record: dict[str, Any]) -> None:
        if not record["delivery"].get("artifact_ack") or not record["delivery"].get("embedding_ack"):
            return
        # The management copy is the durable biometric artifact, while the local
        # crop remains telemetry evidence for the face_seen event URL. Snapshot
        # retention bounds its age and disk usage independently of this outbox.
        record_path.unlink(missing_ok=True)


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
        self.max_pending_bytes = max(1024 * 1024, int(os.getenv(
            "FACE_SAMPLE_OUTBOX_MAX_BYTES", str(1024 * 1024 * 1024)
        )))
        self.max_pending_age = max(60.0, float(os.getenv(
            "FACE_SAMPLE_OUTBOX_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60)
        )))
        self._prune_outbox()
        token = os.getenv("APEXFABRIC_FACE_IDENTITY_TOKEN", "").strip()
        if token:
            self.uploader = FaceManagementUploader(
                self.outbox,
                os.getenv(
                    "APEXFABRIC_FACE_ARTIFACT_URL_TEMPLATE",
                    "http://apexfabric-ui.apexfabric.svc/internal/face-artifacts/<sample-id>",
                ),
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
            self._prune_outbox(reserve_records=1)
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
                "track_id": str(face.track_id),
                "observed_at": observed_at,
                "model_id": self.extractor.model_id,
                "dimensions": self.extractor.dimension,
                "embedding": [float(value) for value in embedding],
                "quality": round(float(face.quality), 6),
            }
            _atomic_json(self.outbox / f"{sample_id}.json", {
                "schema_version": 2,
                "created_at": now,
                "sample": sample,
                "artifact": {
                    "path": assets["face_crop"]["path"],
                    "ref": assets["face_crop"]["ref"],
                    "content_type": "image/jpeg",
                    "size_bytes": assets["face_crop"]["size"],
                    "sha256": "sha256:" + assets["face_crop"]["artifact_id"],
                },
                "delivery": {
                    "artifact_ack": None,
                    "embedding_ack": None,
                    "artifact_attempts": 0,
                    "embedding_attempts": 0,
                },
            })
            self._prune_outbox()
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

    def _prune_outbox(self, reserve_records: int = 0) -> None:
        now = time.time()
        records = []
        for path in self.outbox.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                created = float(record.get("created_at", path.stat().st_mtime))
                artifact_path = Path((record.get("artifact") or {}).get("path", ""))
                size = path.stat().st_size
                if artifact_path.is_file():
                    size += artifact_path.stat().st_size
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                created, size = 0.0, path.stat().st_size if path.exists() else 0
            records.append((created, path, size))
        records.sort(key=lambda item: (item[0], item[1].name))
        total = sum(item[2] for item in records)
        for created, path, size in list(records):
            if created and now - created <= self.max_pending_age:
                continue
            self._drop_outbox_record(path, "max_age")
            records.remove((created, path, size))
            total -= size
        while records and (
            len(records) + reserve_records > self.max_pending
            or total > self.max_pending_bytes
        ):
            _, path, size = records.pop(0)
            self._drop_outbox_record(path, "capacity")
            total -= size
        self.metrics.outbox(len(records), max(0, total))

    def _drop_outbox_record(self, path: Path, reason: str) -> None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            artifact_path = Path((record.get("artifact") or {}).get("path", ""))
            if artifact_path.is_file() and self.state_root.resolve() in artifact_path.resolve().parents:
                artifact_path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        path.unlink(missing_ok=True)
        self.metrics.outbox_drop(reason)

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
        evidence_crop = self._evidence_crop(packet.frame, face.bbox)
        frame_digest, frame_size = _write_jpeg(frame_path, annotated)
        crop_digest, crop_size = _write_jpeg(crop_path, evidence_crop)
        return {
            "event_frame": self._artifact(frame_path, frame_digest, frame_size),
            "face_crop": self._artifact(crop_path, crop_digest, crop_size),
        }

    @staticmethod
    def _evidence_crop(frame: np.ndarray, bbox) -> np.ndarray:
        """Return a square UI crop with context; embedding still uses face.chip."""
        frame_height, frame_width = frame.shape[:2]
        x1, y1, x2, y2 = [int(round(value)) for value in bbox]
        x1, x2 = sorted((max(0, min(frame_width, x1)), max(0, min(frame_width, x2))))
        y1, y2 = sorted((max(0, min(frame_height, y1)), max(0, min(frame_height, y2))))
        face_width = x2 - x1
        face_height = y2 - y1
        if face_width <= 0 or face_height <= 0:
            raise ValueError("face evidence bounding box is empty")

        # Make the detected face occupy at most half the crop. Shift the square
        # inside the frame near boundaries instead of inventing padded pixels.
        side = min(frame_width, frame_height, max(2, 2 * max(face_width, face_height)))
        center_x = (x1 + x2) / 2.0
        center_y = (y1 + y2) / 2.0
        left = max(0, min(frame_width - side, int(round(center_x - side / 2.0))))
        top = max(0, min(frame_height - side, int(round(center_y - side / 2.0))))
        crop = frame[top:top + side, left:left + side]
        if crop.size == 0:
            raise ValueError("face evidence crop is empty")
        return crop.copy()

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
