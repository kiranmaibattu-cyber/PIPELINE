from __future__ import annotations

import json
from pathlib import Path

from traffic_pilot_runtime.solution_image_entrypoint import (
    RuntimeState,
    _events_from_jsonl_line,
    _metrics,
    _resolve_snapshot_path,
)


def test_metrics_matches_apexfabric_outer_contract():
    state = RuntimeState("/configs/desired_state.json")
    payload = _metrics(state)

    assert payload["format"] == "application/json"
    assert payload["events"] == {
        "protocol": "server-sent-events",
        "path": "/events",
        "delivery": "at-most-once",
        "start_position": "eof",
        "historical_replay": False,
        "heartbeat_seconds": 15.0,
    }
    assert payload["snapshots"]["path_prefix"] == "/snapshots/"
    assert payload["snapshots"]["source"] == "persistent_state"
    runtime = payload["runtime"]
    assert runtime["solution_pack"] == "sporada-secure"
    assert runtime["desired_state"]["path"] == "/configs/desired_state.json"
    assert runtime["desired_state"]["reload_state"] == "idle"


def test_worker_jsonl_is_normalized_to_apexfabric_analytics_event(monkeypatch, tmp_path):
    state_root = tmp_path / "state"
    snapshot_path = state_root / "snapshots" / "cam1" / "frame.jpg"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_bytes(b"jpeg")
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(state_root))
    line = json.dumps({
        "message_id": "msg-1",
        "observed_at": "2026-09-11T10:00:00Z",
        "camera": {"id": "cam1"},
        "events": [{
            "id": "evt-1",
            "type": "smoke_detected",
            "use_case": "fire_smoke_detection",
            "timestamp": "2026-09-11T10:00:01Z",
            "snapshot": {"path": str(snapshot_path)},
            "details": {"snapshot_path": str(snapshot_path)},
        }],
    })

    events = _events_from_jsonl_line(line)

    assert len(events) == 1
    event = events[0]
    assert event["schema_version"] == "1.0"
    assert event["solution_pack"] == "sporada-secure"
    assert event["application"] == "fire_smoke_detection"
    assert event["event_type"] == "smoke_detected"
    assert event["payload"]["snapshot_ref"] == "snapshots/cam1/frame.jpg"
    assert event["payload"]["snapshot_url"] == "/snapshots/cam1/frame.jpg"
    assert "snapshot" not in event["payload"]
    assert "snapshot_path" not in event["payload"].get("details", {})


def test_snapshot_refs_are_served_from_state_root(monkeypatch, tmp_path):
    state_root = tmp_path / "state"
    image = state_root / "snapshots" / "cam1" / "frame.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"jpeg")
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(state_root))

    assert _resolve_snapshot_path("snapshots/cam1/frame.jpg") == image.resolve()
    assert _resolve_snapshot_path("../outside.jpg") is None


def test_image_schema_event_examples_validate():
    import json
    from jsonschema import Draft202012Validator

    schema = json.loads(Path("image_schema/analytics-event.schema.json").read_text())
    examples = json.loads(Path("image_schema/event.examples.json").read_text())
    validator = Draft202012Validator(schema)
    for event in examples:
        validator.validate(event)


def test_normalized_worker_event_validates_against_image_schema(monkeypatch, tmp_path):
    import json
    from jsonschema import Draft202012Validator

    state_root = tmp_path / "state"
    snapshot_path = state_root / "snapshots" / "cam1" / "frame.jpg"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_bytes(b"jpeg")
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(state_root))
    line = json.dumps({
        "message_id": "msg-1",
        "observed_at": "2026-09-11T10:00:00Z",
        "camera": {"id": "cam1"},
        "events": [{
            "id": "evt-1",
            "type": "smoke_detected",
            "use_case": "fire_smoke_detection",
            "timestamp": "2026-09-11T10:00:01Z",
            "subject": {"type": "smoke", "confidence": 0.8, "bbox": {"x1": 1, "y1": 2, "x2": 20, "y2": 30}},
            "snapshot": {"path": str(snapshot_path)},
        }],
    })
    event = _events_from_jsonl_line(line)[0]
    Draft202012Validator(json.loads(Path("image_schema/analytics-event.schema.json").read_text())).validate(event)


def test_face_event_references_sample_without_exposing_embedding():
    line = json.dumps({
        "message_id": "msg-face",
        "observed_at": "2026-09-18T10:00:00Z",
        "camera": {"id": "cam1"},
        "events": [{
            "id": "face-event",
            "type": "face_seen",
            "use_case": "face_recognition",
            "timestamp": "2026-09-18T10:00:00Z",
            "sample_id": "face-0123456789abcdef0123456789abcdef",
            "model_id": "adaface-ir101-int8-v1",
            "embedding_space": "adaface-ir101-int8-v1:512:bgr-aligned-112",
            "face_quality": 0.9,
            "subject": {"type": "face", "confidence": 0.95,
                        "track_id": 7, "bbox": {"x1": 1, "y1": 2, "x2": 30, "y2": 40}},
            "snapshot_ref": "snapshots/cam1/faces/sample-frame.jpg",
            "snapshot_url": "/snapshots/cam1/faces/sample-frame.jpg",
            "snapshot_content_type": "image/jpeg",
            "snapshot_assets": {
                "event_frame": {"ref": "snapshots/cam1/faces/sample-frame.jpg",
                                "url": "/snapshots/cam1/faces/sample-frame.jpg", "content_type": "image/jpeg"},
                "face_crop": {"ref": "snapshots/cam1/faces/sample-face.jpg",
                              "url": "/snapshots/cam1/faces/sample-face.jpg", "content_type": "image/jpeg"},
            },
        }],
    })

    event = _events_from_jsonl_line(line)[0]

    assert event["application"] == "face_recognition"
    assert event["event_type"] == "face_seen"
    assert event["payload"]["sample_id"].startswith("face-")
    assert "embedding" not in event["payload"]
    assert "face_crop" in event["payload"]["snapshot_assets"]
