from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import threading

import numpy as np

WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from detectors.backends.openvino_face import FaceSample  # noqa: E402
from pipeline.face_samples import FaceManagementUploader, FaceSamplePipeline  # noqa: E402
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
    assert len(record["sample"]["embedding"]) == 512
    assert record["sample"]["face_crop_url"].startswith("/snapshots/")
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).parents[1] / "image_schema/face-sample.schema.json").read_text())
    Draft202012Validator(schema).validate(record["sample"])
    assert len(packet.analytics_events) == 1
    assert packet.analytics_events[0]["sample_id"] == record["sample"]["sample_id"]
    assert "embedding" not in packet.analytics_events[0]
    assert next((state / "snapshots/cam1/faces").glob("*-face.jpg")).is_file()


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
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"] == "Bearer test-token"
            received.append(("sample", body["sample_id"]))
            self.respond({"sample_id": body["sample_id"]})

        def respond(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
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
        record = outbox / "face-test.json"
        record.write_text(json.dumps({
            "sample": {"sample_id": "face-test"},
            "artifacts": {},
        }), encoding="utf-8")

        from pipeline.face_metrics import FaceMetrics
        monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path / "state"))
        uploader = FaceManagementUploader(
            outbox, f"http://127.0.0.1:{server.server_port}/internal/face-samples", "test-token",
            FaceMetrics("cam1"),
        )
        uploader._cycle()

        assert received == [("sample", "face-test")]
        assert not record.exists()
    finally:
        server.shutdown()
        server.server_close()
