from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import threading
import time

import numpy as np
import cv2
import pytest

WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from detectors.backends.openvino_face import FaceSample  # noqa: E402
from pipeline.face_samples import (  # noqa: E402
    FaceManagementUploader,
    FaceSamplePipeline,
    _PermanentRejection,
    _RetryLater,
)
from pipeline.types import FramePacket  # noqa: E402


class FakeExtractor:
    dimension = 512
    model_id = "face-embedding-model-v1"
    embedding_space = "face-embedding-model-v1:512:bgr-aligned-112"

    def extract(self, frame, people):
        assert len(people) == 1
        return [FaceSample(
            bbox=(20, 20, 80, 90),
            embedding=np.asarray([1.0] + [0.0] * 511, dtype=np.float32),
            detector_confidence=0.95,
            quality=0.9,
            track_id=7,
            chip=np.zeros((112, 112, 3), dtype=np.uint8),
        )]


def test_face_sample_vector_is_durable_but_not_in_analytics_event(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(state))
    monkeypatch.setenv("SNAPSHOT_ROOT", str(state / "snapshots"))
    monkeypatch.setenv("FACE_PROCESS_INTERVAL", "1")
    monkeypatch.setenv("FACE_MIN_QUALITY", "0.3")
    monkeypatch.setenv("FACE_MANAGEMENT_CONFIG", str(tmp_path / "absent.json"))
    camera_config = {"analytics": {"face_recognition": {
        "zones": [],
        "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
        "emission": {"minimum_quality": 0.3, "cooldown_seconds": 5,
                     "material_change_threshold": 0.15},
    }}, "runtime_analytics": {"face_recognition": {"zones": []}}}
    pipeline = FaceSamplePipeline("cam1", "edge1", FakeExtractor(), camera_config)
    packet = FramePacket(index=1, name="cam1", frame=np.zeros((120, 160, 3), dtype=np.uint8))
    person = SimpleNamespace(model_name="vehicle", class_name="pedestrian",
                             bbox=[10, 10, 100, 115], metadata={"track_id": 7})

    pipeline.process(packet, [person])

    records = list((state / "face_samples" / "outbox" / "cam1").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["schema_version"] == 2
    assert len(record["sample"]["embedding"]) == 512
    assert record["sample"]["track_id"] == "7"
    assert "face_crop_url" not in record["sample"]
    assert record["artifact"]["sha256"].startswith("sha256:")
    assert record["delivery"]["artifact_ack"] is None
    assert record["delivery"]["embedding_ack"] is None
    assert len(packet.analytics_events) == 1
    assert packet.analytics_events[0]["sample_id"] == record["sample"]["sample_id"]
    assert "embedding" not in packet.analytics_events[0]
    crop_path = next((state / "snapshots/cam1/faces").glob("*-face.jpg"))
    crop = cv2.imread(str(crop_path))
    assert crop is not None
    assert crop.shape[:2] == (120, 120)


def test_face_evidence_crop_keeps_context_and_shifts_at_frame_boundary():
    frame = np.zeros((200, 300, 3), dtype=np.uint8)

    interior = FaceSamplePipeline._evidence_crop(frame, (100, 70, 140, 120))
    boundary = FaceSamplePipeline._evidence_crop(frame, (0, 40, 40, 90))

    assert interior.shape[:2] == (100, 100)
    assert boundary.shape[:2] == (100, 100)


def test_zero_embedding_is_suppressed(monkeypatch, tmp_path):
    class ZeroExtractor(FakeExtractor):
        def extract(self, frame, people):
            sample = super().extract(frame, people)[0]
            sample.embedding[:] = 0
            return [sample]

    state = tmp_path / "state"
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(state))
    monkeypatch.setenv("SNAPSHOT_ROOT", str(state / "snapshots"))
    monkeypatch.setenv("FACE_PROCESS_INTERVAL", "1")
    monkeypatch.delenv("APEXFABRIC_FACE_IDENTITY_TOKEN", raising=False)
    config = {"analytics": {"face_recognition": {
        "zones": [], "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
        "emission": {"minimum_quality": 0.3, "cooldown_seconds": 5,
                     "material_change_threshold": 0.15},
    }}, "runtime_analytics": {"face_recognition": {"zones": []}}}
    pipeline = FaceSamplePipeline("cam1", "edge1", ZeroExtractor(), config)
    packet = FramePacket(index=1, name="cam1", frame=np.zeros((120, 160, 3), dtype=np.uint8))
    person = SimpleNamespace(model_name="vehicle", class_name="pedestrian",
                             bbox=[10, 10, 100, 115], metadata={"track_id": 7})

    pipeline.process(packet, [person])

    assert not packet.analytics_events
    assert not list((state / "face_samples/outbox/cam1").glob("*.json"))


def test_management_submits_contract_sample_and_acknowledges(monkeypatch, tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer test-token"
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.startswith("/internal/face-artifacts/"):
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                sample_id = self.path.rsplit("/", 1)[1]
                assert self.headers["Content-Type"] == "image/jpeg"
                assert self.headers["X-ApexFabric-Artifact-SHA256"] == digest
                received.append(("artifact", sample_id, raw))
                self.respond({
                    "artifact_id": "artifact-sha256-" + digest.removeprefix("sha256:"),
                    "sample_id": sample_id, "sha256": digest,
                    "content_type": "image/jpeg", "size_bytes": len(raw), "status": "created",
                }, 201)
                return
            body = json.loads(raw)
            assert body["track_id"] == "7"
            assert body["dimensions"] == 512 and len(body["embedding"]) == 512
            assert body["artifact_id"].startswith("artifact-sha256-")
            assert body["artifact_sha256"].startswith("sha256:")
            assert "face_crop_url" not in body
            received.append(("sample", body["sample_id"]))
            self.respond({"sample_id": body["sample_id"]}, 201)

        def respond(self, payload, status):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        crop = outbox / "face-test.jpg"
        crop.write_bytes(b"exact-jpeg-bytes")
        digest = hashlib.sha256(crop.read_bytes()).hexdigest()
        record = outbox / "face-test.json"
        record.write_text(json.dumps({
            "schema_version": 2,
            "created_at": time.time(),
            "sample": {
                "sample_id": "face-test", "event_id": "event-test", "camera_id": "cam1",
                "track_id": "7", "observed_at": "2026-09-21T12:00:00Z",
                "model_id": "face-embedding-model-v1", "dimensions": 512,
                "embedding": [1.0] + [0.0] * 511, "quality": 0.9,
            },
            "artifact": {
                "path": str(crop), "ref": "snapshots/cam1/face-test.jpg",
                "content_type": "image/jpeg", "size_bytes": crop.stat().st_size,
                "sha256": "sha256:" + digest,
            },
            "delivery": {"artifact_ack": None, "embedding_ack": None,
                         "artifact_attempts": 0, "embedding_attempts": 0},
        }), encoding="utf-8")

        from pipeline.face_metrics import FaceMetrics
        monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path / "state"))
        uploader = FaceManagementUploader(
            outbox, f"http://127.0.0.1:{server.server_port}/internal/face-artifacts/<sample-id>",
            f"http://127.0.0.1:{server.server_port}/internal/face-samples", "test-token",
            FaceMetrics("cam1"),
        )
        uploader._cycle()

        assert received[0] == ("artifact", "face-test", b"exact-jpeg-bytes")
        assert received[1] == ("sample", "face-test")
        assert not record.exists()
        assert not crop.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_artifact_ack_survives_embedding_outage_and_restart(monkeypatch, tmp_path):
    requests = {"artifact": 0, "sample": 0}
    allow_sample = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            nonlocal allow_sample
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if "/face-artifacts/" in self.path:
                requests["artifact"] += 1
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                sample_id = self.path.rsplit("/", 1)[1]
                return self.respond({
                    "artifact_id": "artifact-sha256-" + digest[7:], "sample_id": sample_id,
                    "sha256": digest, "content_type": "image/jpeg",
                    "size_bytes": len(raw), "status": "created",
                }, 201)
            requests["sample"] += 1
            body = json.loads(raw)
            if not allow_sample:
                return self.respond({"error": "offline"}, 503)
            self.respond({"sample_id": body["sample_id"]}, 201)

        def respond(self, payload, status):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        outbox = tmp_path / "outbox"; outbox.mkdir()
        crop = outbox / "crop.jpg"; crop.write_bytes(b"stable-crop")
        digest = hashlib.sha256(crop.read_bytes()).hexdigest()
        path = outbox / "sample.json"
        path.write_text(json.dumps({
            "schema_version": 2, "created_at": time.time(),
            "sample": {"sample_id": "sample-restart", "event_id": "event-restart",
                       "camera_id": "cam1", "track_id": "9", "observed_at": "2026-09-21T12:00:00Z",
                       "model_id": "face-embedding-model-v1", "dimensions": 512,
                       "embedding": [1.0] + [0.0] * 511, "quality": 0.8},
            "artifact": {"path": str(crop), "ref": "snapshots/crop.jpg",
                         "content_type": "image/jpeg", "size_bytes": crop.stat().st_size,
                         "sha256": "sha256:" + digest},
            "delivery": {"artifact_ack": None, "embedding_ack": None,
                         "artifact_attempts": 0, "embedding_attempts": 0},
        }))
        from pipeline.face_metrics import FaceMetrics
        monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path / "state"))
        args = (outbox, f"http://127.0.0.1:{server.server_port}/internal/face-artifacts/<sample-id>",
                f"http://127.0.0.1:{server.server_port}/internal/face-samples", "token")
        first = FaceManagementUploader(*args, FaceMetrics("cam1"))
        with pytest.raises(_RetryLater, match="retryable HTTP 503"):
            first._cycle()
        saved = json.loads(path.read_text())
        assert saved["delivery"]["artifact_ack"]["sample_id"] == "sample-restart"
        assert crop.exists()

        allow_sample = True
        restarted = FaceManagementUploader(*args, FaceMetrics("cam1"))
        restarted._cycle()
        assert requests == {"artifact": 1, "sample": 2}
        assert not path.exists() and not crop.exists()
    finally:
        server.shutdown(); server.server_close()


def test_conflicting_artifact_is_permanent_and_discards_biometric_bytes(monkeypatch, tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            self.send_response(409)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        outbox = tmp_path / "outbox"; outbox.mkdir()
        crop = outbox / "crop.jpg"; crop.write_bytes(b"conflicting-crop")
        digest = hashlib.sha256(crop.read_bytes()).hexdigest()
        path = outbox / "sample.json"
        path.write_text(json.dumps({
            "schema_version": 2, "created_at": time.time(),
            "sample": {"sample_id": "sample-conflict", "event_id": "event-conflict",
                       "camera_id": "cam1", "track_id": "3", "observed_at": "2026-09-21T12:00:00Z",
                       "model_id": "face-embedding-model-v1", "dimensions": 512,
                       "embedding": [1.0] + [0.0] * 511, "quality": 0.8},
            "artifact": {"path": str(crop), "ref": "snapshots/crop.jpg",
                         "content_type": "image/jpeg", "size_bytes": crop.stat().st_size,
                         "sha256": "sha256:" + digest},
            "delivery": {"artifact_ack": None, "embedding_ack": None,
                         "artifact_attempts": 0, "embedding_attempts": 0},
        }))
        from pipeline.face_metrics import FaceMetrics
        monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path / "state"))
        uploader = FaceManagementUploader(
            outbox, f"http://127.0.0.1:{server.server_port}/face-artifacts/<sample-id>",
            f"http://127.0.0.1:{server.server_port}/face-samples", "token", FaceMetrics("cam1"),
        )
        uploader._cycle()

        diagnostic = json.loads(path.read_text())
        assert diagnostic["delivery"]["permanent_error"]["status"] == 409
        assert "embedding" not in diagnostic["sample"]
        assert "path" not in diagnostic["artifact"]
        assert not crop.exists()
    finally:
        server.shutdown(); server.server_close()
