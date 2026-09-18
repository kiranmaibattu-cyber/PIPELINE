"""Face sample persistence, analytics events, and management delivery."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import ssl
import threading
import time
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
import uuid

import cv2

logger = logging.getLogger(__name__)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
    def __init__(self, edge_id: str, outbox: Path, config_path: str) -> None:
        super().__init__(name="face-management-uploader", daemon=True)
        self.edge_id = edge_id
        self.outbox = outbox
        self.stop_event = threading.Event()
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.base_url = str(config["url"]).rstrip("/")
        parsed = urlsplit(self.base_url)
        insecure_loopback = bool(config.get("allow_insecure_loopback")) and parsed.hostname in {
            "127.0.0.1", "localhost", "::1",
        }
        if parsed.scheme != "https" and not insecure_loopback:
            raise ValueError("face management URL must use HTTPS")
        self.token = Path(config["token_file"]).read_text(encoding="utf-8").strip()
        context = ssl.create_default_context(cafile=config.get("ca_file"))
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))
        self.timeout = float(config.get("timeout_seconds", 20))
        self.poll_seconds = float(config.get("poll_seconds", 2))

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._cycle()
            except Exception as exc:  # noqa: BLE001
                logger.warning("face management upload failed: %s", exc)
            self.stop_event.wait(self.poll_seconds)

    def _cycle(self) -> None:
        for record_path in sorted(self.outbox.glob("*.json")):
            if self.stop_event.is_set():
                return
            record = json.loads(record_path.read_text(encoding="utf-8"))
            for artifact in record["artifacts"].values():
                data = Path(artifact["path"]).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if digest != artifact["artifact_id"]:
                    raise ValueError(f"artifact checksum changed: {artifact['path']}")
                response = self._request(
                    "PUT",
                    f"/v1/edges/{quote(self.edge_id, safe='')}/artifacts/{digest}",
                    data,
                    "image/jpeg",
                )
                if response.get("sha256") != digest:
                    raise ValueError("management returned an invalid artifact acknowledgement")
            response = self._request(
                "POST",
                f"/v1/edges/{quote(self.edge_id, safe='')}/face-samples",
                json.dumps(record["sample"], separators=(",", ":")).encode("utf-8"),
                "application/json",
            )
            if response.get("sample_id") != record["sample"]["sample_id"]:
                raise ValueError("management returned an invalid face-sample acknowledgement")
            record_path.unlink()

    def _request(self, method: str, path: str, body: bytes, content_type: str) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": content_type,
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
        self.stream_session_id = uuid.uuid4().hex
        self.cooldown = float(os.getenv("FACE_SAMPLE_COOLDOWN_SECONDS", "5"))
        self.min_quality = float(os.getenv("FACE_MIN_QUALITY", "0.20"))
        self.interval = max(1, int(os.getenv("FACE_PROCESS_INTERVAL", "3")))
        self.last_emitted: dict[int, float] = {}
        self.uploader = None
        config_path = os.getenv("FACE_MANAGEMENT_CONFIG", "/configs/face-management.json")
        if Path(config_path).is_file():
            self.uploader = FaceManagementUploader(edge_id, self.outbox, config_path)
            self.uploader.start()
        else:
            logger.warning("face management config is absent; samples will remain in %s", self.outbox)

    def process(self, packet, people: list) -> None:
        if packet.index % self.interval:
            return
        now = time.time()
        eligible = [person for person in people if self._eligible(person, packet.frame.shape, now)]
        for face in self.extractor.extract(packet.frame, eligible):
            if face.quality < self.min_quality:
                continue
            sample_id = "face-" + uuid.uuid4().hex
            observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            event_id = f"{self.camera_id}:face_recognition:face_seen:{sample_id}"
            assets = self._save_assets(packet, face, sample_id)
            sample = {
                "schema_version": "1.0",
                "sample_id": sample_id,
                "event_id": event_id,
                "edge_id": self.edge_id,
                "camera_id": self.camera_id,
                "stream_session_id": self.stream_session_id,
                "track_id": str(face.track_id),
                "frame_id": packet.index,
                "observed_at": observed_at,
                "model_id": self.extractor.model_id,
                "embedding_space": self.extractor.embedding_space,
                "dimensions": self.extractor.dimension,
                "embedding": [float(value) for value in face.embedding],
                "quality": round(float(face.quality), 6),
                "detector_confidence": round(float(face.detector_confidence), 6),
                "face_bbox": {
                    "x1": face.bbox[0], "y1": face.bbox[1],
                    "x2": face.bbox[2], "y2": face.bbox[3],
                },
                "artifacts": {
                    name: {
                        "artifact_id": value["artifact_id"],
                        "content_type": "image/jpeg",
                        "ref": value["ref"],
                    }
                    for name, value in assets.items()
                },
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
                "model_id": self.extractor.model_id,
                "embedding_space": self.extractor.embedding_space,
                "face_quality": round(float(face.quality), 6),
                "subject": {
                    "track_id": face.track_id,
                    "parent_track_id": face.track_id,
                    "class": "face",
                    "confidence": round(float(face.detector_confidence), 6),
                    "bbox": list(face.bbox),
                },
                "snapshot": self._event_asset(assets["event_frame"]),
                "snapshots": {"face_crop": self._event_asset(assets["face_crop"])},
            })
            self.last_emitted[face.track_id] = now

    def _eligible(self, person, frame_shape, now: float) -> bool:
        if person.model_name != "vehicle" or person.class_name != "pedestrian":
            return False
        track_id = person.metadata.get("track_id")
        if track_id is None or now - self.last_emitted.get(int(track_id), 0.0) < self.cooldown:
            return False
        config = (self.camera_config.get("runtime_analytics") or {}).get("face_recognition") or {}
        zones = config.get("zones") or []
        if not zones:
            return True
        height, width = frame_shape[:2]
        x1, y1, x2, y2 = person.bbox
        point = ((x1 + x2) / 2.0 / width, (y1 + y2) / 2.0 / height)
        return any(self._inside(point, zone.get("points") or []) for zone in zones)

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
