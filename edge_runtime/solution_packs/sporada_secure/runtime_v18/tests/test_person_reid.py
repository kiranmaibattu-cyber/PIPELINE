from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np

WORKER_ROOT = Path(__file__).resolve().parents[1] / "services" / "worker"
sys.path.insert(0, str(WORKER_ROOT))

from pipeline.person_reid import (
    BODY_DIMENSIONS,
    FACE_DIMENSIONS,
    GAIT_DIMENSIONS,
    OpenVINOGaitExtractor,
    PersonReidPipeline,
    PresenceResolver,
    opengait_pretreat,
)
from pipeline.types import Detection, FramePacket


class FakeBodyExtractor:
    def embed(self, crops):
        vector = np.zeros(BODY_DIMENSIONS, np.float32)
        vector[0] = 1.0
        return np.stack([vector.copy() for _ in crops])


class FakeGaitExtractor:
    sequence_length = 30
    source = "background_subtraction"

    def __init__(self):
        self.cache = {}

    def collect(self, frame, people, camera_id, keys):
        vector = np.zeros(GAIT_DIMENSIONS, np.float32)
        vector[3] = 1.0
        for person in people:
            track_id = int(person.metadata["track_id"])
            self.cache[keys[track_id]] = vector.copy()
        return {}

    def rekey(self, old_key, new_key):
        if old_key in self.cache:
            self.cache[new_key] = self.cache.pop(old_key)

    def cached(self, key):
        return self.cache.get(key)


def _config():
    return {
        "analytics": {
            "person_reid": {
                "embedding": {
                    "body": {
                        "model_id": "transreid_ssl_int8", "model_version": "v1",
                        "embedding_space": "transreid_ssl_int8@v1:l2:384",
                        "dimensions": 384,
                    },
                    "face": {
                        "model_id": "face-embedding-model-v1",
                        "model_version": "adaface_ir101_int8-v1",
                        "embedding_space": "adaface-ir101-int8@v1:aligned112:l2:512",
                        "dimensions": 512,
                    },
                    "gait": {
                        "model_id": "gaitbase_int8", "model_version": "v1",
                        "embedding_space": "gaitbase_int8@v1:30x64x44:parts16x256:l2:4096",
                        "dimensions": 4096,
                    },
                },
                "emission": {
                    "interval_seconds": 5,
                    "jpeg_quality": 88,
                    "silhouette_interval_frames": 1,
                    "minimum_body_quality": 0,
                },
                "gait": {
                    "silhouette_source": "background_subtraction",
                    "minimum_motion_pixels": 2,
                },
                "reassociation": {
                    "maximum_gap_seconds": 8,
                    "minimum_body_similarity": 0.78,
                    "maximum_center_distance_pixels": 320,
                },
            }
        },
        "runtime_analytics": {"person_reid": {"zones": []}},
    }


def test_multimodal_reid_record_is_durable_and_not_sse(monkeypatch, tmp_path):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    pipeline = PersonReidPipeline(
        "cam1", "edge1", FakeBodyExtractor(), FakeGaitExtractor(), _config()
    )
    frame = np.random.default_rng(7).integers(0, 255, (240, 320, 3), np.uint8)
    person = Detection(
        bbox=[70, 30, 190, 225], class_id=0, class_name="pedestrian",
        confidence=0.92, model_name="vehicle", metadata={"track_id": 7},
    )
    face_vector = np.zeros(FACE_DIMENSIONS, np.float32)
    face_vector[4] = 1.0
    face = SimpleNamespace(
        track_id=7, bbox=[105, 45, 150, 95], quality=0.87,
        embedding=face_vector,
    )
    packet = FramePacket(index=1, name="cam1", frame=frame, detections=[person])

    assert pipeline.process(packet, [person], [face]) == 1
    assert packet.analytics_events == []

    paths = list((tmp_path / "sentinel_v2" / "outbox" / "cam1").glob("*.json"))
    assert len(paths) == 1
    record = json.loads(paths[0].read_text())
    assert record["observation"]["event_type"] == "reid_observation"
    assert record["observation"]["presence_id"].startswith("cam1:presence:")
    dimensions = {item["modality"]: item["dimensions"] for item in record["embeddings"]}
    assert dimensions == {"body": 384, "gait": 4096, "face": 512}
    assert {asset["metadata"]["evidence_type"] for asset in record["evidence"]} == {
        "person_crop", "face_crop",
    }


def test_presence_reassociation_keeps_presence_id():
    resolver = PresenceResolver("cam1", max_gap=8, threshold=0.78)
    vector = np.zeros(BODY_DIMENSIONS, np.float32)
    vector[0] = 1.0
    first, relation = resolver.resolve(4, vector, [0, 0, 10, 20], 100.0, {4})
    assert relation is None

    second, relation = resolver.resolve(19, vector, [1, 0, 11, 20], 102.0, {19})
    assert second == first
    assert relation["previous_track_id"] == 4
    assert relation["method"] == "body_reid_temporal"


def test_opengait_silhouette_normalization():
    mask = np.zeros((100, 80), np.uint8)
    mask[10:95, 25:58] = 255
    normalized = opengait_pretreat(mask)

    assert normalized.shape == (64, 44)
    assert set(np.unique(normalized)) <= {0, 255}


def test_mog2_extracts_silhouette_inside_person_box():
    extractor = OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
    extractor.backgrounds = {}
    person = SimpleNamespace(bbox=[20, 10, 80, 110])
    background = np.zeros((120, 100, 3), np.uint8)

    # Let the fixed-camera background model settle before introducing motion.
    for _ in range(20):
        extractor._silhouettes(background, [person], "cam1")

    moving = background.copy()
    moving[25:105, 35:65] = 255
    silhouette = extractor._silhouettes(moving, [person], "cam1")[0]

    assert silhouette is not None
    assert silhouette.shape == (64, 44)
