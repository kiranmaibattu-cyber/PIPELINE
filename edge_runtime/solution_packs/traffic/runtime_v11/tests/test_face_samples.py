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
    model_id = "adaface-ir101-int8-v1"
    embedding_space = "adaface-ir101-int8-v1:512:bgr-aligned-112"

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
    camera_config = {"runtime_analytics": {"face_recognition": {"zones": []}}}
    pipeline = FaceSamplePipeline("cam1", "edge1", FakeExtractor(), camera_config)
    packet = FramePacket(index=1, name="cam1", frame=np.zeros((120, 160, 3), dtype=np.uint8))
    person = SimpleNamespace(model_name="vehicle", class_name="pedestrian",
                             bbox=[10, 10, 100, 115], metadata={"track_id": 7})

    pipeline.process(packet, [person])

    records = list((state / "face_samples" / "outbox" / "cam1").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert len(record["sample"]["embedding"]) == 512
    assert set(record["sample"]["artifacts"]) == {"event_frame", "face_crop"}
    assert len(packet.analytics_events) == 1
    assert packet.analytics_events[0]["sample_id"] == record["sample"]["sample_id"]
    assert "embedding" not in packet.analytics_events[0]
    assert (state / record["sample"]["artifacts"]["face_crop"]["ref"]).is_file()


def test_management_uploads_artifact_before_sample_and_acknowledges(monkeypatch, tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            digest = self.path.rsplit("/", 1)[1]
            assert self.headers["Authorization"] == "Bearer test-token"
            assert hashlib.sha256(body).hexdigest() == digest
            received.append(("artifact", digest))
            self.respond({"sha256": digest})

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
        token = tmp_path / "token"
        token.write_text("test-token", encoding="utf-8")
        config = tmp_path / "management.json"
        config.write_text(json.dumps({
            "url": f"http://127.0.0.1:{server.server_port}",
            "token_file": str(token),
            "allow_insecure_loopback": True,
        }), encoding="utf-8")
        artifact = tmp_path / "face.jpg"
        artifact.write_bytes(b"jpeg-data")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        record = outbox / "face-test.json"
        record.write_text(json.dumps({
            "sample": {"sample_id": "face-test"},
            "artifacts": {"face_crop": {"artifact_id": digest, "path": str(artifact)}},
        }), encoding="utf-8")

        uploader = FaceManagementUploader("edge-test", outbox, str(config))
        uploader._cycle()

        assert received == [("artifact", digest), ("sample", "face-test")]
        assert not record.exists()
    finally:
        server.shutdown()
        server.server_close()
