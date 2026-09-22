from __future__ import annotations

import json
from pathlib import Path

from traffic_pilot_runtime.solution_image_entrypoint import (
    RuntimeState,
    _events_from_jsonl_line,
    _metrics,
    _resolve_snapshot_path,
)
from services.worker.pipeline.output_sinks import simple_event

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "image_schema"


def test_metrics_matches_apexfabric_outer_contract():
    state = RuntimeState("/configs/desired_state.json")
    payload = _metrics(state)

    assert "apexfabric_runtime_ready 0" in payload
    assert "apexfabric_runtime_cameras 0" in payload
    assert "apexfabric_runtime_revision 0" in payload


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

    examples = json.loads((SCHEMA_DIR / "event.examples.json").read_text())
    schemas = {
        "vehicle_count_event": "analytics-event.schema.json",
        "vehicle_entry_exit_request": "vehicle-crossing.schema.json",
        "vehicle_entry_exit_response_created": "vehicle-crossing-response.schema.json",
    }
    for name, schema_name in schemas.items():
        schema = json.loads((SCHEMA_DIR / schema_name).read_text())
        Draft202012Validator(schema).validate(examples[name])


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
    Draft202012Validator(json.loads((SCHEMA_DIR / "analytics-event.schema.json").read_text())).validate(event)


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
            "person_id": None,
            "track_id": "7",
            "match_confidence": None,
            "model_id": "face-embedding-model-v1",
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
    schema = json.loads((SCHEMA_DIR / "analytics-event.schema.json").read_text())
    from jsonschema import Draft202012Validator
    Draft202012Validator(schema).validate(event)


def test_face_event_serializer_accepts_normalized_bbox_and_null_identity():
    event = simple_event({
        "observation_id": "face-event",
        "observed_at": "2026-09-18T10:00:00Z",
        "use_case": "face_recognition",
        "type": "face_seen",
        "sample_id": "face-sample",
        "person_id": None,
        "track_id": "7",
        "match_confidence": None,
        "face_quality": 0.9,
        "subject": {
            "type": "face", "track_id": 7, "confidence": 0.95,
            "bbox": {"x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.4},
        },
    })

    assert event["subject"]["type"] == "face"
    assert event["subject"]["bbox"]["x1"] == 0.1
    assert event["track_id"] == "7"
    assert event["person_id"] is None
    assert event["match_confidence"] is None
