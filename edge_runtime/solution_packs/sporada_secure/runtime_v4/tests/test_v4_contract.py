from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import uuid

import jsonschema
import numpy as np

RUNTIME = Path(__file__).resolve().parents[1]
WORKER = RUNTIME / "services" / "worker"
sys.path[:0] = [str(RUNTIME), str(WORKER)]

from pipeline.types import Detection, FramePacket
from pipeline.v4_events import V4EventPipeline
from traffic_pilot_runtime.adapter import write_worker_config
from traffic_pilot_runtime.desired_state import DesiredStateValidator
from traffic_pilot_runtime.graph import compile_runtime_plan


SCHEMAS = RUNTIME / "image_schema"


def _id():
    return str(uuid.uuid4())


def _config(zone_id, line_id, threshold_id):
    v4 = {
        "deployment_id": "deployment-test", "config_revision": 7,
        "outputs": ["observations", "evidence"],
        "enabled_event_types": [
            "object_present", "person_count", "vehicle_count", "dwell",
            "threshold_exceeded", "plate_read", "line_cross", "zone_entry",
            "zone_exit", "fire_smoke_suspected", "camera_health",
        ],
        "zones": [
            {"id": zone_id, "name": "area", "kind": "polygon",
             "points": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8], [0.1, 0.8]]},
            {"id": line_id, "name": "gate", "kind": "line",
             "points": [[0.5, 0.0], [0.5, 1.0]], "forward_means": "entry"},
        ],
        "thresholds": [{"id": threshold_id, "metric": "person_count",
                        "zone_id": zone_id, "value": 0, "direction": "over"}],
        "embedding_profile_ids": [], "minimum_quality": 0.2,
        "evidence_cooldown_seconds": 1,
    }
    return {"analytics": {"v4_events": {"enabled": True, "v4": v4}}}


def _validate_records(root):
    schema = json.loads((SCHEMAS / "observation.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    records = []
    for path in (root / "sentinel_v4" / "outbox" / "cam1").glob("*.json"):
        record = json.loads(path.read_text())
        validator.validate(record["observation"])
        records.append(record)
    return records


def test_typed_v4_events_and_positive_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("SENTINEL_INGEST_BASE_URL", raising=False)
    zone_id, line_id, threshold_id = _id(), _id(), _id()
    pipeline = V4EventPipeline("cam1", _config(zone_id, line_id, threshold_id))
    frame = np.full((100, 100, 3), 127, np.uint8)
    person = Detection([20, 10, 40, 60], 0, "pedestrian", .9, "vehicle",
                       metadata={"track_id": 4})
    packet = FramePacket(1, "cam1", frame, [person])
    assert pipeline.process(packet) >= 4

    # A positive outside observation completes dwell and emits zone_exit.
    person.bbox = [85, 10, 99, 60]
    pipeline.process(FramePacket(2, "cam1", frame, [person]))
    records = _validate_records(tmp_path)
    event_types = {record["observation"]["event_type"] for record in records}
    assert {"camera_health", "object_present", "person_count", "vehicle_count",
            "dwell", "threshold_exceeded", "zone_exit"} <= event_types


def test_track_loss_does_not_emit_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    zone_id, line_id, threshold_id = _id(), _id(), _id()
    pipeline = V4EventPipeline("cam1", _config(zone_id, line_id, threshold_id))
    frame = np.zeros((100, 100, 3), np.uint8)
    person = Detection([20, 10, 40, 60], 0, "pedestrian", .9, "vehicle",
                       metadata={"track_id": 4})
    pipeline.process(FramePacket(1, "cam1", frame, [person]))
    pipeline.process(FramePacket(2, "cam1", frame, []))
    records = _validate_records(tmp_path)
    assert "zone_exit" not in {record["observation"]["event_type"] for record in records}
    completed = [record for record in records if record["observation"].get("status") == "completed"]
    assert completed == []


def test_v4_desired_state_compiles_to_worker_config(tmp_path):
    secrets = tmp_path / "cameras"
    secrets.mkdir()
    source = secrets / "cam1.url"
    source.write_text("rtsp://camera.test/live\n")
    zone_id, line_id, threshold_id = _id(), _id(), _id()
    camera = _config(zone_id, line_id, threshold_id)["analytics"]["v4_events"]["v4"]
    desired = {
        "contract": "v4", "edge_id": "edge-test", "deployment_id": "deployment-test",
        "revision": 7, "cameras": [{
            "camera_id": "cam1", "source": f"file:{source}", "fps": 8,
            "outputs": camera["outputs"],
            "enabled_event_types": camera["enabled_event_types"],
            "zones": camera["zones"], "thresholds": camera["thresholds"],
            "embedding_profile_ids": [], "minimum_quality": .2,
            "evidence_cooldown_seconds": 1,
        }],
    }
    path = tmp_path / "desired.json"
    path.write_text(json.dumps(desired))
    state = DesiredStateValidator(secrets).load(path)
    worker = write_worker_config(state, tmp_path / "worker.json")
    plan = compile_runtime_plan(state).to_dict()
    assert {"pedestrian_counting", "vehicle_counting", "plate_detection",
            "fire_smoke_detection", "v4_events"} <= set(state.cameras[0].apps)
    assert worker["cameras"][0]["analytics"]["v4_events"]["v4"]["config_revision"] == 7
    assert plan["deployment_id"] == "deployment-test"
