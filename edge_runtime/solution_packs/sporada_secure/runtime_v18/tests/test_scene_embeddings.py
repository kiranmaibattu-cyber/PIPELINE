from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import cv2
import numpy as np

WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from pipeline.scene_embeddings import (  # noqa: E402
    DEFAULT_EMBEDDING_SPACE,
    SCENE_DIMENSIONS,
    SCENE_MODEL_ID,
    SCENE_MODEL_VERSION,
    SceneEmbeddingPipeline,
)
from pipeline.sentinel_delivery import SentinelV2Uploader  # noqa: E402
from pipeline.types import FramePacket  # noqa: E402


class FakeSceneExtractor:
    model_id = SCENE_MODEL_ID
    model_version = SCENE_MODEL_VERSION
    dimension = SCENE_DIMENSIONS

    def embed(self, image):
        assert image.size
        vector = np.zeros(SCENE_DIMENSIONS, dtype=np.float32)
        vector[0] = 1.0
        return vector


def _camera_config():
    return {
        "analytics": {
            "scene_embeddings": {
                "zones": [],
                "embedding": {
                    "model_id": SCENE_MODEL_ID,
                    "model_version": SCENE_MODEL_VERSION,
                    "embedding_space": DEFAULT_EMBEDDING_SPACE,
                    "dimensions": SCENE_DIMENSIONS,
                },
                "emission": {"interval_seconds": 1, "jpeg_quality": 88},
            }
        },
        "runtime_analytics": {"scene_embeddings": {"zones": []}},
    }


def test_scene_embedding_is_durable_and_not_added_to_sse(monkeypatch, tmp_path):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("SENTINEL_INGEST_BASE_URL", raising=False)
    monkeypatch.delenv("SENTINEL_EDGE_TOKEN", raising=False)
    pipeline = SceneEmbeddingPipeline(
        "cam1", "edge1", FakeSceneExtractor(), _camera_config()
    )
    frame = np.full((120, 160, 3), 127, dtype=np.uint8)
    packet = FramePacket(index=9, name="cam1", frame=frame)

    assert pipeline.process(packet) == 1

    records = list((tmp_path / "state/sentinel_v2/outbox/cam1").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    observation = record["observation"]
    embedding = record["embeddings"][0]
    evidence = record["evidence"][0]
    assert observation["edge_id"] == "edge1"
    assert observation["stream_session_id"]
    assert observation["event_type"] == "scene_embedding_created"
    assert embedding["dimensions"] == 768
    assert len(embedding["vector"]) == 768
    assert np.isclose(np.linalg.norm(embedding["vector"]), 1.0)
    assert embedding["embedding_space"] == DEFAULT_EMBEDDING_SPACE
    assert packet.analytics_events == []

    evidence_path = Path(evidence["path"])
    body = evidence_path.read_bytes()
    assert evidence["metadata"]["sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()
    assert evidence["metadata"]["size_bytes"] == len(body)
    assert cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (120, 160)


def test_scene_zone_uses_roi_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path / "state"))
    config = _camera_config()
    config["runtime_analytics"]["scene_embeddings"]["zones"] = [{
        "id": "scooter-area",
        "name": "Scooter area",
        "normalized": True,
        "points": [
            {"x": 0.25, "y": 0.25}, {"x": 0.75, "y": 0.25},
            {"x": 0.75, "y": 0.75}, {"x": 0.25, "y": 0.75},
        ],
    }]
    pipeline = SceneEmbeddingPipeline("cam1", "edge1", FakeSceneExtractor(), config)
    packet = FramePacket(
        index=1, name="cam1", frame=np.full((100, 200, 3), 127, dtype=np.uint8)
    )

    assert pipeline.process(packet) == 1

    record_path = next((tmp_path / "state/sentinel_v2/outbox/cam1").glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["observation"]["location"]["zone_id"] == "scooter-area"
    assert record["evidence"][0]["metadata"]["width"] == 100
    assert record["evidence"][0]["metadata"]["height"] == 50


def test_uploader_preserves_dependency_order(monkeypatch, tmp_path):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("SENTINEL_INGEST_BASE_URL", raising=False)
    monkeypatch.delenv("SENTINEL_EDGE_TOKEN", raising=False)
    pipeline = SceneEmbeddingPipeline(
        "cam1", "edge1", FakeSceneExtractor(), _camera_config()
    )
    packet = FramePacket(
        index=1, name="cam1", frame=np.full((40, 60, 3), 127, dtype=np.uint8)
    )
    pipeline.process(packet)
    record_path = next(pipeline.store.outbox.glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    observation_id = record["observation"]["observation_id"]
    evidence_id = record["evidence"][0]["metadata"]["evidence_id"]
    embedding_id = record["embeddings"][0]["embedding_id"]
    digest = record["evidence"][0]["metadata"]["sha256"]
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            assert self.headers["Authorization"] == "Bearer token"
            if self.path.endswith("/observations"):
                payload = json.loads(raw)
                received.append("observation")
                self.respond({"status": "created", "observation_id": payload["observation_id"]})
            elif self.path.endswith("/evidence"):
                assert b'name="metadata"' in raw and b'name="file"' in raw
                received.append("evidence")
                self.respond({
                    "status": "stored", "evidence_id": evidence_id, "sha256": digest,
                })
            elif self.path.endswith("/embeddings"):
                payload = json.loads(raw)
                received.append("embedding")
                self.respond({"status": "created", "embedding_id": payload["embedding_id"]})
            else:
                self.send_error(404)

        def respond(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        uploader = SentinelV2Uploader(
            pipeline.store, f"http://127.0.0.1:{server.server_port}", "token"
        )
        uploader.submit(record_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert received == ["observation", "evidence", "embedding"]
    assert not record_path.exists()
    assert not list(pipeline.store.evidence.glob("*"))
    assert observation_id and evidence_id and embedding_id
