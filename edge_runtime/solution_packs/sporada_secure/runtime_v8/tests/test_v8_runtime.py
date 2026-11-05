from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_policy
from collections import deque
import json
import multiprocessing as mp
from pathlib import Path
import socketserver
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "worker"))

from pipeline.sentinel_v8_handoff import (  # noqa: E402
    PRIORITY_CRITICAL,
    PRIORITY_DURABLE,
    PRIORITY_IDENTITY,
    PRIORITY_REPLACEABLE,
    V8HandoffClient,
    V8HandoffScheduler,
    canonical_json_bytes,
    create_handoff_channels,
    package_digest,
)
from traffic_pilot_runtime.desired_state import (  # noqa: E402
    DEFAULT_CAMERA_FPS,
    DesiredCamera,
    DesiredState,
    DesiredStateValidator,
)
from traffic_pilot_runtime.graph import compile_runtime_plan  # noqa: E402
from pipeline.runtime_telemetry import CameraTelemetry  # noqa: E402
from pipeline.adaptive_policy import AdaptivePolicyResolver  # noqa: E402
from detectors.backends.openvino_ocr_async import resolve_ocr_queue_depth  # noqa: E402
from detectors.backends.openvino_detector import OpenVINOYOLODetector  # noqa: E402
from pipeline.shared_vehicle_inference import (  # noqa: E402
    SharedVehicleInferenceScheduler,
    resolve_batch_settings,
)
from pipeline.scene_embeddings import (  # noqa: E402
    normalize_scene_embedding,
    preprocess_scene_image,
)
from pipeline.shared_scene_inference import (  # noqa: E402
    SharedSceneExtractorClient,
    SharedSceneInferenceScheduler,
)
from pipeline.surface_stream import (  # noqa: E402
    SourcePtsClock,
    SourcePtsSampler,
    _probe_source,
    dlstreamer_model_instance_id,
)
from pipeline.dlstreamer_bridge import GvaTrackerMetadataStage  # noqa: E402
import pipeline.dls_secondary as dls_secondary  # noqa: E402
from pipeline.dls_secondary import secondary_model_instance_id  # noqa: E402
from pipeline.types import Detection, FramePacket  # noqa: E402
from pipeline.v8_events import V8EventPipeline  # noqa: E402
from pipeline.zero_copy_shadow import (  # noqa: E402
    ShadowDetection,
    ZeroCopyShadowComparator,
    compare_detection_sets,
)


def test_gvatrack_metadata_stage_preserves_authoritative_ids_without_reassociation():
    stage = GvaTrackerMetadataStage(target_fps=5)
    first = FramePacket(
        index=10,
        name="cam-1",
        frame=np.zeros((100, 200, 3), dtype=np.uint8),
        detections=[Detection(
            bbox=[10, 20, 40, 80], class_id=7, class_name="pedestrian",
            confidence=0.8, model_name="vehicle",
            metadata={"track_id": 91, "predicted": False,
                      "tracker_backend": "gvatrack"},
        )],
    )
    stage.process([first])
    assert first.detections[0].metadata["track_id"] == 91
    assert first.detections[0].metadata["track_hits"] == 1
    assert first.analytics_state["adaptive_tracker_observation"] == {
        "detector_evaluated": True,
        "active_tracks": 1,
        "matched_tracks": 1,
        "predicted_tracks": 0,
        "retained_tracker_memory": 1,
        "backend": "gvatrack",
    }

    second = FramePacket(
        index=13,
        name="cam-1",
        frame=first.frame,
        detections=[Detection(
            bbox=[14, 20, 44, 80], class_id=7, class_name="pedestrian",
            confidence=0.82, model_name="vehicle",
            metadata={"track_id": 91, "predicted": False,
                      "tracker_backend": "gvatrack"},
        )],
    )
    stage.process([second])
    assert second.detections[0].metadata["track_id"] == 91
    assert second.detections[0].metadata["track_hits"] == 2
    assert second.detections[0].metadata["track_age"] == 4


def test_adaptive_policy_geometry_only_frame_does_not_consume_quality_sample():
    resolver = AdaptivePolicyResolver("cam-1", target_fps=8)
    geometry_only = np.empty((720, 1280, 0), dtype=np.uint8)
    assert resolver.image_sample_due(100.0)
    resolver.observe_frame(geometry_only, [], 100.0)
    assert resolver.image_sample_due(100.0)
    resolver.observe_frame(np.zeros((720, 1280, 3), dtype=np.uint8), [], 100.0)
    assert not resolver.image_sample_due(100.5)
    assert resolver.image_sample_due(101.0)


def test_v8_primary_pixel_admission_preserves_policy_and_scene_requirements():
    pipeline = V8EventPipeline.__new__(V8EventPipeline)
    pipeline.scene = None
    pipeline.policy = SimpleNamespace(image_sample_due=lambda now: False)
    pipeline.bindings = []
    pipeline.last_scene = {}
    assert not pipeline.primary_pixels_required(100.0, [])
    detection = Detection(
        bbox=[1, 2, 20, 40], class_id=7, class_name="pedestrian",
        confidence=0.8, model_name="vehicle",
    )
    assert pipeline.primary_pixels_required(100.0, [detection])
    pipeline.policy = SimpleNamespace(image_sample_due=lambda now: True)
    assert pipeline.primary_pixels_required(100.0, [])


def test_dlstreamer_secondary_models_have_isolated_instance_ids():
    names = ["plate-detector", "plate-ocr", "body-reid", "gaitbase-montage", "scene-siglip"]
    identifiers = [secondary_model_instance_id(name) for name in names]
    assert len(set(identifiers)) == len(names)
    assert all(identifier.startswith("sentinel-") for identifier in identifiers)


def test_dlstreamer_secondary_executor_is_shared_and_reference_counted(monkeypatch):
    created = []

    class FakeEngine:
        actual_device = "NPU/DLStreamer"

        def __init__(self, *args, **kwargs):
            self.closed = 0
            created.append(self)

        def infer(self, image):
            return image

        def close(self):
            self.closed += 1

    dls_secondary._ENGINE_REGISTRY.clear()
    monkeypatch.setattr(dls_secondary, "DlsRawImageInferencer", FakeEngine)
    arguments = dict(
        model_path="/tmp/model.xml",
        model_proc="/tmp/model-proc.json",
        width=128,
        height=64,
        device="NPU",
        name="ocr",
        expected_elements=32,
    )
    first = dls_secondary.acquire_dls_inferencer(**arguments)
    second = dls_secondary.acquire_dls_inferencer(**arguments)
    assert len(created) == 1
    first.close()
    assert created[0].closed == 0
    second.close()
    assert created[0].closed == 1
    assert not dls_secondary._ENGINE_REGISTRY


def test_dlstreamer_secondary_scheduler_round_robins_camera_owners():
    engine = dls_secondary.DlsRawImageInferencer.__new__(
        dls_secondary.DlsRawImageInferencer
    )
    engine._condition = threading.Condition()
    first_a = dls_secondary._InferenceJob("camera-a", np.zeros((1, 1, 3)))
    second_a = dls_secondary._InferenceJob("camera-a", np.zeros((1, 1, 3)))
    first_b = dls_secondary._InferenceJob("camera-b", np.zeros((1, 1, 3)))
    engine._owner_queues = {
        "camera-a": deque([first_a, second_a]),
        "camera-b": deque([first_b]),
    }
    engine._owner_order = deque(["camera-a", "camera-b"])
    engine._closed = False

    assert engine._take_job() is first_a
    assert engine._take_job() is first_b
    assert engine._take_job() is second_a
from stream_fleet_openvino import (  # noqa: E402
    SharedVehicleDetectorClient,
    camera_groups,
)


def test_dlstreamer_model_instances_share_only_inside_explicit_process_scope(
    monkeypatch,
):
    monkeypatch.delenv("SENTINEL_DLSTREAMER_MODEL_SHARE_SCOPE", raising=False)
    assert dlstreamer_model_instance_id("vehicle", "cam-1") != (
        dlstreamer_model_instance_id("vehicle", "cam-2")
    )
    monkeypatch.setenv("SENTINEL_DLSTREAMER_MODEL_SHARE_SCOPE", "group-3")
    assert dlstreamer_model_instance_id("vehicle", "cam-1") == (
        dlstreamer_model_instance_id("vehicle", "cam-2")
    )
    assert dlstreamer_model_instance_id("vehicle", "cam-1") != (
        dlstreamer_model_instance_id("fire", "cam-1")
    )


def test_shared_dlstreamer_groups_are_bounded_and_keep_every_camera():
    cameras = [{"name": f"cam-{index}"} for index in range(12)]
    groups = camera_groups(cameras, 5)
    assert [len(group) for group in groups] == [5, 5, 2]
    assert [camera["name"] for group in groups for camera in group] == [
        camera["name"] for camera in cameras
    ]


def test_v8_desired_state_uses_per_camera_fps_and_default_without_fixed_aggregate_cap(
    tmp_path, monkeypatch,
):
    cameras = []
    for index in range(50):
        number = index + 1
        cameras.append({
            "camera_id": f"camera-{number:02d}",
            "source": f"file:/run/secrets/sentinel/cameras/camera-{number:02d}.url",
            "bindings": [{
                "use_case_id": f"count-{number:02d}",
                "app": "vehicle_counting",
                "scope": "full_frame",
                "geometry_ids": [],
                "object_types": ["vehicle"],
            }],
        })
    payload = {
        "contract": "sentry-v8",
        "schema_version": "8.0",
        "edge_id": "capacity-default-fps",
        "deployment_id": "capacity-default-fps",
        "revision": 1,
        "cameras": cameras,
    }
    validator = DesiredStateValidator(tmp_path)
    monkeypatch.setattr(validator, "_validate_source", lambda *_: None)
    validator.validate(payload)
    resolved = [validator._camera(camera, payload).fps for camera in cameras]
    assert resolved == [DEFAULT_CAMERA_FPS] * 50
    assert sum(resolved) == 400.0


def test_runtime_plan_preserves_resolved_camera_fps():
    desired = DesiredState(
        edge_id="fps-plan", deployment_id="fps-plan", revision=1,
        content_hash="test", cameras=(DesiredCamera(
            camera_id="cam-1", source="file:/run/secrets/sentinel/cameras/cam-1.url",
            apps=("v8_events",), fps=12.5,
        ),),
    )
    plan = compile_runtime_plan(desired).to_dict()
    assert plan["cameras"][0]["fps"] == 12.5


def _observation(event_type: str = "object_present", **values):
    payload = {
        "schema_version": "7.0",
        "observation_id": str(uuid.uuid4()),
        "deployment_id": "test-deployment",
        "camera_id": "cam-1",
        "config_revision": 1,
        "stream_session_id": str(uuid.uuid4()),
        "use_case_id": "presence-1",
        "event_type": event_type,
        "observed_at": "2026-11-02T08:00:00Z",
        "source_frame_id": "cam-1:1",
        "evidence_ids": [],
        "embedding_ids": [],
    }
    payload.update(values)
    return payload


def test_ocr_queue_depth_is_safe_and_bounded(monkeypatch):
    monkeypatch.delenv("OCR_QUEUE_DEPTH", raising=False)
    assert resolve_ocr_queue_depth() == 4
    assert resolve_ocr_queue_depth("1") == 1
    assert resolve_ocr_queue_depth("6") == 6
    assert resolve_ocr_queue_depth("0") == 1
    assert resolve_ocr_queue_depth("16") == 8


def test_vehicle_batch_settings_are_bounded_and_batch_one_is_compatible():
    assert resolve_batch_settings(1, 0.008) == (1, 0.008)
    assert resolve_batch_settings(0, -1) == (1, 0.0)
    assert resolve_batch_settings(99, 1) == (4, 0.015)
    assert __import__("inspect").signature(
        OpenVINOYOLODetector
    ).parameters["batch_size"].default == 1


def test_source_pts_clock_keeps_event_time_at_decoder_ingress():
    clock = SourcePtsClock()
    assert clock.observe(10_000, 100.0, clock_hz=1_000.0) == 100.0
    # Five seconds of downstream delay must not move the media observation by
    # more than the one second represented by the PTS progression.
    assert clock.observe(11_000, 105.0, clock_hz=1_000.0) == 101.0
    # A backwards jump is a new media epoch (sender restart/file loop).
    assert clock.observe(100, 200.0, clock_hz=1_000.0) == 200.0


def test_source_pts_sampler_preserves_configured_media_cadence():
    sampler = SourcePtsSampler(5, clock_hz=1_000)
    accepted = [pts for pts in range(0, 1_001, 40) if sampler.accept(pts)]
    assert accepted == [0, 200, 400, 600, 800, 1_000]


def test_source_pts_sampler_resets_after_sender_pts_restart():
    sampler = SourcePtsSampler(5, clock_hz=1_000)
    assert sampler.accept(1_000)
    assert not sampler.accept(1_040)
    assert sampler.accept(0)


def test_tcp_surface_source_uses_contract_codec_without_second_connection():
    assert _probe_source("tcp://192.0.2.10:9001") == "h264"


def test_zero_copy_shadow_comparison_is_class_aware_and_non_mutating():
    authoritative = [
        SimpleNamespace(
            bbox=[10, 10, 110, 110], class_id=1, class_name="car",
            confidence=0.91,
        ),
        SimpleNamespace(
            bbox=[200, 20, 250, 180], class_id=7, class_name="pedestrian",
            confidence=0.80,
        ),
    ]
    shadow = [
        ShadowDetection((12, 12, 108, 108), 1, "car", 0.88),
        # Strong overlap but wrong class must not be accepted as the person.
        ShadowDetection((201, 21, 249, 179), 5, "truck", 0.79),
    ]
    original_boxes = [list(item.bbox) for item in authoritative]
    result = compare_detection_sets(authoritative, shadow)
    assert result["matched"] == 1
    assert result["missing"] == 1
    assert result["extra"] == 1
    assert result["mean_iou"] > 0.9
    assert result["mean_confidence_delta"] == pytest.approx(0.03)
    assert [item.bbox for item in authoritative] == original_boxes


def test_zero_copy_shadow_correlates_by_pts_in_either_arrival_order():
    comparator = ZeroCopyShadowComparator("cam-1", capacity=8)
    car = ShadowDetection((1, 2, 11, 22), 1, "car", 0.75)
    comparator.submit_shadow(200, [car])
    comparator.submit_authoritative(100, [car])
    assert comparator.snapshot()["frames_compared"] == 0
    comparator.submit_authoritative(200, [car])
    comparator.submit_shadow(100, [car])
    snapshot = comparator.snapshot()
    assert snapshot["frames_compared"] == 2
    assert snapshot["matched"] == 2
    assert snapshot["missing"] == 0
    assert snapshot["extra"] == 0


def test_shared_vehicle_detector_keeps_source_coordinate_contract():
    class Transport:
        def infer(self, canvas):
            assert canvas.shape == (640, 640, 3)
            output = np.zeros((1, 2, 6), np.float32)
            output[0, 0] = [32, 192, 320, 480, 0.9, 1]
            return {"output": output, "error": None}

        def reset(self):
            return None

    detector = SharedVehicleDetectorClient.__new__(SharedVehicleDetectorClient)
    detector._transport = Transport()
    detector.confidence = 0.3
    detector.exec_devices = "GPU.0"
    detections = detector.detect(np.zeros((100, 200, 3), np.uint8))
    assert len(detections) == 1
    assert detections[0].class_name == "car"
    assert detections[0].bbox == [10, 10, 100, 100]


def test_dynamic_batch_completion_demultiplexes_without_cross_camera_leakage():
    class Tensor:
        data = np.stack([
            np.full((3, 6), 11, np.float32),
            np.full((3, 6), 22, np.float32),
        ])

    class Request:
        def get_output_tensor(self):
            return Tensor()

    scheduler = SharedVehicleInferenceScheduler.__new__(SharedVehicleInferenceScheduler)
    scheduler.completed = 0
    scheduler.completed_batches = 0
    scheduler.batch_size_counts = {1: 0, 2: 0}
    scheduler.rejected = 0
    responses = {}
    scheduler._put_response = lambda camera_id, response: responses.setdefault(
        camera_id, response
    )
    collection_started = 20.0
    inference_started = 20.008
    batch = [
        ("cam-a", {"job_id": "session-a:1", "submitted_monotonic": 19.995}),
        ("cam-b", {"job_id": "session-b:7", "submitted_monotonic": 20.004}),
    ]
    scheduler._complete(
        Request(),
        (batch, collection_started, inference_started,
         np.zeros((2, 640, 640, 3), np.uint8), []),
    )
    assert set(responses) == {"cam-a", "cam-b"}
    assert responses["cam-a"]["job_id"] == "session-a:1"
    assert responses["cam-b"]["job_id"] == "session-b:7"
    assert responses["cam-a"]["output"].shape == (1, 3, 6)
    assert responses["cam-b"]["output"].shape == (1, 3, 6)
    assert np.all(responses["cam-a"]["output"] == 11)
    assert np.all(responses["cam-b"]["output"] == 22)
    assert responses["cam-a"]["scheduler_queue_wait_seconds"] == pytest.approx(0.005)
    assert responses["cam-b"]["scheduler_queue_wait_seconds"] == 0.0
    assert responses["cam-a"]["batch_collection_seconds"] == pytest.approx(0.008)
    assert responses["cam-b"]["batch_collection_seconds"] == pytest.approx(0.004)
    assert responses["cam-a"]["batch_size"] == 2
    assert scheduler.completed == 2
    assert scheduler.completed_batches == 1
    assert scheduler.batch_size_counts[2] == 1


def test_batch_one_uses_historical_mmap_view_without_an_extra_copy():
    scheduler = SharedVehicleInferenceScheduler.__new__(SharedVehicleInferenceScheduler)
    scheduler.max_batch_size = 1
    slot_lock = threading.Lock()
    source = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
    scheduler.channels = {"cam-a": SimpleNamespace(slot_lock=slot_lock)}
    scheduler._slots = {"cam-a": SimpleNamespace(array=source)}
    input_view, held_locks = scheduler._assemble_batch([
        ("cam-a", {"job_id": "session:1"})
    ])
    assert input_view.shape == (1, 4, 5, 3)
    assert np.shares_memory(input_view, source)
    assert not slot_lock.acquire(blocking=False)
    for held_lock in held_locks:
        held_lock.release()
    assert slot_lock.acquire(blocking=False)
    slot_lock.release()


def test_scene_preprocessing_preserves_the_canonical_siglip2_contract():
    image = np.zeros((300, 500, 3), np.uint8)
    image[:, :, 0] = 255
    tensor = preprocess_scene_image(image)
    assert tensor.shape == (1, 3, 224, 224)
    assert tensor.dtype == np.float32
    assert tensor.flags.c_contiguous
    # BGR blue becomes RGB channel 2 after preprocessing and maps to +1;
    # the other two channels map to -1.
    assert np.all(tensor[0, 0] == -1.0)
    assert np.all(tensor[0, 1] == -1.0)
    assert np.all(tensor[0, 2] == 1.0)


def test_shared_scene_client_keeps_embedding_normalization_contract():
    raw = np.arange(1, 769, dtype=np.float32)

    class Transport:
        def infer(self, tensor):
            assert tensor.shape == (1, 3, 224, 224)
            return raw.copy()

    client = SharedSceneExtractorClient.__new__(SharedSceneExtractorClient)
    client.transport = Transport()
    vector = client.embed(np.zeros((480, 640, 3), np.uint8))
    assert vector.shape == (768,)
    assert vector.dtype == np.float32
    assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-6)
    assert np.array_equal(vector, normalize_scene_embedding(raw))


def test_shared_scene_completion_returns_only_to_own_camera_and_releases_slot():
    class Tensor:
        data = np.arange(768, dtype=np.float32)[None, :]

    class Request:
        def get_output_tensor(self):
            return Tensor()

    scheduler = SharedSceneInferenceScheduler.__new__(SharedSceneInferenceScheduler)
    scheduler.completed = 0
    scheduler.rejected = 0
    responses = {}
    scheduler._put_response = lambda camera_id, response: responses.setdefault(
        camera_id, response
    )
    lock = threading.Lock()
    lock.acquire()
    job = {"job_id": "scene-session:4", "submitted_monotonic": time.monotonic()}
    scheduler._complete(
        Request(),
        ("cam-b", job, time.monotonic(),
         np.zeros((1, 3, 224, 224), np.float32), lock),
    )
    assert set(responses) == {"cam-b"}
    assert responses["cam-b"]["job_id"] == "scene-session:4"
    assert responses["cam-b"]["output"].shape == (1, 768)
    assert responses["cam-b"]["error"] is None
    assert scheduler.completed == 1
    assert lock.acquire(blocking=False)
    lock.release()


def test_delayed_face_candidate_retains_only_original_pixel_crop():
    pipeline = V8EventPipeline.__new__(V8EventPipeline)
    pipeline.face_candidates = {}
    pipeline.face_selection_window = 2.0
    frame = np.arange(100 * 120 * 3, dtype=np.uint8).reshape(100, 120, 3)
    packet = SimpleNamespace(frame=frame)
    state = {"id": "presence-1", "track_id": 7, "last_feature": {}}
    binding = {"use_case_id": "identity-1"}
    pipeline._offer_face_candidate(
        packet, state, binding, np.ones(512, np.float32), 0.8,
        [40, 30, 60, 50], 1000.25, "camera01:22",
    )
    candidate = pipeline.face_candidates["presence-1"]
    x1, y1, x2, y2 = candidate["bounds"]
    expected = frame[y1:y2, x1:x2].copy()
    assert np.array_equal(candidate["image"], expected)
    assert not np.shares_memory(candidate["image"], frame)
    assert candidate["captured_at"] == 1000.25
    assert candidate["frame_id"] == "camera01:22"
    frame[y1:y2, x1:x2] = 0
    assert np.array_equal(candidate["image"], expected)


def test_anpr_candidate_reuses_one_camera_local_original_frame_snapshot():
    class Policy:
        def observe_plate(self, **values):
            self.values = values

    pipeline = V8EventPipeline.__new__(V8EventPipeline)
    pipeline.policy = Policy()
    frame = np.full((80, 160, 3), 127, np.uint8)
    snapshot = frame.copy()
    packet = SimpleNamespace(frame=frame)
    plate = SimpleNamespace(bbox=[30, 40, 100, 60])
    candidate = pipeline._plate_candidate(
        packet, plate, "KA03NX3787", 0.91, {"parking"},
        2000.5, "camera01:31", frame_snapshot=snapshot,
    )
    assert candidate["frame"] is snapshot
    assert candidate["frame_id"] == "camera01:31"
    assert candidate["captured_at"] == 2000.5
    assert candidate["containing"] == {"parking"}
    frame[:] = 0
    assert np.all(candidate["frame"] == 127)


def test_scene_policy_keeps_crop_timestamp_evidence_and_package_camera_local():
    class Scene:
        calls = 0

        def embed(self, image):
            self.calls += 1
            self.input = image.copy()
            vector = np.ones(768, np.float32)
            return vector / np.linalg.norm(vector)

    class Store:
        def persist(self, observation, evidence, embeddings):
            self.package = (observation, evidence, embeddings)
            return True

    pipeline = V8EventPipeline.__new__(V8EventPipeline)
    pipeline.camera_id = "camera01"
    pipeline.config = {"deployment_id": "deployment-1", "config_revision": 9}
    pipeline.session_id = "session-1"
    pipeline.scene = Scene()
    pipeline.telemetry = None
    pipeline.zones = {
        "parking": {"poly": [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]}
    }
    pipeline.last_scene = {}
    pipeline.store = Store()
    pipeline._scene_capacity = lambda now: True
    frame = np.arange(80 * 120 * 3, dtype=np.uint8).reshape(80, 120, 3)
    packet = SimpleNamespace(frame=frame)
    binding = {
        "app": "scene_search",
        "use_case_id": "scene-parking",
        "scope": "geometry",
        "geometry_ids": ["parking"],
    }
    pipeline.bindings = [binding, dict(binding, use_case_id="scene-parking-other")]
    observed_at = 2000.125
    pipeline._scene(
        packet, binding, "parking", observed_at, "camera01:44", "interval"
    )
    observation, evidence, embeddings = pipeline.store.package
    assert np.array_equal(pipeline.scene.input, frame[20:60, 30:90])
    assert observation["observed_at"] == "1970-01-01T00:33:20.125000Z"
    assert observation["source_frame_id"] == "camera01:44"
    assert observation["camera_id"] == "camera01"
    assert observation["zone_id"] == "parking"
    assert observation["trigger"] == "interval"
    assert len(evidence) == 1 and len(embeddings) == 1
    metadata, jpeg = evidence[0]
    assert metadata["captured_at"] == observation["observed_at"]
    assert metadata["source_frame_id"] == observation["source_frame_id"]
    assert metadata["evidence_type"] == "scene_region_crop"
    assert metadata["source_bbox_normalized"] == {
        "x1": 0.25, "y1": 0.25, "x2": 0.75, "y2": 0.75,
    }
    assert jpeg.startswith(b"\xff\xd8")
    assert embeddings[0]["evidence_id"] == metadata["evidence_id"]
    assert embeddings[0]["profile_id"] == "siglip2-base-v1"
    assert embeddings[0]["source_frame_id"] == "camera01:44"

    # Another binding must still emit its own complete linked package, without
    # re-running the identical same-frame model/encoding work.
    second_binding = dict(binding, use_case_id="scene-parking-other")
    pipeline._scene(packet, second_binding, "parking", observed_at,
                    "camera01:44", "event", "trigger-event")
    other, other_evidence, other_embeddings = pipeline.store.package
    assert pipeline.scene.calls == 1
    assert other["observation_id"] != observation["observation_id"]
    assert other["use_case_id"] == "scene-parking-other"
    assert other["triggering_observation_id"] == "trigger-event"
    assert other_evidence[0][0]["evidence_id"] != metadata["evidence_id"]
    assert other_evidence[0][1] == jpeg
    assert other_embeddings[0]["embedding_id"] != embeddings[0]["embedding_id"]
    assert other_embeddings[0]["evidence_id"] == other_evidence[0][0]["evidence_id"]
    assert other_embeddings[0]["vector"] == embeddings[0]["vector"]
    pipeline._scene(packet, second_binding, "parking", observed_at + 2,
                    "camera01:45", "interval")
    assert pipeline.scene.calls == 2


class _AgentHandler(socketserver.StreamRequestHandler):
    received = []
    next_sequence = 1

    def handle(self):
        request_line = self.rfile.readline().decode("ascii").strip()
        assert request_line == "POST /v1/packages HTTP/1.1"
        headers = {}
        while True:
            line = self.rfile.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            key, value = line.decode("latin-1").split(":", 1)
            headers[key.lower()] = value.strip()
        body = self.rfile.read(int(headers["content-length"]))
        message = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + body
        )
        parts = {part.get_param("name", header="content-disposition"):
                 part.get_payload(decode=True) for part in message.iter_parts()}
        manifest = json.loads(parts.pop("manifest"))
        assert set(parts) == {item["part_name"] for item in manifest["evidence"]}
        self.__class__.received.append((manifest, parts))
        rows = [("observation", manifest["observation"]["observation_id"])]
        rows.extend(("evidence", item["metadata"]["evidence_id"])
                    for item in manifest["evidence"])
        rows.extend(("embedding", item["embedding_id"])
                    for item in manifest["embeddings"])
        first = self.__class__.next_sequence
        self.__class__.next_sequence += len(rows)
        response = {
            "status": "durable",
            "package_id": manifest["package_id"],
            "deployment_id": manifest["deployment_id"],
            "sequences": [
                {"sequence": first + index, "object_type": kind, "object_id": identifier}
                for index, (kind, identifier) in enumerate(rows)
            ],
        }
        encoded = canonical_json_bytes(response)
        self.wfile.write(
            b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode()
            + encoded
        )


def test_digest_is_stable_and_sensitive_to_evidence_order():
    manifest = {"z": 1, "a": "value"}
    expected = package_digest(manifest, [("evidence_0", b"one"), ("evidence_1", b"two")])
    assert expected == package_digest(
        {"a": "value", "z": 1}, [("evidence_0", b"one"), ("evidence_1", b"two")]
    )
    assert expected != package_digest(
        manifest, [("evidence_1", b"two"), ("evidence_0", b"one")]
    )


def test_priority_is_cv_policy_not_desired_state():
    parent, child = mp.Pipe(duplex=True)
    client = V8HandoffClient(child, "test-deployment", "cam-1")
    assert client._priority(_observation("fire_smoke_suspected")) == (PRIORITY_CRITICAL, False)
    assert client._priority(_observation("zone_entry")) == (PRIORITY_DURABLE, False)
    assert client._priority(_observation("person_feature_sample")) == (PRIORITY_IDENTITY, False)
    assert client._priority(_observation("scene_sample", trigger="interval")) == (
        PRIORITY_REPLACEABLE, True
    )
    client.close()
    parent.close()


def test_complete_package_reaches_agent_over_unix_socket(tmp_path):
    socket_path = tmp_path / "agent.sock"
    status_path = tmp_path / "handoff.json"
    _AgentHandler.received = []
    _AgentHandler.next_sequence = 1
    server = socketserver.UnixStreamServer(str(socket_path), _AgentHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    context = mp.get_context("spawn")
    parents, children = create_handoff_channels(context, ["cam-1"])
    scheduler = V8HandoffScheduler(parents, str(socket_path), status_path).start()
    client = V8HandoffClient(children["cam-1"], "test-deployment", "cam-1")
    image = b"jpeg-evidence"
    evidence_id = str(uuid.uuid4())
    observation = _observation()
    observation["evidence_ids"] = [evidence_id]
    evidence = {
        "schema_version": "7.0",
        "evidence_id": evidence_id,
        "observation_id": observation["observation_id"],
        "evidence_type": "frame",
        "captured_at": observation["observed_at"],
        "source_frame_id": observation["source_frame_id"],
        "source_bbox_normalized": None,
        "content_type": "image/jpeg",
        "width": 4,
        "height": 4,
        "size_bytes": len(image),
        "sha256": "sha256:" + __import__("hashlib").sha256(image).hexdigest(),
    }
    assert client.persist(observation, [(evidence, image)])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not _AgentHandler.received:
        time.sleep(0.01)
    assert len(_AgentHandler.received) == 1
    manifest, parts = _AgentHandler.received[0]
    assert manifest["observation"] == observation
    assert parts == {"evidence_0": image}
    deadline = time.monotonic() + 3
    status = {}
    while time.monotonic() < deadline:
        status = json.loads(status_path.read_text())
        if status["counters"]["durable"] == 1:
            break
        time.sleep(0.01)
    assert status["retained_packages"] == 0
    assert status["counters"]["durable"] == 1
    client.close()
    scheduler.close()
    server.shutdown()
    server.server_close()


def test_v8_desired_state_and_plan_have_no_cv_outbox(tmp_path):
    example = json.loads((ROOT / "image_schema" / "desired-state.example.json").read_text())
    source = example["cameras"][0]
    desired = DesiredState(
        edge_id=example["edge_id"], deployment_id=example["deployment_id"],
        revision=example["revision"], content_hash="test",
        cameras=(DesiredCamera(
            camera_id=source["camera_id"], source=source["source"], fps=source["fps"],
            apps=("v8_events", "face_recognition", "person_reid", "scene_embeddings"),
            config={"v8": {
                "deployment_id": example["deployment_id"],
                "config_revision": example["revision"],
                "bindings": source["bindings"], "geometry": source["geometry"],
            }},
        ),),
    )
    plan = compile_runtime_plan(desired).to_dict()
    node_ids = {node for camera in plan["cameras"] for node in camera["node_ids"]}
    assert "v8_handoff_scheduler" in node_ids
    assert "delivery_agent_socket" in node_ids
    assert not any("outbox" in node or "uploader" in node for node in node_ids)


def test_v8_output_telemetry_reports_handoff_without_claiming_durability(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    telemetry = CameraTelemetry("cam-1", target_fps=8, devices={})
    event = _observation("scene_sample", trigger="interval")
    telemetry.event_handoff_admitted(event, 0.01)
    snapshot = telemetry.flush(force=True)
    assert snapshot["processing"]["totals"]["events_handoff_admitted"] == 1
    assert "events_persisted" not in snapshot["processing"]["totals"]
    assert snapshot["outputs"]["by_event_type"]["scene_sample"]["total"] == 1
    assert snapshot["processing"]["output_events_per_second"] > 0
    telemetry.close()
