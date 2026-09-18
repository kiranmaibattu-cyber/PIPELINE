"""All-OpenVINO fleet worker.

Runs the full ALPR cascade — vehicle detect -> plate detect -> OCR — entirely on
OpenVINO (Intel iGPU/NPU/CPU). It also runs the fire/smoke detector as a side
OpenVINO model and feeds all detections into the existing analytics/output path.

Architecture: SHARED-NOTHING MULTIPROCESS — one process per camera, each running
decode -> detect -> plate -> OCR -> analytics -> publish entirely on its own, with
its own ov.Core, detectors, OCR queue, tracker and analytics publisher. Processes share
the iGPU/NPU only at the driver level (compute contention), never the GIL.

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
import sys
import time
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stream_fleet_openvino")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAMERAS_FILE = os.getenv("CAMERAS_FILE", f"{REPO}/config/cameras.json")
INFER_FPS = int(os.getenv("INFER_FPS", "12"))
OPENVINO_MODELS_DIR = os.getenv("OPENVINO_MODELS_DIR", f"{REPO}/models/openvino")
FACE_MODELS_DIR = os.getenv("FACE_MODELS_DIR", "/models/face/openvino")
CAMERA_CONFIG_RELOAD_INTERVAL_S = float(os.getenv("CAMERA_CONFIG_RELOAD_INTERVAL_S", "1"))
# Device placement (capacity analysis: vehicle is the binding stage -> iGPU; plate +
# OCR fit on the NPU). All overridable so the capacity probe can re-assign.
VEHICLE_DEVICE = os.getenv("VEHICLE_DEVICE", "GPU")
PLATE_DEVICE = os.getenv("PLATE_DEVICE", "GPU")
SMOKE_FIRE_DEVICE = os.getenv("SMOKE_FIRE_DEVICE", "AUTO:GPU,NPU,CPU")
VEHICLE_CLASS_IDS = {0, 2, 3, 5, 7}
VEHICLE_CLASS_NAMES = {0: "pedestrian", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
STREAM_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")


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
    cx = (plate.bbox[0] + plate.bbox[2]) / 2.0
    cy = (plate.bbox[1] + plate.bbox[3]) / 2.0
    for vehicle in vehicles:
        x1, y1, x2, y2 = vehicle.bbox
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return vehicle
    return None


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
                    (packet.name, track_id, packet.index, plate.confidence, plate.bbox[2] - plate.bbox[0]),
                )
                stab.note_ocr_submit(packet.name, track_id)
        text = stab.confirmed_text(packet.name, track_id)
        if text:
            plate.metadata["ocr_text"] = text
        provisional = stab.text_for(packet.name, track_id)
        if provisional:
            plate.metadata["ocr_provisional"] = provisional
    analytics_stage.process([packet])


def _camera_proc(cam: dict, camera_config: dict, redis_host: str, redis_port: int,
                 stream_key: str) -> None:
    """Child process: owns ONE camera end to end. Builds its own Core, detectors,
    OCR, tracker/analytics stages, decoder and analytics publisher - no shared state."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault("OPENVINO_MODELS_DIR", OPENVINO_MODELS_DIR)
    # Die if the parent worker dies (even on SIGKILL), so camera processes — and the
    # ffmpeg decoders they own — never orphan and keep hogging the GPU/media engine.
    try:
        import ctypes as _ct
        import signal as _sig
        _ct.CDLL("libc.so.6").prctl(1, _sig.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass

    from decode import FfmpegDecoder
    from detectors.backends.openvino_detector import OpenVINOYOLODetector, shared_core
    from detectors.backends.openvino_ocr_async import AsyncOCR
    from detectors.backends.openvino_smoke_fire import OpenVINOSmokeFireDetector
    from detectors.base import openvino_model_path
    from pipeline.config import load_worker_config, model_config_from_worker_config
    from pipeline.ocr_stabilizer import OcrStabilizer
    from pipeline.output_sinks import AsyncAnalyticsDispatcher, build_analytics_sink
    from pipeline.types import FramePacket

    name, uri = cam["name"], cam["uri"]
    camera_configs = {name: camera_config}
    worker_config = load_worker_config()
    mc = model_config_from_worker_config(worker_config)

    runtime = camera_config.get("runtime_analytics") or {}
    enable_plate = "plate_detection" in runtime
    enable_smoke_fire = "fire_smoke_detection" in runtime
    enable_face = "face_recognition" in runtime

    shared_core()  # per-process singleton Core + CACHE_DIR
    veh = OpenVINOYOLODetector(
        openvino_model_path("vehicle"), "vehicle", device=VEHICLE_DEVICE, imgsz=640,
        confidence=float(os.getenv("VEHICLE_CONF", "0.3")),
        class_ids=VEHICLE_CLASS_IDS, class_names=VEHICLE_CLASS_NAMES)
    plate = None
    if enable_plate:
        plate = OpenVINOYOLODetector(
            openvino_model_path("license_plate"), "license_plate", device=PLATE_DEVICE, imgsz=224,
            confidence=float(os.getenv("PLATE_CONF", "0.25")), max_detections=1)
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
    face_pipeline = None
    if enable_face:
        from detectors.backends.openvino_face import OpenVINOFaceExtractor
        from pipeline.face_samples import FaceSamplePipeline
        face_extractor = OpenVINOFaceExtractor(
            os.path.join(FACE_MODELS_DIR, "scrfd_500m.onnx"),
            os.path.join(FACE_MODELS_DIR, "adaface_ir101_int8.xml"),
        )
        face_extractor.warmup()
        face_pipeline = FaceSamplePipeline(
            name, os.getenv("EDGE_ID", "unknown"), face_extractor, camera_config,
        )

    stab = OcrStabilizer(min_confidence=0.0, min_length=4,
                         positional_min_character_ratio=mc.lp_ocr_stable_char_ratio)
    async_ocr = None
    if enable_plate:
        async_ocr = AsyncOCR(openvino_model_path("ocr"), device=os.getenv("OCR_DEVICE", "MULTI:GPU,NPU"),
                             on_result=lambda text, ud: stab.observe(ud[0], ud[1], text, ud[3], ud[2]))
    geo, tracker, analytics_stage = build_post_detection_stages(camera_configs, mc)

    sink = build_analytics_sink(worker_config, redis_host=redis_host, redis_port=redis_port,
                                stream_key=stream_key)
    analytics = AsyncAnalyticsDispatcher(sink=sink, camera_configs=camera_configs,
                                         max_queue_size=4000).start()
    from monitor import WorkerMetricsMonitor
    if analytics.sink is not None:
        WorkerMetricsMonitor.from_env(
            analytics, interval=float(os.getenv("METRICS_INTERVAL", "2"))).start()

    log.info("cam[%s] up: vehicle=%s plate=%s smoke_fire=%s ocr=%s face=%s", name, veh.exec_devices,
             plate.exec_devices if plate is not None else "disabled",
             smoke_fire.exec_devices if smoke_fire is not None else "disabled",
             async_ocr.actual_device if async_ocr is not None else "disabled",
             "GPU+NPU" if face_pipeline is not None else "disabled")
    dec = FfmpegDecoder(uri, fps=INFER_FPS, name=name)
    fidx = 0
    last = time.time()
    report_n = 0
    try:
        config_mtime_ns = os.stat(CAMERAS_FILE).st_mtime_ns
    except OSError:
        config_mtime_ns = None
    last_config_check = 0.0
    for frame in dec.frames():
        fidx += 1
        now = time.time()
        if now - last_config_check >= CAMERA_CONFIG_RELOAD_INTERVAL_S:
            last_config_check = now
            config_mtime_ns = _refresh_camera_config_if_changed(name, camera_config, config_mtime_ns)
        vdets = veh.detect(frame)
        dets = list(vdets)
        if plate is not None:
            for vd in vdets:
                if vd.class_name == "pedestrian":
                    continue
                x1, y1, x2, y2 = vd.bbox
                crop = frame[y1:y2, x1:x2]
                for pd in plate.detect(crop):
                    pd.bbox = [x1 + pd.bbox[0], y1 + pd.bbox[1], x1 + pd.bbox[2], y1 + pd.bbox[3]]
                    dets.append(pd)
        if smoke_fire is not None and smoke_fire.should_process(fidx):
            dets.extend(smoke_fire.detect(frame))
        packet = FramePacket(index=fidx, name=name, frame=frame)
        packet.detections = dets
        consume_packet(packet, geo, tracker, analytics_stage, stab, async_ocr)
        if face_pipeline is not None:
            tracked_people = [
                detection for detection in packet.detections
                if detection.model_name == "vehicle"
                and detection.class_name == "pedestrian"
                and detection.metadata.get("track_id") is not None
            ]
            face_pipeline.process(packet, tracked_people)
        analytics.publish_packets([packet])
        tnow = time.time()
        if fidx % 200 == 0:
            stab.prune(fidx)
        report_n += 1
        if tnow - last >= 10.0:
            log.info("cam[%s] %.1f fps", name, report_n / (tnow - last))
            last = tnow
            report_n = 0


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
    SystemMonitor.from_env(interval=float(os.getenv("SYSTEM_MONITOR_INTERVAL", "5"))).start()

    ctx = mp.get_context("spawn")
    def start_camera(camera):
        process = ctx.Process(target=_camera_proc, name=f"cam-{camera['name']}", daemon=False,
                              args=(camera, build_camera_config(camera), rhost, rport, skey))
        process.start()
        return process

    procs = [start_camera(camera) for camera in cams]
    restart_delays = {camera["name"]: 5.0 for camera in cams}
    next_restart = {camera["name"]: 0.0 for camera in cams}
    process_started = {camera["name"]: time.monotonic() for camera in cams}
    log.info("started %d camera processes (shared-nothing, one ov.Core each) @ %dfps",
             len(procs), INFER_FPS)

    try:
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            for index, process in enumerate(procs):
                if process.is_alive():
                    continue
                camera = cams[index]
                name = camera["name"]
                if now < next_restart[name]:
                    continue
                log.error("camera process %s exited (code %s); restarting in %.0fs",
                          process.name, process.exitcode, restart_delays[name])
                process.join(timeout=0)
                next_restart[name] = now + restart_delays[name]
                restart_delays[name] = min(restart_delays[name] * 2, 60.0)
                procs[index] = start_camera(camera)
                process_started[name] = now
                log.info("camera process %s restarted", name)
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
