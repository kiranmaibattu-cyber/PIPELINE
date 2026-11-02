"""All-OpenVINO fleet worker.

Runs the full ALPR cascade — vehicle detect -> plate detect -> OCR — entirely on
OpenVINO (Intel iGPU/NPU/CPU). It also runs the fire/smoke detector as a side
OpenVINO model and feeds all detections into the existing analytics/output path.

Architecture: ISOLATED CAMERA STATE + SHARED INFERENCE — one process per camera
owns decode, tracking, policy, evidence and delivery.  The mandatory
vehicle/person detector is compiled once in the fleet parent and served through a
fair OpenVINO AsyncInferQueue.  Each camera writes its 640x640 model canvas to a
private mmap slot, so image pixels are not serialized through a manager proxy.
This preserves camera fault isolation and all V7 event semantics while keeping
multiple GPU requests in flight across cameras.

Why processes, not threads: the per-frame consume work (YOLO postprocess, tracking,
analytics, payload build) is CPU-bound Python. The GIL serializes it, so a thread-
per-camera design saturates ~1 core of Python work and stalls far below target
(measured: 4 cams capped ~7 fps/cam with cores only ~45% busy — the classic GIL
wall). Processes give each camera its own interpreter. This is the same reason the
OpenVINO has no single-cascade-stream limit, so there is no dispatcher funnel and
no crop IPC. Each process is fully independent.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

from pipeline.motion_gate import MotionGateController
from pipeline.camera_state import publish_camera_state
from pipeline.runtime_status import (
    increment_supervisor_status,
    read_decoder_status,
)
from pipeline.structured_logging import (
    configure_structured_logging,
    log_event,
    set_log_context,
)

configure_structured_logging()
log = logging.getLogger("stream_fleet_openvino")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAMERAS_FILE = os.getenv("CAMERAS_FILE", f"{REPO}/config/cameras.json")
INFER_FPS = int(os.getenv("INFER_FPS", "12"))
OPENVINO_MODELS_DIR = os.getenv("OPENVINO_MODELS_DIR", f"{REPO}/models/openvino")
FACE_MODELS_DIR = os.getenv("FACE_MODELS_DIR", "/models/face/openvino")
SCENE_MODEL_PATH = os.getenv(
    "SCENE_MODEL_PATH", "/models/scene/openvino/siglip2-base-patch16-224.xml"
)
SCENE_EMBEDDER_DEVICE = os.getenv("SCENE_EMBEDDER_DEVICE", "GPU")
REID_MODELS_DIR = os.getenv("REID_MODELS_DIR", "/models/surveillance")
BODY_REID_DEVICE = os.getenv("BODY_REID_DEVICE", "NPU")
GAIT_DEVICE = os.getenv("GAIT_DEVICE", "NPU")
CAMERA_CONFIG_RELOAD_INTERVAL_S = float(os.getenv("CAMERA_CONFIG_RELOAD_INTERVAL_S", "1"))
# Device placement (capacity analysis: vehicle is the binding stage -> iGPU; plate +
# OCR fit on the NPU). All overridable so the capacity probe can re-assign.
VEHICLE_DEVICE = os.getenv("VEHICLE_DEVICE", "GPU")
PLATE_DEVICE = os.getenv("PLATE_DEVICE", "GPU")
SMOKE_FIRE_DEVICE = os.getenv("SMOKE_FIRE_DEVICE", "NPU")
MOTION_GATE_ENABLED = os.getenv("SENTINEL_MOTION_GATE_ENABLED", "true").lower() not in {"0", "false", "no"}
MOTION_CONFIRM_WIN = int(os.getenv("SENTINEL_MOTION_CONFIRM_WIN", "14"))
MOTION_CONFIRM_NEED = int(os.getenv("SENTINEL_MOTION_CONFIRM_NEED", "4"))
MOTION_RELEASE_MISSES = int(os.getenv("SENTINEL_MOTION_RELEASE_MISSES", "6"))
MOTION_SWEEP_SECONDS = float(os.getenv("SENTINEL_MOTION_SWEEP_SECONDS", "5.0"))
MOTION_REDET_EVERY = int(os.getenv("SENTINEL_MOTION_REDET_EVERY", "25"))
GPU_RESOURCE_BACKOFF_INITIAL_S = float(os.getenv("GPU_RESOURCE_BACKOFF_INITIAL_S", "0.05"))
GPU_RESOURCE_BACKOFF_MAX_S = float(os.getenv("GPU_RESOURCE_BACKOFF_MAX_S", "0.5"))
SHARED_VEHICLE_INFERENCE = os.getenv(
    "SENTINEL_SHARED_VEHICLE_INFERENCE", "true"
).lower() in {"1", "true", "yes", "on"}
SHARED_VEHICLE_REQUESTS = int(os.getenv("SENTINEL_SHARED_VEHICLE_REQUESTS", "4"))


def require_accelerator_device(label: str, requested: str) -> None:
    parts = requested.split(":", 1)
    if len(parts) == 2 and parts[0] != "MULTI":
        raise ValueError(f"{label} must use GPU/NPU or MULTI:GPU,NPU, got {requested}")
    devices = parts[-1].split(",")
    if not devices or any(device.split(".", 1)[0] not in {"GPU", "NPU"} for device in devices):
        raise ValueError(f"{label} must use GPU/NPU, got {requested}")
# Class order from the nine-class vehicle model's embedded metadata. The model
# calls class 7 "person"; the rest of this pipeline uses the canonical label
# "pedestrian", so normalize it at the detector boundary.
VEHICLE_CLASS_NAMES = {
    0: "bus",
    1: "car",
    2: "minivan",
    3: "motorcycle",
    4: "rickshaw",
    5: "truck",
    6: "bicycle",
    7: "pedestrian",
}
VEHICLE_CLASS_IDS = set(VEHICLE_CLASS_NAMES)
STREAM_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://", "tcp://")


def should_evaluate_fire_smoke(detector, frame_index: int, *, idle_sweep: bool) -> bool:
    """Respect the model cadence during activity and force idle safety sweeps."""
    return bool(detector is not None and (idle_sweep or detector.should_process(frame_index)))


def is_transient_gpu_resource_error(exc: BaseException) -> bool:
    """Identify bounded inference failures that can be retried on a later frame."""
    message = str(exc).upper()
    return (
        "CL_OUT_OF_RESOURCES" in message
        or "ERROR CODE: -5" in message
        or "SHARED VEHICLE INFERENCE TIMED OUT" in message
    )


class SharedVehicleDetectorClient:
    """Camera-side adapter preserving the local detector interface."""

    def __init__(self, channel, confidence: float, execution_devices: str):
        from pipeline.shared_vehicle_inference import VehicleInferenceTransport

        self._transport = VehicleInferenceTransport(
            channel,
            timeout_seconds=float(os.getenv("SENTINEL_SHARED_INFERENCE_TIMEOUT_S", "30")),
        )
        self.confidence = confidence
        self.exec_devices = execution_devices

    def warmup(self, n: int = 2) -> None:
        del n  # The shared service is warmed before camera processes start.

    def reset_request(self) -> None:
        self._transport.reset()

    def detect(self, bgr):
        from detectors.backends.openvino_detector import letterbox, yolo_detections

        if bgr is None or bgr.size == 0:
            return []
        canvas, scale, pad_x, pad_y = letterbox(bgr, 640)
        response = self._transport.infer(canvas)
        if response["error"]:
            raise RuntimeError(response["error"])
        h, w = bgr.shape[:2]
        return yolo_detections(
            response["output"], image_shape=(h, w), scale=scale,
            pad_x=pad_x, pad_y=pad_y, confidence=self.confidence,
            class_ids=VEHICLE_CLASS_IDS, class_names=VEHICLE_CLASS_NAMES,
            model_name="vehicle",
        )

    @property
    def scheduler_queue_wait_seconds(self) -> float:
        return self._transport.last_queue_wait_seconds

    @property
    def scheduler_device_inference_seconds(self) -> float:
        return self._transport.last_device_inference_seconds


def load_cameras() -> list[dict]:
    import json

    video_dir = os.getenv("VIDEO_DIR", "")
    with open(CAMERAS_FILE) as fh:
        data = json.load(fh)
    cams = []
    for cam in data.get("cameras", []):
        if not cam.get("enabled", True):
            continue
        uri = (cam.get("source") or {}).get("uri")
        if not uri:
            continue
        if not uri.startswith(STREAM_SCHEMES):
            if not os.path.isabs(uri) and video_dir:
                uri = os.path.join(video_dir, uri)
            if not os.path.exists(uri):
                log.warning("camera %s: file %r missing, skipping", cam.get("name"), uri)
                continue
        cams.append({"name": cam["name"], "uri": uri, "cfg": cam})
    return cams


def build_camera_config(c: dict) -> dict[str, Any]:
    from pipeline.processor_config import normalize_runtime_analytics

    analytics = (c["cfg"].get("analytics") or {}).copy()
    if not any(uc.get("enabled") for uc in analytics.values()):
        analytics = {"plate_detection": {"enabled": True, "lines": [], "zones": [], "masks": []}}
    cfg = {
        "camera_id": c["name"],
        "name": c["name"],
        "enabled": True,
        "processing": c["cfg"].get("processing") or {},
        "source": c["cfg"].get("source") or {},
        "analytics": analytics,
    }
    cfg["runtime_analytics"] = normalize_runtime_analytics(cfg)
    return cfg


def _same_hot_reload_shape(current: dict[str, Any], updated: dict[str, Any]) -> bool:
    return (
        (current.get("source") or {}).get("uri") == (updated.get("source") or {}).get("uri")
        and set((current.get("analytics") or {})) == set((updated.get("analytics") or {}))
    )


def _refresh_camera_config_if_changed(
    camera_name: str,
    camera_config: dict[str, Any],
    last_mtime_ns: int | None,
) -> int | None:
    try:
        mtime_ns = os.stat(CAMERAS_FILE).st_mtime_ns
    except OSError:
        return last_mtime_ns
    if last_mtime_ns is None:
        return mtime_ns
    if mtime_ns == last_mtime_ns:
        return last_mtime_ns
    updated_camera = next((cam for cam in load_cameras() if cam["name"] == camera_name), None)
    if updated_camera is None:
        log.warning("cam[%s] hot config skipped: camera removed from %s", camera_name, CAMERAS_FILE)
        return mtime_ns
    updated_config = build_camera_config(updated_camera)
    if not _same_hot_reload_shape(camera_config, updated_config):
        log.warning("cam[%s] hot config skipped: source/apps changed", camera_name)
        return mtime_ns
    camera_config["analytics"] = updated_config["analytics"]
    camera_config["runtime_analytics"] = updated_config["runtime_analytics"]
    camera_config["processing"] = updated_config["processing"]
    log.info("cam[%s] hot config applied from %s", camera_name, CAMERAS_FILE)
    return mtime_ns


def build_post_detection_stages(camera_configs, model_config):
    from pipeline.analytics import DetectionGeometryFilterStage, TrafficAnalyticsStage
    from pipeline.tracking import DetectionTrackerStage

    t = model_config.vehicle_tracker
    first_camera = next(iter(camera_configs.values()), {})
    target_fps = float((first_camera.get("processing") or {}).get("fps", INFER_FPS))
    return (
        DetectionGeometryFilterStage(camera_configs, model_name="vehicle"),
        DetectionTrackerStage(
            model_name="vehicle",
            max_distance=float(t.get("max_distance", 320.0)),
            max_disappeared=int(t.get("max_disappeared", 45)),
            bbox_smoothing=float(t.get("bbox_smoothing", 0.25)),
            min_iou=float(t.get("min_iou", 0.01)),
            class_aware=bool(t.get("class_aware", False)),
            class_switch_cost=float(t.get("class_switch_cost", 0.15)),
            draw_predictions=bool(t.get("draw_predictions", True)),
            target_fps=target_fps,
            adaptive=True,
        ),
        TrafficAnalyticsStage(camera_configs),
    )


def _crop(frame, bbox):
    h, w = frame.shape[:2]
    x1 = max(0, min(w, bbox[0])); x2 = max(0, min(w, bbox[2]))
    y1 = max(0, min(h, bbox[1])); y2 = max(0, min(h, bbox[3]))
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


def _parent_vehicle(plate, vehicles):
    from pipeline.detection_quality import best_parent_vehicle
    return best_parent_vehicle(plate, vehicles)


def consume_packet(packet, geo, tracker, analytics_stage, stab, async_ocr):
    geo.process([packet])
    tracker.process([packet])
    vehicles = [
        d for d in packet.detections
        if d.model_name == "vehicle" and d.metadata.get("track_id") is not None
    ]
    for plate in packet.detections:
        if plate.model_name != "license_plate":
            continue
        vehicle = _parent_vehicle(plate, vehicles)
        if vehicle is None:
            continue
        track_id = int(vehicle.metadata["track_id"])
        plate.parent_id = track_id
        if async_ocr is not None and stab.should_ocr(packet.name, track_id):
            crop = _crop(packet.frame, plate.bbox)
            if crop is not None and getattr(crop, "size", 0):
                async_ocr.submit(
                    crop,
                    (packet.name, track_id, packet.index, plate.bbox[2] - plate.bbox[0]),
                )
                stab.note_ocr_submit(packet.name, track_id)
        text = stab.confirmed_text(packet.name, track_id)
        if text:
            plate.metadata["ocr_text"] = text
            confidence = stab.confirmed_confidence(packet.name, track_id)
            if confidence is not None:
                plate.metadata["ocr_confidence"] = confidence
            version = stab.confirmed_version(packet.name, track_id)
            if version is not None:
                plate.metadata["ocr_observation_id"] = f"{packet.name}:{track_id}:{version}"
                plate.metadata["ocr_support_reads"] = stab.confirmed_support(packet.name, track_id)
        provisional = stab.text_for(packet.name, track_id)
        if provisional:
            plate.metadata["ocr_provisional"] = provisional
    if "v8_events" not in ((geo.camera_configs.get(packet.name) or {}).get("analytics") or {}):
        analytics_stage.process([packet])


def _camera_proc_impl(cam: dict, camera_config: dict, redis_host: str, redis_port: int,
                      stream_key: str, vehicle_channel=None,
                      vehicle_execution_devices: str | None = None,
                      delivery_channel=None) -> None:
    """Child process: owns ONE camera end to end. Builds its own Core, detectors,
    OCR, tracker/analytics stages, decoder and analytics publisher - no shared state."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    stream_session_id = str(uuid.uuid4())
    set_log_context(cam.get("name"), stream_session_id)
    os.environ.setdefault("OPENVINO_MODELS_DIR", OPENVINO_MODELS_DIR)
    # Die if the parent worker dies (even on SIGKILL), so camera processes — and the
    # ffmpeg decoders they own — never orphan and keep hogging the GPU/media engine.
    try:
        import ctypes as _ct
        import signal as _sig
        _ct.CDLL("libc.so.6").prctl(1, _sig.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass

    from pipeline.sentinel_v8_handoff import V8HandoffClient
    v8_config = (
        (camera_config.get("analytics") or {}).get("v8_events", {}).get("v8") or {}
    )
    handoff_client = V8HandoffClient(
        delivery_channel, str(v8_config["deployment_id"]), str(cam["name"])
    )

    if MOTION_GATE_ENABLED and os.getenv("SENTINEL_SURFACE_MOTION_GATE_ENABLED", "false").lower() in {"1", "true", "yes"}:
        try:
            from pipeline.surface_stream import UnsupportedSurfaceSource, run_surface_camera
            return run_surface_camera(
                cam, camera_config, redis_host, redis_port, stream_key,
                handoff_client=handoff_client,
            )
        except UnsupportedSurfaceSource as exc:
            log.warning("cam[%s] native surface motion path unsupported: %s; using BGR motion gate fallback",
                        cam["name"], exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("cam[%s] native surface motion path failed: %s; using BGR motion gate fallback",
                        cam["name"], exc)

    from decode import FfmpegDecoder
    from detectors.backends.openvino_detector import OpenVINOYOLODetector, shared_core
    from detectors.backends.openvino_ocr_async import AsyncOCR
    from detectors.backends.openvino_smoke_fire import OpenVINOSmokeFireDetector
    from detectors.base import openvino_model_path
    from pipeline.config import load_worker_config, model_config_from_worker_config
    from pipeline.detection_quality import deduplicate_vehicles, plate_crop_is_usable
    from pipeline.ocr_stabilizer import OcrStabilizer
    from pipeline.output_sinks import AsyncAnalyticsDispatcher, build_analytics_sink
    from pipeline.types import FramePacket

    name, uri = cam["name"], cam["uri"]
    camera_fps = max(1, int(round(float(
        (camera_config.get("processing") or {}).get("fps", INFER_FPS)
    ))))
    camera_configs = {name: camera_config}
    worker_config = load_worker_config()
    mc = model_config_from_worker_config(worker_config)

    runtime = camera_config.get("runtime_analytics") or {}
    enable_plate = "plate_detection" in runtime
    enable_smoke_fire = "fire_smoke_detection" in runtime
    enable_face = "face_recognition" in runtime
    enable_scene = "scene_embeddings" in runtime
    enable_reid = "person_reid" in runtime
    enable_v8 = "v8_events" in runtime
    v8 = (camera_config.get("analytics") or {}).get("v8_events", {}).get("v8") or {}
    enabled_profiles = {profile for binding in v8.get("bindings") or []
                        for profile in binding.get("embedding_profile_ids") or []}

    if enable_v8:
        requested_devices = {"vehicle": VEHICLE_DEVICE}
        if enable_plate:
            requested_devices["plate"] = PLATE_DEVICE
            requested_devices["ocr"] = os.getenv("OCR_DEVICE", "MULTI:GPU,NPU")
        if enable_smoke_fire:
            smoke_config = ((worker_config.get("models") or {}).get("smoke_fire") or {})
            requested_devices["fire/smoke"] = str(smoke_config.get("device") or SMOKE_FIRE_DEVICE)
        if enable_face:
            requested_devices["face detector"] = os.getenv("FACE_DETECTOR_DEVICE", "GPU")
            requested_devices["face embedder"] = os.getenv("FACE_EMBEDDER_DEVICE", "NPU")
            requested_devices["head pose"] = os.getenv("HEAD_POSE_DEVICE", "NPU")
        if enable_reid and "transreid-ssl-v18.1" in enabled_profiles:
            requested_devices["body"] = BODY_REID_DEVICE
        if enable_reid and "gaitbase-v18.1" in enabled_profiles:
            requested_devices["gait"] = GAIT_DEVICE
        if enable_scene:
            requested_devices["scene"] = SCENE_EMBEDDER_DEVICE
        for label, device in requested_devices.items():
            require_accelerator_device(label, device)

    shared_core()  # per-process singleton Core + CACHE_DIR
    vehicle_config = ((worker_config.get("models") or {}).get("vehicle") or {})
    plate_config = ((worker_config.get("models") or {}).get("plate") or {})
    vehicle_confidence = float(os.getenv(
        "VEHICLE_CONF", str(vehicle_config.get("minimum_confidence", 0.3))
    ))
    if vehicle_channel is None:
        veh = OpenVINOYOLODetector(
            openvino_model_path("vehicle_yolo26n_fp16_dynamic"), "vehicle",
            device=VEHICLE_DEVICE, imgsz=640, confidence=vehicle_confidence,
            class_ids=VEHICLE_CLASS_IDS, class_names=VEHICLE_CLASS_NAMES,
        )
    else:
        veh = SharedVehicleDetectorClient(
            vehicle_channel,
            vehicle_confidence,
            vehicle_execution_devices or VEHICLE_DEVICE,
        )
    plate = None
    if enable_plate:
        plate = OpenVINOYOLODetector(
            openvino_model_path("license_plate"), "license_plate", device=PLATE_DEVICE, imgsz=224,
            confidence=float(os.getenv("PLATE_CONF", str(plate_config.get("confidence", 0.35)))),
            max_detections=1)
    smoke_fire = None
    if enable_smoke_fire:
        smoke_fire_config = ((worker_config.get("models") or {}).get("smoke_fire") or {})
        smoke_fire = OpenVINOSmokeFireDetector(
            openvino_model_path("smoke_fire"),
            device=str(smoke_fire_config.get("device") or SMOKE_FIRE_DEVICE),
            imgsz=int(smoke_fire_config.get("imgsz") or 320),
            confidence=float(smoke_fire_config.get("confidence", 0.35)),
            iou_threshold=float(smoke_fire_config.get("iou_threshold", 0.45)),
            processing_interval=int(smoke_fire_config.get("processing_interval", 5)),
        )
    veh.warmup()
    if plate is not None:
        plate.warmup()
    if smoke_fire is not None:
        smoke_fire.warmup()
    motion_gate = MotionGateController(
        confirm_win=MOTION_CONFIRM_WIN,
        confirm_need=MOTION_CONFIRM_NEED,
        release_misses=MOTION_RELEASE_MISSES,
        sweep_seconds=MOTION_SWEEP_SECONDS,
        redet_every=MOTION_REDET_EVERY,
        start_active=True,
    ) if MOTION_GATE_ENABLED else None
    face_pipeline = None
    face_extractor = None
    if enable_face:
        from detectors.backends.openvino_face import OpenVINOFaceExtractor
        from pipeline.face_samples import FaceSamplePipeline
        face_extractor = OpenVINOFaceExtractor(
            os.path.join(FACE_MODELS_DIR, "scrfd_500m.onnx"),
            os.path.join(FACE_MODELS_DIR, "adaface_ir101_int8.xml"),
            os.path.join(FACE_MODELS_DIR, "best_a0_fp16.xml"),
        )
        face_extractor.warmup()
        face_pipeline = FaceSamplePipeline(
            name, os.getenv("EDGE_ID", "unknown"), face_extractor, camera_config,
            enable_legacy_delivery=False,
        )
    body_extractor = None
    gait_extractor = None
    if enable_reid:
        from pipeline.person_reid import (
            OpenVINOBodyExtractor,
            OpenVINOGaitExtractor,
        )
        if "transreid-ssl-v18.1" in enabled_profiles:
            body_extractor = OpenVINOBodyExtractor(
                os.path.join(REID_MODELS_DIR, "transreid_ssl_int8.xml"), BODY_REID_DEVICE
            )
            body_extractor.warmup()
        if "gaitbase-v18.1" in enabled_profiles:
            gait_extractor = OpenVINOGaitExtractor(
                os.path.join(REID_MODELS_DIR, "gaitbase_int8.xml"), gait_device=GAIT_DEVICE,
            )
            gait_extractor.warmup()
    scene_extractor = None
    if enable_scene:
        from pipeline.scene_embeddings import OpenVINOSceneExtractor
        scene_extractor = OpenVINOSceneExtractor(
            SCENE_MODEL_PATH, device=SCENE_EMBEDDER_DEVICE
        )
        scene_extractor.warmup()
    from pipeline.runtime_telemetry import CameraTelemetry
    telemetry_devices = {
        "decode": "GPU/VAAPI",
        "vehicle_person_detection": VEHICLE_DEVICE,
    }
    if plate is not None:
        telemetry_devices["anpr_plate_detection"] = PLATE_DEVICE
        telemetry_devices["anpr_ocr"] = os.getenv("OCR_DEVICE", "MULTI:GPU,NPU")
    if smoke_fire is not None:
        telemetry_devices["fire_smoke_detection"] = smoke_fire.exec_devices
    if face_pipeline is not None:
        telemetry_devices.update({
            "face_detection": os.getenv("FACE_DETECTOR_DEVICE", "GPU"),
            "face_embedding": os.getenv("FACE_EMBEDDER_DEVICE", "NPU"),
            "face_pose": os.getenv("HEAD_POSE_DEVICE", "NPU"),
        })
    if body_extractor is not None:
        telemetry_devices["body_embedding"] = BODY_REID_DEVICE
    if gait_extractor is not None:
        telemetry_devices["gait_embedding"] = GAIT_DEVICE
    if scene_extractor is not None:
        telemetry_devices["scene_embedding"] = SCENE_EMBEDDER_DEVICE
    telemetry = CameraTelemetry(
        name, target_fps=camera_fps, devices=telemetry_devices,
        use_case_catalog={str(item["use_case_id"]): str(item["app"])
                          for item in v8.get("bindings") or []},
    )
    if face_extractor is not None:
        face_extractor.telemetry = telemetry
        face_extractor._pose.telemetry = telemetry
    v8_pipeline = None
    if enable_v8:
        from pipeline.v8_events import V8EventPipeline
        v8_pipeline = V8EventPipeline(name, camera_config, scene=scene_extractor,
                                      body=body_extractor, gait=gait_extractor,
                                      telemetry=telemetry,
                                      stream_session_id=stream_session_id,
                                      handoff_client=handoff_client)

    crossing_pipeline = None
    if "vehicle_entry_exit_counts" in runtime and not enable_v8:
        from pipeline.vehicle_crossings import VehicleCrossingPipeline
        crossing_pipeline = VehicleCrossingPipeline(name, camera_config)

    stab = OcrStabilizer(
        min_confidence=float(os.getenv(
            "OCR_MIN_CONFIDENCE", str(plate_config.get("ocr_min_confidence", 0.25))
        )),
        min_plate_width=int(os.getenv(
            "PLATE_MIN_WIDTH", str(plate_config.get("min_width", 16))
        )),
        confirm_min_reads=int(os.getenv(
            "OCR_CONFIRM_MIN_READS", str(plate_config.get("confirm_min_reads", 1))
        )),
        positional_min_character_ratio=mc.lp_ocr_stable_char_ratio,
        require_format=str(os.getenv(
            "OCR_REQUIRE_FORMAT", str(plate_config.get("require_format", False))
        )).lower() in {"1", "true", "yes"},
    )
    async_ocr = None
    if enable_plate:
        async_ocr = AsyncOCR(
            openvino_model_path("ocr"), device=os.getenv("OCR_DEVICE", "MULTI:GPU,NPU"),
            on_result=lambda text, confidence, ud: stab.observe(
                ud[0], ud[1], text, confidence, ud[2], plate_width=ud[3]
            ),
            telemetry=telemetry,
        )
        telemetry.devices["anpr_ocr"] = async_ocr.actual_device
    geo, tracker, analytics_stage = build_post_detection_stages(camera_configs, mc)

    sink = build_analytics_sink(worker_config, redis_host=redis_host, redis_port=redis_port,
                                stream_key=stream_key)
    analytics = AsyncAnalyticsDispatcher(sink=sink, camera_configs=camera_configs,
                                         max_queue_size=4000).start()
    log_event(log, logging.INFO, "camera_pipeline_started", "camera pipeline started",
              target_fps=camera_fps, vehicle_device=str(veh.exec_devices),
              plate_device=str(plate.exec_devices) if plate is not None else "disabled",
              fire_smoke_device=str(smoke_fire.exec_devices) if smoke_fire is not None else "disabled",
              ocr_device=str(async_ocr.actual_device) if async_ocr is not None else "disabled",
              face_device="GPU+NPU" if face_pipeline is not None else "disabled",
              body_device=BODY_REID_DEVICE if body_extractor is not None else "disabled",
              gait_device=GAIT_DEVICE if gait_extractor is not None else "disabled",
              scene_device=SCENE_EMBEDDER_DEVICE if scene_extractor is not None else "disabled",
              v8_events_enabled=v8_pipeline is not None)
    log.debug("cam[%s] device summary: vehicle=%s plate=%s smoke_fire=%s ocr=%s face=%s body=%s gait=%s scene=%s v8=%s", name, veh.exec_devices,
             plate.exec_devices if plate is not None else "disabled",
             smoke_fire.exec_devices if smoke_fire is not None else "disabled",
             async_ocr.actual_device if async_ocr is not None else "disabled",
             "GPU+NPU" if face_pipeline is not None else "disabled",
             BODY_REID_DEVICE if body_extractor is not None else "disabled",
             GAIT_DEVICE if gait_extractor is not None else "disabled",
             SCENE_EMBEDDER_DEVICE if scene_extractor is not None else "disabled",
             "enabled" if v8_pipeline is not None else "disabled")
    log.info("cam[%s] motion_gate %s confirm=%d/%d release=%d sweep=%.1fs",
             name, "enabled" if motion_gate is not None else "disabled",
             MOTION_CONFIRM_NEED, MOTION_CONFIRM_WIN, MOTION_RELEASE_MISSES,
             MOTION_SWEEP_SECONDS)
    # This marker means the camera's models, policy and shared V8 handoff client
    # have initialized. Stream availability remains a camera-health concern and
    # must not prevent healthy peers from making the shared runtime ready.
    publish_camera_state(name, camera_config, "ffmpeg")
    dec = FfmpegDecoder(
        uri, fps=camera_fps, name=name,
        status_callback=v8_pipeline.camera_status if v8_pipeline is not None else None,
        stream_session_id=stream_session_id,
    )
    fidx = 0
    consecutive_gpu_resource_failures = 0
    last = time.time()
    report_n = 0
    try:
        config_mtime_ns = os.stat(CAMERAS_FILE).st_mtime_ns
    except OSError:
        config_mtime_ns = None
    last_config_check = 0.0
    for frame in dec.frames():
        fidx += 1
        frame_received_at = time.time()
        frame_processing_started = time.perf_counter()
        now = time.time()
        if now - last_config_check >= CAMERA_CONFIG_RELOAD_INTERVAL_S:
            last_config_check = now
            previous_mtime_ns = config_mtime_ns
            config_mtime_ns = _refresh_camera_config_if_changed(name, camera_config, config_mtime_ns)
            if config_mtime_ns != previous_mtime_ns and v8_pipeline is not None:
                v8_pipeline.reconfigure(camera_config)
                publish_camera_state(name, camera_config, "ffmpeg")
        if motion_gate is None:
            gate_state, gate_ran, gate_reason = "off", True, "disabled"
        else:
            gate_state, gate_ran, gate_reason = motion_gate.update(
                frame, frame_received_at, camera_fps
            )
        # Count decode immediately. Active frames become inference frames only
        # after the mandatory detector actually succeeds.
        telemetry.frame(inferred=False if not gate_ran else None)

        if not gate_ran:
            if v8_pipeline is not None:
                with telemetry.measure("event_policy.quiet_lifecycle"):
                    v8_pipeline.process_quiet(frame_received_at, fidx)
        else:
            try:
                with telemetry.measure("detection.vehicle_person", device=VEHICLE_DEVICE):
                    raw_vdets = veh.detect(frame)
            except RuntimeError as exc:
                if not is_transient_gpu_resource_error(exc):
                    raise
                consecutive_gpu_resource_failures += 1
                shared_timeout = "shared vehicle inference timed out" in str(exc).lower()
                telemetry.count(
                    "shared_inference_timeouts" if shared_timeout
                    else "gpu_resource_rejections"
                )
                veh.reset_request()
                backoff = min(
                    GPU_RESOURCE_BACKOFF_MAX_S,
                    GPU_RESOURCE_BACKOFF_INITIAL_S
                    * (2 ** min(consecutive_gpu_resource_failures - 1, 4)),
                )
                log_event(
                    log, logging.WARNING,
                    "shared_inference_timeout_retry" if shared_timeout
                    else "gpu_inference_resource_retry",
                    "shared inference timeout; frame skipped"
                    if shared_timeout else
                    "GPU inference resource pressure; frame skipped and request reset",
                    consecutive_failures=consecutive_gpu_resource_failures,
                    retry_backoff_seconds=backoff,
                )
                telemetry.observe_latency(
                    "pipeline.frame_processing_total",
                    time.perf_counter() - frame_processing_started,
                )
                telemetry.maybe_flush()
                time.sleep(backoff)
                continue
            consecutive_gpu_resource_failures = 0
            if isinstance(veh, SharedVehicleDetectorClient):
                telemetry.observe_latency(
                    "detection.vehicle_person.scheduler_queue_wait",
                    veh.scheduler_queue_wait_seconds,
                )
                telemetry.observe_latency(
                    "detection.vehicle_person.device_inference",
                    veh.scheduler_device_inference_seconds,
                )
            telemetry.inference_succeeded()
            vdets = deduplicate_vehicles(
                raw_vdets,
                iou_threshold=float(os.getenv(
                    "VEHICLE_DEDUP_IOU", str(vehicle_config.get("dedup_iou", 0.55))
                )),
                containment_threshold=float(os.getenv(
                    "VEHICLE_DEDUP_CONTAINMENT",
                    str(vehicle_config.get("dedup_containment", 0.85)),
                )),
            )
            dets = list(vdets)
            if plate is not None:
                for vd in vdets:
                    if vd.class_name == "pedestrian":
                        continue
                    x1, y1, x2, y2 = vd.bbox
                    crop = frame[y1:y2, x1:x2]
                    with telemetry.measure("anpr.plate_detection", device=PLATE_DEVICE):
                        for pd in plate.detect(crop):
                            pd.bbox = [x1 + pd.bbox[0], y1 + pd.bbox[1], x1 + pd.bbox[2], y1 + pd.bbox[3]]
                            plate_crop = _crop(frame, pd.bbox)
                            if not plate_crop_is_usable(
                                plate_crop,
                                width=pd.bbox[2] - pd.bbox[0],
                                height=pd.bbox[3] - pd.bbox[1],
                                min_width=int(os.getenv(
                                    "PLATE_MIN_WIDTH", str(plate_config.get("min_width", 16))
                                )),
                                min_height=int(os.getenv(
                                    "PLATE_MIN_HEIGHT", str(plate_config.get("min_height", 5))
                                )),
                                min_sharpness=float(os.getenv(
                                    "PLATE_MIN_SHARPNESS", str(plate_config.get("min_sharpness", 0.0))
                                )),
                                min_aspect_ratio=float(os.getenv(
                                    "PLATE_MIN_ASPECT", str(plate_config.get("min_aspect_ratio", 0.8))
                                )),
                                max_aspect_ratio=float(os.getenv(
                                    "PLATE_MAX_ASPECT", str(plate_config.get("max_aspect_ratio", 12.0))
                                )),
                            ):
                                continue
                            dets.append(pd)
            if smoke_fire is None:
                smoke_evaluated = False
                smoke_dets = []
            else:
                # During active processing respect the model's configured
                # interval. An idle periodic sweep always evaluates smoke/fire
                # so a static hazard can wake the gate.
                smoke_evaluated = should_evaluate_fire_smoke(
                    smoke_fire, fidx, idle_sweep=gate_reason == "sweep"
                )
                smoke_dets = []
                if smoke_evaluated:
                    try:
                        with telemetry.measure(
                            "fire_smoke.detection", device=smoke_fire.exec_devices
                        ):
                            smoke_dets = smoke_fire.detect(frame)
                        dets.extend(smoke_dets)
                    except RuntimeError as exc:
                        smoke_evaluated = False
                        log.warning("cam[%s] fire/smoke inference skipped: %s", name, exc)
            if motion_gate is not None:
                motion_gate.observe_models(
                    bool(vdets), bool(smoke_dets),
                    fully_evaluated=(smoke_fire is None or smoke_evaluated),
                )
            # The rawvideo pipe does not carry source PTS.  Preserve the edge
            # receive clock as receive provenance instead of mislabelling it
            # as a camera/source timestamp.
            packet = FramePacket(index=fidx, name=name, frame=frame,
                                 frame_observed_at=None,
                                 frame_received_at=frame_received_at)
            if isinstance(veh, SharedVehicleDetectorClient):
                packet.analytics_state["model_queue_wait_seconds"] = (
                    veh.scheduler_queue_wait_seconds
                )
                packet.analytics_state["model_device_inference_seconds"] = (
                    veh.scheduler_device_inference_seconds
                )
            packet.analytics_state["fire_smoke_evaluated"] = smoke_evaluated
            packet.analytics_state["motion_gate"] = gate_state
            packet.analytics_state["motion_gate_reason"] = gate_reason
            packet.detections = dets
            with telemetry.measure("tracking_geometry_and_ocr_submit"):
                consume_packet(packet, geo, tracker, analytics_stage, stab, async_ocr)
            if crossing_pipeline is not None:
                crossing_pipeline.process(packet)
            tracked_people = [
                detection for detection in packet.detections
                if detection.model_name == "vehicle"
                and detection.class_name == "pedestrian"
                and detection.metadata.get("track_id") is not None
            ]
            face_samples = []
            if face_pipeline is not None:
                with telemetry.measure("person_identity.face_pipeline"):
                    face_samples = face_pipeline.process(packet, tracked_people)
            if v8_pipeline is not None:
                with telemetry.measure("event_policy.active_frame"):
                    v8_pipeline.process(packet, face_samples)
            if not enable_v8:
                analytics.publish_packets([packet])
        telemetry.observe_latency(
            "pipeline.frame_processing_total",
            time.perf_counter() - frame_processing_started,
        )
        tnow = time.time()
        if fidx % 200 == 0:
            stab.prune(fidx)
        telemetry.maybe_flush()
        report_n += 1
        if tnow - last >= 10.0:
            summary = telemetry.latest_snapshot or telemetry.flush(force=True) or {}
            processing = summary.get("processing") or {}
            log_event(
                log, logging.INFO, "camera_telemetry_summary",
                "current per-camera telemetry summary",
                target_fps=processing.get("target_fps"),
                decoded_fps=processing.get("decoded_fps"),
                inference_fps=processing.get("inference_fps"),
                output_events_per_second=processing.get("output_events_per_second"),
                motion_gate_state=(
                    "disabled" if motion_gate is None
                    else "TRACKING" if motion_gate.tracking else "MOTION"
                ),
                motion_switches=motion_gate.switches if motion_gate is not None else None,
                motion_sweeps=motion_gate.sweeps if motion_gate is not None else None,
                motion_skipped_frames=(
                    motion_gate.skipped_frames if motion_gate is not None else None
                ),
            )
            last = tnow
            report_n = 0
    telemetry.close()


def _camera_proc(cam: dict, camera_config: dict, redis_host: str, redis_port: int,
                 stream_key: str, vehicle_channel=None,
                 vehicle_execution_devices: str | None = None,
                 delivery_channel=None) -> None:
    """Run one camera while preserving the real Python failure in structured logs.

    Some Intel GPU plugin releases can abort while Python unwinds a live
    ``CompiledModel`` after an unhandled exception.  Exiting directly after the
    traceback prevents that destructor failure from replacing the original
    error with a misleading SIGSEGV/longjmp flood.  The fleet supervisor still
    observes a non-zero exit and applies its normal camera-local restart policy.
    """
    try:
        _camera_proc_impl(
            cam, camera_config, redis_host, redis_port, stream_key,
            vehicle_channel, vehicle_execution_devices, delivery_channel,
        )
    except BaseException as exc:  # noqa: BLE001 - child-process failure boundary
        log.exception(
            "camera process failed; exiting without unsafe OpenVINO teardown",
            extra={
                "event": "camera_process_unhandled_exception",
                "exception_type": type(exc).__name__,
            },
        )
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(70)


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import multiprocessing as mp

    os.environ.setdefault("VEHICLE_BACKEND", "openvino")
    os.environ.setdefault("PLATE_BACKEND", "openvino")
    os.environ.setdefault("OCR_BACKEND", "openvino")

    cams = load_cameras()
    if not cams:
        log.error("no usable cameras")
        return 1

    rhost = os.getenv("REDIS_HOST", "localhost")
    rport = int(os.getenv("REDIS_PORT", "6379"))
    skey = os.getenv("ANALYTICS_REDIS_STREAM", "traffic:analytics")

    from monitor import SystemMonitor
    SystemMonitor.from_env(interval=float(os.getenv("SYSTEM_MONITOR_INTERVAL", "2"))).start()

    ctx = mp.get_context("spawn")
    from pipeline.sentinel_v8_handoff import (
        V8HandoffScheduler,
        create_handoff_channels,
    )
    delivery_parent_channels, delivery_child_channels = create_handoff_channels(
        ctx, [camera["name"] for camera in cams]
    )
    delivery_scheduler = V8HandoffScheduler(
        delivery_parent_channels,
        os.getenv(
            "SENTINEL_DELIVERY_AGENT_SOCKET",
            "/run/sentinel-delivery/agent.sock",
        ),
        os.getenv(
            "SENTINEL_CV_HANDOFF_STATUS_PATH",
            "/tmp/sentinel/generated/v8_handoff_status.json",
        ),
    ).start()
    vehicle_channels = {}
    vehicle_slot_owners = []
    vehicle_scheduler = None
    if SHARED_VEHICLE_INFERENCE and VEHICLE_DEVICE.split(".")[0] == "GPU":
        from detectors.base import openvino_model_path
        from pipeline.shared_vehicle_inference import (
            SharedVehicleInferenceScheduler,
            create_vehicle_channels,
            destroy_vehicle_channels,
        )
        from pipeline.config import load_worker_config

        worker_config = load_worker_config()
        vehicle_config = ((worker_config.get("models") or {}).get("vehicle") or {})
        vehicle_confidence = float(os.getenv(
            "VEHICLE_CONF", str(vehicle_config.get("minimum_confidence", 0.3))
        ))
        vehicle_channels, vehicle_slot_owners = create_vehicle_channels(
            ctx, [camera["name"] for camera in cams]
        )
        vehicle_scheduler = SharedVehicleInferenceScheduler(
            openvino_model_path("vehicle_yolo26n_fp16_dynamic"),
            vehicle_confidence,
            vehicle_channels,
            device=VEHICLE_DEVICE,
            class_ids=VEHICLE_CLASS_IDS,
            class_names=VEHICLE_CLASS_NAMES,
            requests=SHARED_VEHICLE_REQUESTS,
        ).start()
        log_event(
            log, logging.INFO, "shared_vehicle_inference_started",
            "shared-memory asynchronous vehicle/person GPU scheduler started",
            execution_devices=vehicle_scheduler.execution_devices,
            camera_count=len(cams), input_size=640,
            async_requests=vehicle_scheduler.requests,
            scheduling="fair_round_robin",
        )

    def start_camera(camera):
        channel = vehicle_channels.get(camera["name"])
        process = ctx.Process(target=_camera_proc, name=f"cam-{camera['name']}", daemon=False,
                              args=(camera, build_camera_config(camera), rhost, rport, skey,
                                    channel,
                                    vehicle_scheduler.execution_devices
                                    if vehicle_scheduler is not None else None,
                                    delivery_child_channels[camera["name"]]))
        process.start()
        increment_supervisor_status(
            camera["name"], "process_starts", state="starting",
            camera_process_pid=process.pid, retrying=False,
        )
        return process

    procs = [start_camera(camera) for camera in cams]
    restart_delays = {camera["name"]: 5.0 for camera in cams}
    next_restart = {camera["name"]: 0.0 for camera in cams}
    restart_scheduled = {camera["name"]: False for camera in cams}
    process_started = {camera["name"]: time.monotonic() for camera in cams}
    log_event(
        log, logging.INFO, "camera_process_fleet_started",
        "camera process fleet started",
        camera_count=len(procs), architecture="isolated-camera-processes",
        shared_vehicle_inference=vehicle_scheduler is not None,
        shared_vehicle_transport="mmap+bounded-token-queues"
        if vehicle_scheduler is not None else "disabled",
        shared_vehicle_async_requests=(
            vehicle_scheduler.requests if vehicle_scheduler is not None else 0
        ),
        openvino_core_per_camera=vehicle_scheduler is None,
    )

    try:
        while True:
            time.sleep(1.0)
            if vehicle_scheduler is not None and not vehicle_scheduler.alive:
                raise RuntimeError(
                    "shared vehicle scheduler stopped: "
                    f"{vehicle_scheduler.failure or 'unknown failure'}"
                )
            if not delivery_scheduler.alive:
                raise RuntimeError("shared V8 delivery handoff scheduler stopped")
            now = time.monotonic()
            for index, process in enumerate(procs):
                if process.is_alive():
                    continue
                camera = cams[index]
                name = camera["name"]
                decoder_status = read_decoder_status(name)
                if not restart_scheduled[name]:
                    log_event(
                        log, logging.ERROR, "camera_process_exited",
                        "camera process exited; restart scheduled",
                        camera_id=name, camera_process_pid=process.pid,
                        stream_session_id=decoder_status.get("stream_session_id"),
                        exit_code=process.exitcode, retry_reason="camera_process_exited",
                        retry_backoff_seconds=restart_delays[name],
                    )
                    increment_supervisor_status(
                        name, "process_exits", state="process_exited",
                        camera_process_pid=process.pid, exit_code=process.exitcode,
                        retrying=True, retry_reason="camera_process_exited",
                        retry_backoff_seconds=restart_delays[name],
                    )
                    process.join(timeout=0)
                    next_restart[name] = now + restart_delays[name]
                    restart_delays[name] = min(restart_delays[name] * 2, 60.0)
                    restart_scheduled[name] = True
                    continue
                if now < next_restart[name]:
                    continue
                procs[index] = start_camera(camera)
                process_started[name] = now
                restart_scheduled[name] = False
                log_event(
                    log, logging.INFO, "camera_process_restarted",
                    "camera process restarted", camera_id=name,
                    stream_session_id=decoder_status.get("stream_session_id"),
                    camera_process_pid=procs[index].pid,
                )
            for index, camera in enumerate(cams):
                name = camera["name"]
                if procs[index].is_alive() and now - process_started[name] >= 30:
                    restart_delays[name] = 5.0
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=3)
        if vehicle_scheduler is not None:
            vehicle_scheduler.close()
        if vehicle_channels:
            destroy_vehicle_channels(vehicle_channels, vehicle_slot_owners)
        delivery_scheduler.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
