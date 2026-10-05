from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "worker"))

from pipeline import v5_events
from pipeline.v5_events import V5EventPipeline
from pipeline.sentinel_delivery import SentinelV2Uploader
from detectors.backends.openvino_ocr_async import AsyncOCR
from detectors.backends.openvino_smoke_fire import _device_with_fallback
import decode
from stream_fleet_openvino import require_accelerator_device
from traffic_pilot_runtime.desired_state import DesiredStateValidator
from traffic_pilot_runtime.solution_image_entrypoint import _hot_reload_signature


class Scene:
    def embed(self, image):
        vector = np.zeros(768, np.float32)
        vector[0] = 1
        return vector


class Body:
    def embed(self, crops):
        vector = np.zeros(384, np.float32)
        vector[0] = 1
        return np.stack([vector for _ in crops])


def packet(index, boxes=(), plate=None):
    detections = [SimpleNamespace(model_name="vehicle", class_name=kind,
                                  bbox=box, confidence=.9,
                                  metadata={"track_id": track})
                  for kind, track, box in boxes]
    if plate is not None:
        detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                          confidence=.9, metadata={"ocr_text": plate}, parent_id=1))
    return SimpleNamespace(index=index, frame=np.full((100, 100, 3), 128, np.uint8),
                           detections=detections)


def binding(use_case_id, app, scope="full_frame", ids=(), kind="vehicle", **extra):
    return {"use_case_id": use_case_id, "app": app, "scope": scope,
            "geometry_ids": list(ids), "object_types": [] if kind is None else [kind], **extra}


def config(bindings, zones=(), lines=()):
    return {"analytics": {"v5_events": {"v5": {
        "deployment_id": "test", "config_revision": 1, "bindings": bindings,
        "geometry": {"zones": list(zones), "counting_lines": list(lines)},
    }}}}


def events(tmp_path):
    return [json.loads(path.read_text())["observation"]
            for path in (tmp_path / "sentinel_v5_2" / "outbox" / "cam").glob("*.json")]


def test_presence_count_scene_and_anpr(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    bindings = [binding("presence", "vehicle_presence", "geometry", ["parking"],
                        embedding_profile_ids=["siglip2-base-v1"]),
                binding("count", "vehicle_counting", "geometry", ["parking"]),
                binding("plate", "anpr"),
                binding("scene", "scene_search", "geometry", ["parking"], None,
                        embedding_profile_ids=["siglip2-base-v1"], sample_interval_seconds=2)]
    pipeline = V5EventPipeline("cam", config(bindings, [zone]), scene=Scene())
    box = (20, 20, 70, 75)
    for index in range(1, 5):
        pipeline.process(packet(index, [("car", 1, box)], "KA01AB1234" if index == 3 else None))
    observations = events(tmp_path)
    assert len([item for item in observations if item["event_type"] == "object_present"]) == 1
    assert not any(item["event_type"] == "zone_entry" for item in observations)
    assert any(item["event_type"] == "zone_count" and item["count"] == 0 for item in observations)
    assert any(item["event_type"] == "zone_count" and item["count"] == 1 for item in observations)
    assert any(item["event_type"] == "plate_read" and item["plate_bbox_normalized"]["x1"] == .3 for item in observations)
    scene = next(item for item in observations if item["event_type"] == "scene_sample")
    assert scene["zone_id"] == "parking" and scene["scope"] == "geometry"
    record = json.loads(next(path for path in (tmp_path / "sentinel_v5_2" / "outbox" / "cam").glob("*.json")
                             if json.loads(path.read_text())["observation"]["observation_id"] == scene["observation_id"]).read_text())
    assert record["evidence"][0]["metadata"]["evidence_type"] == "scene_region_crop"
    assert record["evidence"][0]["metadata"]["source_bbox_normalized"] == {"x1": .1, "y1": .1, "x2": .9, "y2": .9}
    assert record["embeddings"][0]["evidence_id"] == record["evidence"][0]["metadata"]["evidence_id"]
    assert record["observation"]["stream_session_id"] == next(item["stream_session_id"] for item in observations if item["event_type"] == "object_present")


def test_loss_is_unknown_and_positive_exit_is_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = V5EventPipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .1
    for index in range(3, 5):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 95))]))
        clock.value += .1
    assert len([item for item in events(tmp_path) if item["event_type"] == "zone_exit"]) == 1
    clock.value += 2.1
    pipeline.process(packet(5))
    clock.value += 8
    pipeline.process(packet(6))
    output = events(tmp_path)
    assert len([item for item in output if item["event_type"] == "track_lost"]) == 1
    assert len([item for item in output if item["event_type"] == "presence_ended_unknown"]) == 1
    assert len([item for item in output if item["event_type"] == "zone_exit"]) == 1


def test_zone_boundary_jitter_does_not_emit_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = V5EventPipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    for index, bottom in enumerate((89, 91, 89, 91, 89), start=3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, bottom))]))
    assert not any(item["event_type"] == "zone_exit" for item in events(tmp_path))


def test_candidate_confirmation_requires_bounded_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    pipeline = V5EventPipeline("cam", config([binding("presence", "vehicle_presence")]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += 2
    assert not any(item["event_type"] == "object_present" for item in events(tmp_path))


def test_frame_receive_time_is_used_for_observations(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    pipeline = V5EventPipeline("cam", config([binding("presence", "vehicle_presence")]))
    for index in range(3):
        item = packet(index, [("car", 1, (20, 20, 70, 75))])
        item.frame_received_at = 100.0 + index * .1
        pipeline.process(item)
    present = next(item for item in events(tmp_path) if item["event_type"] == "object_present")
    assert present["observed_at"] == "1970-01-01T00:01:40.200000Z"


def test_same_track_recovery_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = V5EventPipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .1
    clock.value += 2.1
    pipeline.process(packet(3))
    pipeline.process(packet(4, [("car", 1, (20, 20, 70, 75))]))
    output = events(tmp_path)
    recovered = [item for item in output if item["event_type"] == "presence_recovered"]
    assert len(recovered) == 1
    assert recovered[0]["previous_track_id"] == recovered[0]["track_id"] == "1"
    assert recovered[0]["zone_episodes"][0]["observation_state"] == "observed_inside"


def test_stream_disconnect_has_unknown_not_exit_and_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = V5EventPipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .1
    first_session = pipeline.session_id
    clock.value += 2
    pipeline.camera_status("unavailable", "stream_disconnected")
    clock.value += 1
    pipeline.camera_status("healthy", "none")
    assert pipeline.session_id != first_session
    output = events(tmp_path)
    assert not any(item["event_type"] == "zone_exit" for item in output)
    assert any(item["event_type"] == "camera_health" and item["state"] == "unavailable" for item in output)
    assert any(item["event_type"] == "track_lost" and item["reason"] == "stream_interrupted" for item in output)
    assert any(item["event_type"] == "presence_ended_unknown" and item["stream_session_id"] == first_session for item in output)
    assert any(item["event_type"] == "camera_health" and item["state"] == "healthy" and item["stream_session_id"] == pipeline.session_id for item in output)


def test_binding_revision_keeps_session_and_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    original = config([binding("a", "vehicle_presence")])
    pipeline = V5EventPipeline("cam", original)
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    first = next(item for item in events(tmp_path) if item["event_type"] == "object_present")
    session = pipeline.session_id
    updated = config([binding("b", "vehicle_presence")])
    updated["analytics"]["v5_events"]["v5"]["config_revision"] = 2
    pipeline.reconfigure(updated)
    assert pipeline.session_id == session
    clock.value += 61
    pipeline.process(packet(3, [("car", 1, (20, 20, 70, 75))]))
    present = [item for item in events(tmp_path) if item["event_type"] == "object_present"]
    assert len(present) == 2
    assert {item["presence_id"] for item in present} == {first["presence_id"]}
    latest = next(item for item in present if item["config_revision"] == 2)
    assert latest["reason"] == "periodic" and latest["use_case_id"] == "b"


def test_binding_revision_does_not_require_worker_restart():
    def desired(revision, use_case):
        camera = SimpleNamespace(camera_id="cam", name="cam", source="file:/x",
                                 fps=8.0, apps=("v5_events",),
                                 config={"v5": {"geometry": {}, "bindings": [binding(use_case, "vehicle_presence")]}})
        return SimpleNamespace(edge_id="edge", deployment_id="dep", revision=revision,
                               cameras=[camera])
    assert _hot_reload_signature(desired(1, "a")) == _hot_reload_signature(desired(2, "b"))


def test_shared_polygon_fans_out_with_one_episode(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    bindings = [binding("a", "vehicle_presence", "geometry", ["parking"]),
                binding("b", "vehicle_presence", "geometry", ["parking"])]
    pipeline = V5EventPipeline("cam", config(bindings, [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 95))]))
    for index in range(3, 5):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    output = events(tmp_path)
    entrances = [item for item in output if item["event_type"] == "zone_entry"]
    assert {item["use_case_id"] for item in entrances} == {"a", "b"}
    assert len({item["zone_episode_id"] for item in entrances}) == 1
    first = [item for item in output if item["event_type"] == "object_present"]
    assert len(first) == 1 and first[0]["use_case_id"] == "a"


def test_face_and_body_are_independent_samples(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["adaface-ir101-v18.1", "transreid-ssl-v18.1"])
    pipeline = V5EventPipeline("cam", config([identity]), body=Body())
    face_vector = np.zeros(512, np.float32)
    face_vector[0] = 1
    face = SimpleNamespace(track_id=2, embedding=face_vector, quality=.8, bbox=(40, 30, 60, 55))
    for index in range(1, 4):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]), [face] if index == 3 else [])
        clock.value += .6
    clock.value += 1
    pipeline.process(packet(4))
    output = events(tmp_path)
    assert len([item for item in output if item["event_type"] == "object_present"]) == 1
    features = [item for item in output if item["event_type"] == "person_feature_sample"]
    assert {item["kind"] for item in features} == {"face", "body"}
    assert len({item["presence_id"] for item in features}) == 1


def test_face_window_selects_best_original_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["adaface-ir101-v18.1"])
    pipeline = V5EventPipeline("cam", config([identity]))
    vector = np.zeros(512, np.float32)
    vector[0] = 1
    for index in range(3):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]))
    first = SimpleNamespace(track_id=2, embedding=vector, quality=.5, bbox=(40, 30, 60, 55))
    pipeline.process(packet(3, [("pedestrian", 2, (20, 10, 80, 90))]), [first])
    clock.value += 1
    better = SimpleNamespace(track_id=2, embedding=vector, quality=.8, bbox=(42, 30, 62, 55))
    pipeline.process(packet(4, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    assert not any(item["event_type"] == "person_feature_sample" for item in events(tmp_path))
    clock.value += .6
    pipeline.process(packet(5))
    selected = [item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]
    assert len(selected) == 1 and selected[0]["quality"] == .8
    assert selected[0]["source_frame_id"] == "cam:4"


def test_face_improvement_respects_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["adaface-ir101-v18.1"])
    pipeline = V5EventPipeline("cam", config([identity]))
    vector = np.zeros(512, np.float32)
    vector[0] = 1
    first = SimpleNamespace(track_id=2, embedding=vector, quality=.4, bbox=(40, 30, 60, 55))
    better = SimpleNamespace(track_id=2, embedding=vector, quality=.8, bbox=(40, 30, 60, 55))
    for index in range(3):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]),
                         [first] if index == 2 else [])
    clock.value += 2
    pipeline.process(packet(3, [("pedestrian", 2, (20, 10, 80, 90))]))
    pipeline.process(packet(4, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    clock.value += 2
    pipeline.process(packet(5, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 1
    clock.value += 4
    pipeline.process(packet(6, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    clock.value += 2
    pipeline.process(packet(7))
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 2


def test_disconnect_flushes_pending_face_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["adaface-ir101-v18.1"])
    pipeline = V5EventPipeline("cam", config([identity]))
    vector = np.zeros(512, np.float32)
    vector[0] = 1
    face = SimpleNamespace(track_id=2, embedding=vector, quality=.8, bbox=(40, 30, 60, 55))
    for index in range(3):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]),
                         [face] if index == 2 else [])
    assert not any(item["event_type"] == "person_feature_sample" for item in events(tmp_path))
    pipeline.camera_status("unavailable", "stream_disconnected")
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 1


def test_line_crossing_requires_actual_segment_intersection(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    line = {"id": "gate", "name": "Gate", "a": [.5, .1], "b": [.5, .9],
            "direction_mapping": {"left_to_right": "in", "right_to_left": "out"}}
    crossing = binding("cross", "vehicle_entry_exit_counts", "geometry", ["gate"])
    pipeline = V5EventPipeline("cam", config([crossing], lines=[line]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (10, 20, 50, 70))]))
    pipeline.process(packet(3, [("car", 1, (50, 20, 90, 70))]))
    crosses = [item for item in events(tmp_path) if item["event_type"] == "line_cross"]
    assert len(crosses) == 1 and crosses[0]["line_id"] == "gate"


def test_fire_episode_has_clear_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    pipeline = V5EventPipeline("cam", config([binding("hazard", "fire_smoke", kind=None)]))
    for index in range(3):
        sample = packet(index)
        sample.detections.append(SimpleNamespace(model_name="smoke_fire", class_name="fire",
                                                 bbox=(20, 20, 60, 60), confidence=.9))
        pipeline.process(sample)
        clock.value += .5
    first_negative = packet(3)
    pipeline.process(first_negative)
    clock.value += 9
    skipped = packet(4)
    skipped.analytics_state = {"fire_smoke_evaluated": False}
    pipeline.process(skipped)
    assert not any(item["event_type"] == "fire_smoke_cleared" for item in events(tmp_path))
    clock.value += 2
    pipeline.process(packet(5))
    output = events(tmp_path)
    suspected = next(item for item in output if item["event_type"] == "fire_smoke_suspected")
    cleared = next(item for item in output if item["event_type"] == "fire_smoke_cleared")
    assert cleared["hazard_episode_id"] == suspected["hazard_episode_id"]


def test_low_score_hazard_does_not_become_event(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    pipeline = V5EventPipeline("cam", config([binding("hazard", "fire_smoke", kind=None)]))
    for index in range(4):
        sample = packet(index)
        sample.detections.append(SimpleNamespace(model_name="smoke_fire", class_name="smoke",
                                                 bbox=(20, 20, 60, 60), confidence=.4))
        pipeline.process(sample)
    assert not any(item["event_type"] == "fire_smoke_suspected" for item in events(tmp_path))


def test_v5_accelerators_fail_closed(monkeypatch):
    core = SimpleNamespace(available_devices=["CPU"])
    with pytest.raises(RuntimeError):
        _device_with_fallback(core, "GPU")
    with pytest.raises(RuntimeError):
        AsyncOCR._resolve_device(core, "MULTI:GPU,NPU")
    with pytest.raises(ValueError):
        require_accelerator_device("scene", "CPU")
    with pytest.raises(ValueError):
        require_accelerator_device("scene", "AUTO:GPU,NPU")
    require_accelerator_device("ocr", "MULTI:GPU,NPU")
    monkeypatch.setattr(decode, "_HW", False)
    with pytest.raises(RuntimeError):
        decode._ffmpeg_cmd("rtsp://example.invalid/live", 8, True)


def test_plate_attempt_has_one_failure_outcome_and_later_correction(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    pipeline = V5EventPipeline("cam", config([binding("plate-a", "anpr"), binding("plate-b", "anpr")]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    attempted = packet(3, [("car", 1, (20, 20, 70, 75))])
    attempted.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                                 confidence=.8, metadata={"ocr_provisional": "KA01"}, parent_id=1))
    pipeline.process(attempted)
    clock.value += 13
    pipeline.process(packet(4, [("car", 1, (20, 20, 70, 75))]))
    outcomes = [item for item in events(tmp_path) if item["event_type"] == "plate_outcome"]
    assert len(outcomes) == 2 and all(item["outcome"] == "unreadable" for item in outcomes)
    pipeline.process(packet(5, [("car", 1, (20, 20, 70, 75))], "KA01AB1234"))
    clock.value += .1
    pipeline.process(packet(6, [("car", 1, (20, 20, 70, 75))], "KA01AB1235"))
    reads = [item for item in events(tmp_path) if item["event_type"] == "plate_read"]
    assert len(reads) == 4
    by_binding = {key: [item for item in reads if item["use_case_id"] == key]
                  for key in ("plate-a", "plate-b")}
    for items in by_binding.values():
        first = next(item for item in items if item["supersedes_observation_id"] is None)
        correction = next(item for item in items if item["supersedes_observation_id"] is not None)
        assert correction["supersedes_observation_id"] == first["observation_id"]


def test_delivery_orders_event_evidence_embedding(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    scene = binding("scene", "scene_search", kind=None,
                    embedding_profile_ids=["siglip2-base-v1"], sample_interval_seconds=2)
    pipeline = V5EventPipeline("cam", config([scene]), scene=Scene())
    pipeline.process(packet(1))
    path = next(item for item in pipeline.store.outbox.glob("*.json")
                if json.loads(item.read_text())["observation"]["event_type"] == "scene_sample")
    uploader = SentinelV2Uploader(pipeline.store, "http://localhost:18081", "test-token")
    stages = []

    def post_json(endpoint, payload, id_field, expected_id):
        stages.append(endpoint)
        return {"status": "stored", id_field: expected_id}

    def post_multipart(metadata, body):
        stages.append("/api/v5/ingest/evidence")
        return {"status": "stored", "evidence_id": metadata["evidence_id"],
                "sha256": metadata["sha256"]}

    monkeypatch.setattr(uploader, "_post_json", post_json)
    monkeypatch.setattr(uploader, "_post_multipart", post_multipart)
    uploader.submit(path)
    assert stages == ["/api/v5/ingest/events", "/api/v5/ingest/evidence",
                      "/api/v5/ingest/embeddings"]
    assert not path.exists()


def test_scene_pressure_skips_job_and_reports_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SENTINEL_SCENE_MAX_PENDING_BYTES", "0")
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    scene = binding("scene", "scene_search", kind=None,
                    embedding_profile_ids=["siglip2-base-v1"], sample_interval_seconds=2)
    pipeline = V5EventPipeline("cam", config([scene]), scene=Scene())
    pipeline.process(packet(1))
    output = events(tmp_path)
    assert not any(item["event_type"] == "scene_sample" for item in output)
    assert any(item["event_type"] == "camera_health" and item["reason"] == "backpressure" for item in output)
    pipeline.scene_pressure_limit = 1024 ** 2
    clock.value += 6
    pipeline.process(packet(2))
    output = events(tmp_path)
    assert any(item["event_type"] == "scene_sample" for item in output)
    assert any(item["event_type"] == "camera_health" and item["state"] == "healthy" and item["observed_at"] == v5_events.utc(clock.value) for item in output)


def test_real_http_event_evidence_embedding_exchange(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    scene = binding("scene", "scene_search", kind=None,
                    embedding_profile_ids=["siglip2-base-v1"], sample_interval_seconds=2)
    pipeline = V5EventPipeline("cam", config([scene]), scene=Scene())
    pipeline.process(packet(1))
    record_path = next(item for item in pipeline.store.outbox.glob("*.json")
                       if json.loads(item.read_text())["observation"]["event_type"] == "scene_sample")
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            assert self.headers["Authorization"] == "Bearer test-token"
            if self.path.endswith("/evidence"):
                envelope = b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
                parts = list(BytesParser(policy=email_policy).parsebytes(envelope).iter_parts())
                assert len(parts) == 2
                metadata = json.loads(parts[0].get_payload(decode=True))
                image_bytes = parts[1].get_payload(decode=True)
                assert len(image_bytes) == metadata["size_bytes"]
                assert image_bytes[:2] == b"\xff\xd8"
                reply = {"status": "stored", "evidence_id": metadata["evidence_id"],
                         "sha256": metadata["sha256"]}
            else:
                item = json.loads(body)
                identifier = "embedding_id" if self.path.endswith("/embeddings") else "observation_id"
                reply = {"status": "stored", identifier: item[identifier]}
            received.append(self.path)
            output = json.dumps(reply).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        uploader = SentinelV2Uploader(pipeline.store, f"http://127.0.0.1:{server.server_port}", "test-token")
        uploader.submit(record_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert received == ["/api/v5/ingest/events", "/api/v5/ingest/evidence",
                        "/api/v5/ingest/embeddings"]
    assert not record_path.exists()


def test_desired_state_rejects_stale_fields(tmp_path):
    secret_root = tmp_path / "cameras"
    secret_root.mkdir()
    (secret_root / "cam.url").write_text("rtsp://127.0.0.1/live")
    validator = DesiredStateValidator(secret_root)
    validator.schema["$defs"]["camera"]["properties"]["source"]["pattern"] = r"^file:.*cam\.url$"
    data = {"contract": "sentry-v5", "schema_version": "5.2", "edge_id": "edge",
            "deployment_id": "test", "revision": 1,
            "cameras": [{"camera_id": "cam", "source": f"file:{secret_root / 'cam.url'}", "fps": 8,
                         "bindings": [binding("count", "vehicle_counting")]}]}
    validator.validate(data)
    data["cameras"][0]["apps"] = ["vehicle_counting"]
    try:
        validator.validate(data)
    except ValueError:
        pass
    else:
        raise AssertionError("stale apps list was accepted")
