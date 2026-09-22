from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from services.worker.pipeline.types import Detection, FramePacket
from services.worker.pipeline.vehicle_crossings import VehicleCrossingPipeline, VehicleCrossingUploader


def _line(a=(0.1, 0.5), b=(0.9, 0.5), cooldown=10):
    return {
        "id": "entrance-line",
        "name": "Entrance line",
        "shape": "line",
        "points": [{"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}],
        "vehicle_classes": ["car", "truck", "bus", "motorcycle"],
        "minimum_track_age_frames": 3,
        "minimum_crossing_displacement": 0.02,
        "crossing_cooldown_seconds": cooldown,
    }


def _pipeline(tmp_path, monkeypatch, line=None):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "state" / "snapshots"))
    monkeypatch.delenv("APEXFABRIC_FACE_IDENTITY_TOKEN", raising=False)
    config = {"runtime_analytics": {"vehicle_entry_exit_counts": {"lines": [line or _line()]}}}
    return VehicleCrossingPipeline("cam1", config)


def _packet(index, bottom, hits=3, cls="car"):
    detection = Detection(
        bbox=[40, bottom - 20, 60, bottom],
        class_id=2,
        class_name=cls,
        confidence=0.91,
        model_name="vehicle",
        metadata={"track_id": 17, "track_hits": hits},
    )
    return FramePacket(
        index=index,
        name="cam1",
        frame=np.full((100, 100, 3), 80, dtype=np.uint8),
        detections=[detection],
    )


def _records(pipeline):
    return sorted(pipeline.outbox.glob("*.json"))


def test_right_to_left_emits_in_with_full_frame_evidence(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path, monkeypatch)
    first = _packet(1, 80, hits=3)
    second = _packet(2, 20, hits=4)

    pipeline.process(first)
    pipeline.process(second)

    records = _records(pipeline)
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["event"]["direction"] == "in"
    assert record["event"]["vehicle"]["class"] == "car"
    assert record["event"]["crossing_point"] == {"x": 0.5, "y": 0.2}
    evidence = Path(record["snapshot"]["path"])
    assert evidence.is_file()
    assert record["snapshot"]["sha256"] == hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert second.analytics_events == []


def test_reversing_endpoints_swaps_physical_direction(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path, monkeypatch, _line(a=(0.9, 0.5), b=(0.1, 0.5)))

    pipeline.process(_packet(1, 80, hits=3))
    pipeline.process(_packet(2, 20, hits=4))

    record = json.loads(_records(pipeline)[0].read_text())
    assert record["event"]["direction"] == "out"


def test_track_age_class_and_cooldown_prevent_false_duplicates(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path, monkeypatch, _line(cooldown=60))
    pipeline.process(_packet(1, 80, hits=1))
    pipeline.process(_packet(2, 20, hits=2))
    assert _records(pipeline) == []

    pipeline.process(_packet(3, 80, hits=3))
    pipeline.process(_packet(4, 20, hits=4))
    assert len(_records(pipeline)) == 1
    pipeline.process(_packet(5, 80, hits=5))
    assert len(_records(pipeline)) == 1

    pedestrian = _packet(6, 20, hits=6, cls="pedestrian")
    pipeline.process(pedestrian)
    assert len(_records(pipeline)) == 1


def test_uploader_removes_record_only_after_matching_ack(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    image = b"jpeg-evidence"
    digest = hashlib.sha256(image).hexdigest()
    snapshot = tmp_path / "crossing.jpg"
    snapshot.write_bytes(image)
    event = {"event_id": "event-1"}
    record = {
        "event": event,
        "snapshot": {"path": str(snapshot), "sha256": digest, "size_bytes": len(image)},
        "delivery": {"attempts": 0},
    }
    record_path = outbox / "event-1.json"
    record_path.write_text(json.dumps(record))

    class Response:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self):
            return json.dumps({
                "event_id": "event-1", "status": "created",
                "artifact_id": "artifact-sha256-" + digest,
                "artifact_sha256": "sha256:" + digest,
            }).encode()

    class Opener:
        def open(self, request, timeout):
            assert b'name="event"' in request.data
            assert b'name="snapshot"' in request.data
            return Response()

    uploader = VehicleCrossingUploader(outbox, "http://management/vehicle-crossings", "token")
    uploader.opener = Opener()
    uploader._submit(record_path, record)

    assert not record_path.exists()
    assert not snapshot.exists()
