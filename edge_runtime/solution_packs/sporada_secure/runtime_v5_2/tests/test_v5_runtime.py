from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "worker"))

from pipeline import v5_events
from pipeline.v5_events import V5EventPipeline
from pipeline.motion_gate import MotionGateController
from pipeline.surface_stream import SurfaceSwitch
from pipeline.ocr_stabilizer import OcrStabilizer
from pipeline.sentinel_delivery import SentinelV2Uploader
from pipeline.face_samples import AdaptiveFaceScheduler, FaceSamplePipeline
from pipeline.runtime_telemetry import CameraTelemetry
from pipeline.resource_metrics import AcceleratorSampler, CgroupSampler
from detectors.backends.openvino_ocr_async import AsyncOCR
from detectors.backends.openvino_smoke_fire import _device_with_fallback
from detectors.backends.openvino_face import OpenVINOFaceExtractor, _pose_quality, _rotation_to_euler
import decode
from stream_fleet_openvino import require_accelerator_device, should_evaluate_fire_smoke
from traffic_pilot_runtime.desired_state import DesiredStateValidator
from traffic_pilot_runtime.solution_image_entrypoint import (
    RuntimeState, _health_payload, _hot_reload_signature, _metrics, _per_camera_state,
)


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


class Gait:
    def __init__(self):
        self.buffers = {}
        self.buffer_times = {}

    def collect(self, frame, people, camera_id, keys, captured_at=None):
        vector = np.zeros(4096, np.float32)
        vector[0] = 1
        output = {}
        for person in people:
            track_id = int(person.metadata["track_id"])
            key = keys[track_id]
            self.buffers[key] = [np.ones((64, 44), np.uint8) for _ in range(24)]
            self.buffer_times[key] = [float(captured_at or 0.0) - 1.0, float(captured_at or 0.0)]
            output[track_id] = vector
        return output


def test_camera_runtime_telemetry_reports_rates_latency_outputs_and_resources(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    telemetry = CameraTelemetry(
        "cam-a", target_fps=5,
        devices={"vehicle": "GPU", "face": "NPU"},
        interval_seconds=0.01,
    )
    telemetry.frame(inferred=True)
    telemetry.frame(inferred=False)
    telemetry.observe_latency("detection.vehicle_person", 0.012, device="GPU")
    telemetry.event_persisted({
        "event_type": "object_present",
        "use_case_id": "presence-a",
        "observed_at": v5_events.utc(time.time() - 0.05),
    }, 0.002)
    time.sleep(0.02)
    payload = telemetry.flush(force=True)
    assert payload["processing"]["target_fps"] == 5
    assert payload["processing"]["decoded_fps"] > 0
    assert payload["processing"]["inference_fps"] > 0
    assert payload["processing"]["output_events_per_second"] > 0
    assert payload["outputs"]["by_event_type"]["object_present"]["total"] == 1
    assert payload["outputs"]["by_use_case_id"]["presence-a"]["total"] == 1
    assert payload["use_case_processing"]["detection.vehicle_person"]["avg_ms"] == 12
    assert payload["outputs"]["production_latency_by_event_type"]["object_present"]["avg_ms"] >= 40
    assert payload["resources"]["process"]["rss_mb"] > 0
    accelerator = payload["resources"]["accelerators"]["GPU"]
    assert accelerator["attributed_stage_wall_ms_per_second"] > 0
    assert accelerator["model_calls_in_window"] == 1
    assert accelerator["scope"] == "camera_attributed_model_stages"
    assert (tmp_path / "metrics" / "runtime-camera-cam-a.json").is_file()


def test_health_payload_and_prometheus_metrics_include_per_camera_telemetry(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    telemetry = CameraTelemetry(
        "cam-a", target_fps=5, devices={"vehicle": "GPU"}, interval_seconds=0.01,
    )
    telemetry.frame(inferred=True)
    telemetry.observe_latency("detection.vehicle_person", 0.01, device="GPU")
    telemetry.event_persisted({
        "event_type": "zone_count", "use_case_id": "count-a",
        "observed_at": v5_events.utc(time.time()),
    }, 0.001)
    time.sleep(0.02)
    telemetry.flush(force=True)
    state = RuntimeState("/configs/desired_state.json")
    with state.lock:
        state.ready = True
        state.worker_running = True
        state.active_revision = 6
        state.plan = {"edge_id": "edge-a", "cameras": [{"camera_id": "cam-a"}]}
    payload = _health_payload(state)
    assert payload["ok"] is True
    assert payload["ready"] is True
    camera = payload["telemetry"]["cameras"][0]
    assert camera["camera_id"] == "cam-a"
    assert camera["fresh"] is True
    assert camera["camera_health"] == {
        "state": "degraded", "reason": "worker_starting",
        "last_reliable_at": None, "as_of": None, "retrying": True,
    }
    assert camera["processing"]["decoded_fps"] > 0
    assert payload["telemetry"]["accelerators"]["GPU"]["hardware_utilization_pct"] is None
    prometheus = _metrics(state)
    assert 'sentinel_camera_decoded_fps{camera_id="cam-a"}' in prometheus
    assert 'sentinel_use_case_latency_seconds{camera_id="cam-a",stage="detection.vehicle_person"}' in prometheus
    assert 'sentinel_events_emitted_total{camera_id="cam-a",event_type="zone_count"} 1' in prometheus


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


def test_motion_gate_starts_active_then_releases_and_sweeps():
    gate = MotionGateController(sweep_seconds=1, start_active=True)
    frame = np.zeros((180, 320, 3), np.uint8)
    for index in range(6):
        state, run, reason = gate.update(frame, index / 10, 10)
        assert (state, run, reason) == ("TRACKING", True, "active")
        released = gate.observe_models(False, False)
    assert released
    assert not gate.tracking
    for index in range(6, 16):
        state, run, reason = gate.update(frame, index / 10, 10)
        assert state == "MOTION"
        if run:
            assert reason == "sweep"
            break
    else:
        raise AssertionError("motion gate did not issue an idle sweep")


def test_periodic_fire_smoke_cadence_and_idle_sweep_override():
    detector = SimpleNamespace(should_process=lambda frame_index: frame_index % 5 == 0)
    assert not should_evaluate_fire_smoke(detector, 3, idle_sweep=False)
    assert should_evaluate_fire_smoke(detector, 5, idle_sweep=False)
    assert should_evaluate_fire_smoke(detector, 3, idle_sweep=True)


def test_motion_gate_preserves_negative_progress_when_periodic_model_is_skipped():
    gate = MotionGateController(release_misses=2, start_active=True)
    assert not gate.observe_models(False, False, fully_evaluated=True)
    assert not gate.observe_models(False, False, fully_evaluated=False)
    assert gate.observe_models(False, False, fully_evaluated=True)


def test_cgroup_and_npu_hardware_counters_are_measured(tmp_path, monkeypatch):
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("usage_usec 1000000\n", encoding="utf-8")
    (cgroup / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
    (cgroup / "memory.current").write_text(str(256 * 1024 * 1024), encoding="utf-8")
    (cgroup / "memory.max").write_text(str(1024 * 1024 * 1024), encoding="utf-8")
    (cgroup / "pids.current").write_text("42\n", encoding="utf-8")
    cgroup_sampler = CgroupSampler(cgroup)
    time.sleep(0.01)
    (cgroup / "cpu.stat").write_text("usage_usec 1010000\n", encoding="utf-8")
    cgroup_sample = cgroup_sampler.sample()
    assert cgroup_sample["measurement_status"] == "measured"
    assert cgroup_sample["cpu_cores_used"] > 0
    assert cgroup_sample["cpu_limit_cores"] == 2
    assert cgroup_sample["memory_current_mb"] == 256
    assert cgroup_sample["memory_limit_utilization_pct"] == 25

    npu_busy = tmp_path / "npu_busy"
    npu_memory = tmp_path / "npu_memory"
    npu_frequency = tmp_path / "npu_frequency"
    npu_max_frequency = tmp_path / "npu_max_frequency"
    gpu_frequency = tmp_path / "gpu_frequency"
    gpu_max_frequency = tmp_path / "gpu_max_frequency"
    npu_busy.write_text("1000000\n", encoding="utf-8")
    npu_memory.write_text("4096\n", encoding="utf-8")
    npu_frequency.write_text("800\n", encoding="utf-8")
    npu_max_frequency.write_text("1600\n", encoding="utf-8")
    gpu_frequency.write_text("900\n", encoding="utf-8")
    gpu_max_frequency.write_text("2300\n", encoding="utf-8")
    monkeypatch.setenv("SENTINEL_NPU_BUSY_TIME_PATH", str(npu_busy))
    monkeypatch.setenv("SENTINEL_NPU_MEMORY_PATH", str(npu_memory))
    monkeypatch.setenv("SENTINEL_NPU_FREQUENCY_PATH", str(npu_frequency))
    monkeypatch.setenv("SENTINEL_NPU_MAX_FREQUENCY_PATH", str(npu_max_frequency))
    monkeypatch.setenv("SENTINEL_GPU_FREQUENCY_PATH", str(gpu_frequency))
    monkeypatch.setenv("SENTINEL_GPU_MAX_FREQUENCY_PATH", str(gpu_max_frequency))
    accelerator_sampler = AcceleratorSampler()
    time.sleep(0.01)
    npu_busy.write_text("1005000\n", encoding="utf-8")
    accelerators = accelerator_sampler.sample()
    assert accelerators["NPU"]["hardware_measurement_status"] == "measured"
    assert 0 < accelerators["NPU"]["hardware_utilization_pct"] <= 100
    assert accelerators["NPU"]["memory_used_bytes"] == 4096
    assert accelerators["GPU"]["hardware_utilization_pct"] is None
    assert accelerators["GPU"]["current_frequency_mhz"] == 900


def test_head_pose_rotation_conversion_and_quality_ordering():
    def rotation(pitch, yaw, roll):
        x, y, z = np.radians([pitch, yaw, roll])
        rx = np.asarray([[1, 0, 0], [0, np.cos(x), -np.sin(x)],
                         [0, np.sin(x), np.cos(x)]])
        ry = np.asarray([[np.cos(y), 0, np.sin(y)], [0, 1, 0],
                         [-np.sin(y), 0, np.cos(y)]])
        rz = np.asarray([[np.cos(z), -np.sin(z), 0], [np.sin(z), np.cos(z), 0],
                         [0, 0, 1]])
        return rz @ ry @ rx

    actual = _rotation_to_euler(rotation(12, -24, 8))
    assert actual == pytest.approx((12, -24, 8), abs=1e-5)
    frontal = _pose_quality(3, 4, 2, 25, 35, 25)
    turned = _pose_quality(20, 30, 18, 25, 35, 25)
    boundary = _pose_quality(25, 35, 25, 25, 35, 25)
    assert frontal == pytest.approx(1.0)
    assert frontal > turned > boundary
    assert boundary == pytest.approx(0.0)


def test_head_pose_rotation_rejects_nonfinite_matrix():
    matrix = np.eye(3)
    matrix[0, 0] = np.nan
    with pytest.raises(ValueError, match="not finite"):
        _rotation_to_euler(matrix)


def test_out_of_range_head_pose_is_rejected_before_face_embedding():
    extractor = object.__new__(OpenVINOFaceExtractor)
    extractor.min_face_px = 28
    extractor.max_pitch = 25.0
    extractor.max_yaw = 35.0
    extractor.max_roll = 25.0
    extractor.pose_context = 0.2
    extractor._suppressions = {}
    landmarks = np.asarray([[28, 30], [52, 30], [40, 42], [31, 55], [49, 55]], np.float32)
    extractor._detect = lambda crop: [(np.asarray([20, 15, 60, 65], np.float32), landmarks, .9)]
    extractor._pose = SimpleNamespace(estimate=lambda crop: (5.0, 52.0, 2.0))
    extractor._embed = lambda chip: pytest.fail("AdaFace must not run for rejected head pose")
    person = SimpleNamespace(model_name="vehicle", class_name="pedestrian",
                             bbox=(20, 10, 100, 130), metadata={"track_id": 7})
    assert extractor.extract(np.full((160, 140, 3), 128, np.uint8), [person]) == []
    assert extractor.drain_suppressions() == {"head_pose_out_of_range": 1}


def test_face_scheduler_reduces_one_hour_high_quality_work(monkeypatch):
    monkeypatch.setenv("FACE_ADAPTIVE_SEARCH_INTERVAL_SECONDS", "0.6")
    monkeypatch.setenv("FACE_ADAPTIVE_HIGH_QUALITY_INTERVAL_SECONDS", "15")
    scheduler = AdaptiveFaceScheduler()
    scheduler.configure(selection_window=4, improvement_threshold=.1, improvement_cooldown=5)
    attempts = forwarded = 0
    for step in range(6001):
        now = step * .6
        if not scheduler.due(7, now):
            continue
        attempts += 1
        forwarded += int(scheduler.observe(7, now, .8))
    state = scheduler.states[7]
    assert state.mode == "high_quality"
    assert state.baseline_quality == pytest.approx(.8)
    assert attempts < 260
    assert forwarded < 10


def test_face_scheduler_uses_fast_recovery_window(monkeypatch):
    monkeypatch.setenv("FACE_ADAPTIVE_SEARCH_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("FACE_ADAPTIVE_RECOVERY_GAP_SECONDS", "2")
    monkeypatch.setenv("FACE_ADAPTIVE_RECOVERY_WINDOW_SECONDS", "4")
    scheduler = AdaptiveFaceScheduler()
    scheduler.configure(selection_window=1, improvement_threshold=.1, improvement_cooldown=5)
    assert scheduler.due(9, 0)
    assert scheduler.observe(9, 0, .8)
    assert scheduler.due(9, .5)
    assert scheduler.observe(9, .5, .82)
    assert scheduler.due(9, 1.0)
    assert scheduler.observe(9, 1.0, .81)
    assert scheduler.states[9].mode == "high_quality"
    assert scheduler.due(9, 4.0)
    assert scheduler.states[9].mode == "recovery"
    assert scheduler.observe(9, 4.0, .79)
    assert scheduler.due(9, 4.5)


def test_predicted_track_is_not_eligible_for_face_inference():
    pipeline = object.__new__(FaceSamplePipeline)
    pipeline.camera_config = {}
    person = SimpleNamespace(model_name="vehicle", class_name="pedestrian",
                             bbox=(10, 10, 80, 100),
                             metadata={"track_id": 4, "predicted": True})
    assert not pipeline._eligible(person, (120, 120, 3), 0)


def test_face_pose_crop_uses_original_frame_coordinates_and_axis_scaling():
    extractor = object.__new__(OpenVINOFaceExtractor)
    extractor.pose_context = .2
    frame = np.arange(180 * 240 * 3, dtype=np.int32).reshape(180, 240, 3)
    crop = extractor._pose_crop(frame, (100, 50, 140, 110))
    expected = frame[38:122, 92:148]
    assert crop.shape == (84, 56, 3)
    assert np.array_equal(crop, expected)
    assert np.shares_memory(crop, frame)


def test_face_extractor_maps_person_relative_face_to_original_frame():
    extractor = object.__new__(OpenVINOFaceExtractor)
    extractor.min_face_px = 28
    extractor.max_pitch = 25.0
    extractor.max_yaw = 35.0
    extractor.max_roll = 25.0
    extractor.pose_context = .2
    extractor._suppressions = {}
    landmarks = np.asarray([[25, 35], [65, 35], [45, 52], [30, 72], [60, 72]], np.float32)
    extractor._detect = lambda crop: [(np.asarray([20, 30, 80, 100], np.float32), landmarks, .9)]
    captured = {}

    def pose(pixels):
        captured["pose"] = pixels.copy()
        return 2.0, 3.0, 1.0

    extractor._pose = SimpleNamespace(estimate=pose)
    extractor._align = lambda crop, points: np.full((112, 112, 3), 127, np.uint8)
    extractor._embed = lambda chip: np.r_[np.float32(1), np.zeros(511, np.float32)]
    frame = np.arange(400 * 500 * 3, dtype=np.int32).reshape(400, 500, 3)
    person = SimpleNamespace(model_name="vehicle", class_name="pedestrian",
                             bbox=(100, 50, 300, 350), metadata={"track_id": 7})
    samples = extractor.extract(frame, [person])
    assert len(samples) == 1
    assert samples[0].bbox == (120, 80, 180, 150)
    assert np.array_equal(captured["pose"], frame[66:164, 108:192])


def test_face_evidence_bounds_are_square_original_frame_region():
    frame = np.arange(200 * 300 * 3, dtype=np.int32).reshape(200, 300, 3)
    bounds = V5EventPipeline._face_bounds(frame, (100, 50, 140, 110))
    crop, actual = V5EventPipeline._crop(frame, bounds)
    assert bounds == (60, 20, 180, 140)
    assert actual == bounds
    assert np.array_equal(crop, frame[20:140, 60:180])


def test_surface_switch_starts_active_then_releases():
    switch = SurfaceSwitch(sweep_seconds=1, start_active=True)
    assert switch.tracking
    for _ in range(5):
        assert not switch.observe_models(False, False)
        assert switch.tracking
    assert switch.observe_models(False, False)
    assert not switch.tracking


def test_quiet_frame_advances_lifecycle_without_negative_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = V5EventPipeline("cam", config([
        binding("presence", "vehicle_presence", "geometry", ["parking"]),
        binding("count", "vehicle_counting", "geometry", ["parking"]),
        binding("hazard", "fire_smoke", "geometry", ["parking"], None),
    ], [zone]))
    for index in range(3):
        item = packet(index, [("car", 1, (20, 20, 70, 75))])
        item.frame_received_at = clock.value
        pipeline.process(item)
        clock.value += .1
    before = events(tmp_path)
    zero_counts_before = len([item for item in before
                              if item["event_type"] == "zone_count" and item["count"] == 0])
    clock.value += 2.1
    pipeline.process_quiet(clock.value, 4)
    clock.value += 8
    pipeline.process_quiet(clock.value, 5)
    output = events(tmp_path)
    assert any(item["event_type"] == "track_lost" for item in output)
    assert any(item["event_type"] == "presence_ended_unknown" for item in output)
    assert not any(item["event_type"] == "zone_exit" for item in output)
    assert not any(item["event_type"] == "fire_smoke_cleared" for item in output)
    zero_counts_after = len([item for item in output
                             if item["event_type"] == "zone_count" and item["count"] == 0])
    assert zero_counts_after == zero_counts_before


def test_presence_count_scene_and_anpr(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    bindings = [binding("presence", "vehicle_presence", "geometry", ["parking"],
                        embedding_profile_ids=["siglip2-base-v1"]),
                binding("count", "vehicle_counting", "geometry", ["parking"]),
                binding("plate", "anpr"),
                binding("scene", "scene_search", "geometry", ["parking"], None,
                        embedding_profile_ids=["siglip2-base-v1"], sample_interval_seconds=2)]
    pipeline = V5EventPipeline("cam", config(bindings, [zone]), scene=Scene())
    box = (20, 20, 70, 75)
    for index in range(1, 6):
        pipeline.process(packet(index, [("car", 1, box)], "KA01AB1234" if index >= 4 else None))
        clock.value += 1
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


def test_frame_observed_time_precedes_receive_time_when_available(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    pipeline = V5EventPipeline("cam", config([binding("presence", "vehicle_presence")]))
    for index in range(3):
        item = packet(index, [("car", 1, (20, 20, 70, 75))])
        item.frame_observed_at = 90.0 + index * .1
        item.frame_received_at = 100.0 + index * .1
        pipeline.process(item)
    present = next(item for item in events(tmp_path) if item["event_type"] == "object_present")
    assert present["observed_at"] == "1970-01-01T00:01:30.200000Z"


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


def test_dwell_episode_updates_inside_time_and_completes_only_on_positive_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = V5EventPipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    outside = (20, 20, 70, 95)
    inside = (20, 20, 70, 75)
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, outside)]))
        clock.value += 1
    pipeline.process(packet(3, [("car", 1, inside)]))
    clock.value += 1
    pipeline.process(packet(4, [("car", 1, inside)]))
    clock.value += 5
    pipeline.process(packet(5, [("car", 1, inside)]))
    output = events(tmp_path)
    entry = next(item for item in output if item["event_type"] == "zone_entry")
    clock.value += 61
    pipeline.process(packet(6, [("car", 1, inside)]))
    present = next(item for item in events(tmp_path)
                   if item["event_type"] == "object_present" and item["reason"] == "periodic")
    episode = next(item for item in present["zone_episodes"] if item["zone_id"] == "parking")
    assert episode["zone_episode_id"] == entry["zone_episode_id"]
    assert episode["last_confirmed_inside_at"] == "1970-01-01T00:02:50Z"
    clock.value += 5
    pipeline.process(packet(7))
    assert not any(item["event_type"] == "zone_exit" for item in events(tmp_path))
    pipeline.process(packet(8, [("car", 1, outside)]))
    clock.value += 1
    pipeline.process(packet(9, [("car", 1, outside)]))
    exits = [item for item in events(tmp_path) if item["event_type"] == "zone_exit"]
    assert len(exits) == 1 and exits[0]["zone_episode_id"] == entry["zone_episode_id"]


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
    clock.value += 4
    pipeline.process(packet(4))
    output = events(tmp_path)
    assert len([item for item in output if item["event_type"] == "object_present"]) == 1
    features = [item for item in output if item["event_type"] == "person_feature_sample"]
    assert {item["kind"] for item in features} == {"face", "body"}
    assert len({item["presence_id"] for item in features}) == 1


def test_gait_sample_emits_sequence_metadata_and_embedding(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["gaitbase-v18.1"])
    pipeline = V5EventPipeline("cam", config([identity]), gait=Gait())
    for index in range(4):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]))
        clock.value += .5
    output = events(tmp_path)
    gait = next(item for item in output if item.get("kind") == "gait")
    assert gait["profile_id"] == "gaitbase-v18.1"
    assert gait["source_frame_id"] == "cam:3"
    assert gait["quality"] == pytest.approx(0.8)
    record = json.loads(next(path for path in (tmp_path / "sentinel_v5_2" / "outbox" / "cam").glob("*.json")
                             if json.loads(path.read_text())["observation"]["observation_id"] == gait["observation_id"]).read_text())
    embedding = record["embeddings"][0]
    assert embedding["kind"] == "gait"
    assert len(embedding["vector"]) == 4096
    assert embedding["usable_silhouette_count"] == 24
    assert embedding["sequence_started_at"] == "1970-01-01T00:01:40.500000Z"
    assert embedding["sequence_ended_at"] == "1970-01-01T00:01:41.500000Z"


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
    clock.value += 3.1
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
    clock.value += 4.1
    pipeline.process(packet(3, [("pedestrian", 2, (20, 10, 80, 90))]))
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 1
    pipeline.process(packet(4, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 1
    clock.value += 1.1
    pipeline.process(packet(5, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    clock.value += 4.1
    pipeline.process(packet(6))
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
    for outcome in outcomes:
        record = json.loads(next(path for path in (tmp_path / "sentinel_v5_2" / "outbox" / "cam").glob("*.json")
                                 if json.loads(path.read_text())["observation"]["observation_id"] == outcome["observation_id"]).read_text())
        assert record["evidence"][0]["metadata"]["evidence_type"] == "frame"
    first_read = packet(5, [("car", 1, (20, 20, 70, 75))])
    first_read.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                                 confidence=.8,
                                                 metadata={"ocr_text": "KA01AB1234", "ocr_confidence": .35},
                                                 parent_id=1))
    pipeline.process(first_read)
    clock.value += .1
    selected_read = packet(6, [("car", 1, (20, 20, 70, 75))])
    selected_read.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                                    confidence=.8,
                                                    metadata={"ocr_text": "KA01AB1234", "ocr_confidence": .80},
                                                    parent_id=1))
    pipeline.process(selected_read)
    clock.value += 6
    corrected_read = packet(7, [("car", 1, (20, 20, 70, 75))])
    corrected_read.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                                     confidence=.8,
                                                     metadata={"ocr_text": "KA01AB1235", "ocr_confidence": .95},
                                                     parent_id=1))
    pipeline.process(corrected_read)
    reads = [item for item in events(tmp_path) if item["event_type"] == "plate_read"]
    assert len(reads) == 4
    by_binding = {key: [item for item in reads if item["use_case_id"] == key]
                  for key in ("plate-a", "plate-b")}
    for items in by_binding.values():
        first = next(item for item in items if item["supersedes_observation_id"] is None)
        correction = next(item for item in items if item["supersedes_observation_id"] is not None)
        assert correction["supersedes_observation_id"] == first["observation_id"]


def test_anpr_accepts_single_non_indian_in_range_read():
    stabilizer = OcrStabilizer()
    stabilizer.observe("cam", 7, "GARAGE-12", 0.26, 1, plate_width=80)
    assert stabilizer.confirmed_text("cam", 7) == "GARAGE12"
    assert stabilizer.confirmed_confidence("cam", 7) == pytest.approx(0.26)


def test_anpr_rejects_below_range_read_but_keeps_provisional():
    stabilizer = OcrStabilizer()
    stabilizer.observe("cam", 7, "GARAGE-12", 0.12, 1, plate_width=80)
    assert stabilizer.confirmed_text("cam", 7) is None
    assert stabilizer.text_for("cam", 7) == "GARAGE12"


def test_anpr_improved_confidence_supersedes_same_plate(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    pipeline = V5EventPipeline("cam", config([binding("plate", "anpr")]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    low = packet(3, [("car", 1, (20, 20, 70, 75))])
    low.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                          confidence=.8,
                                          metadata={"ocr_text": "KA01AB1234", "ocr_confidence": .30},
                                          parent_id=1))
    high = packet(4, [("car", 1, (20, 20, 70, 75))])
    high.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                           confidence=.8,
                                           metadata={"ocr_text": "KA01AB1234", "ocr_confidence": .45},
                                           parent_id=1))
    pipeline.process(low)
    clock.value += 2.1
    pipeline.process(low)
    clock.value += 6
    pipeline.process(high)
    reads = sorted((item for item in events(tmp_path) if item["event_type"] == "plate_read"),
                   key=lambda item: item["confidence"])
    assert [item["confidence"] for item in reads] == [.30, .45]
    assert reads[1]["supersedes_observation_id"] == reads[0]["observation_id"]


def test_anpr_suppresses_noisy_alternate_plate_for_same_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(v5_events, "time", clock)
    pipeline = V5EventPipeline("cam", config([binding("plate", "anpr")]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 9, (20, 20, 70, 75))]))
        clock.value += .1
    good = packet(3, [("car", 9, (20, 20, 70, 75))])
    good.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                           confidence=.9,
                                           metadata={"ocr_text": "KA03NP4277", "ocr_confidence": .95},
                                           parent_id=9))
    pipeline.process(good)
    for index, (text, confidence) in enumerate([
        ("KA03BP4277", .91),
        ("KA1TBB4377", .73),
        ("AP1BBD4137", .51),
        ("AP22A8777", .47),
    ], start=4):
        clock.value += 6
        noisy = packet(index, [("car", 9, (20, 20, 70, 75))])
        noisy.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                                confidence=.9,
                                                metadata={"ocr_text": text, "ocr_confidence": confidence},
                                                parent_id=9))
        pipeline.process(noisy)
    reads = [item for item in events(tmp_path) if item["event_type"] == "plate_read"]
    assert [item["plate_text"] for item in reads] == ["KA03NP4277"]


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
    data = {"contract": "sentry-v6", "schema_version": "5.2", "edge_id": "edge",
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


def test_v6_ready_state_reports_camera_retrying_until_health_event(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("ANALYTICS_EVENT_LOG_PATH", str(events_path))
    snap = {"plan": {"cameras": [{"camera_id": "cam-a"}, {"camera_id": "cam-b"}]}}
    assert _per_camera_state(snap) == [
        {"camera_id": "cam-a", "state": "degraded", "reason": "worker_starting",
         "last_reliable_at": None, "as_of": None, "retrying": True},
        {"camera_id": "cam-b", "state": "degraded", "reason": "worker_starting",
         "last_reliable_at": None, "as_of": None, "retrying": True},
    ]
    events_path.write_text(json.dumps({
        "event_type": "camera_health", "camera_id": "cam-a", "state": "healthy",
        "reason": "none", "as_of": "2026-10-09T10:00:00Z",
        "last_reliable_at": "2026-10-09T10:00:00Z",
    }) + "\n", encoding="utf-8")
    states = {item["camera_id"]: item for item in _per_camera_state(snap)}
    assert states["cam-a"]["state"] == "healthy"
    assert states["cam-a"]["retrying"] is False
    assert states["cam-b"]["reason"] == "worker_starting"


def test_v6_ready_state_reads_camera_health_from_v5_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    snap = {"plan": {"cameras": [{"camera_id": "cam"}]}}
    outbox = tmp_path / "sentinel_v5_2" / "outbox" / "cam"
    outbox.mkdir(parents=True)
    (outbox / "health.json").write_text(json.dumps({"observation": {
        "event_type": "camera_health", "camera_id": "cam", "state": "healthy",
        "reason": "none", "observed_at": "2026-10-09T10:00:00Z",
        "as_of": "2026-10-09T10:00:00Z",
        "last_reliable_at": "2026-10-09T10:00:00Z",
    }}), encoding="utf-8")
    assert _per_camera_state(snap) == [
        {"camera_id": "cam", "state": "healthy", "reason": "none",
         "last_reliable_at": "2026-10-09T10:00:00Z",
         "as_of": "2026-10-09T10:00:00Z", "retrying": False}
    ]
