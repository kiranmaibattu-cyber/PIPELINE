"""All-OpenVINO fleet worker for an AIPU-less box.

Runs the full ALPR cascade — vehicle detect -> plate detect -> OCR — entirely on
OpenVINO (Arc iGPU + NPU), no Axelera Metis. Uses the SAME models as the hybrid
cascade: yolo26s @640 vehicle, yolo26n @224 plate, CCT OCR.

Architecture: SHARED-NOTHING MULTIPROCESS — one process per camera, each running
decode -> detect -> plate -> OCR -> analytics -> publish entirely on its own, with
its own ov.Core, detectors, OCR queue, tracker and Redis publisher. Processes share
the iGPU/NPU only at the driver level (compute contention), never the GIL.

Why processes, not threads: the per-frame consume work (YOLO postprocess, tracking,
analytics, payload build) is CPU-bound Python. The GIL serializes it, so a thread-
per-camera design saturates ~1 core of Python work and stalls far below target
(measured: 4 cams capped ~7 fps/cam with cores only ~45% busy — the classic GIL
wall). Processes give each camera its own interpreter. This is the same reason the
hybrid worker fans out to consumer PROCESSES — but here it is cleaner: OpenVINO has
no "single cascade stream" limit, so there is NO dispatcher funnel and NO crop IPC
(the two things that capped the hybrid path). Each process is fully independent.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stream_fleet_openvino")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INFER_FPS = int(os.getenv("INFER_FPS", "12"))
OPENVINO_MODELS_DIR = os.getenv("OPENVINO_MODELS_DIR", f"{REPO}/models/openvino")
# Device placement (capacity analysis: vehicle is the binding stage -> iGPU; plate +
# OCR fit on the NPU). All overridable so the capacity probe can re-assign.
VEHICLE_DEVICE = os.getenv("VEHICLE_DEVICE", "GPU")
PLATE_DEVICE = os.getenv("PLATE_DEVICE", "GPU")
VEHICLE_CLASS_IDS = {2, 3, 5, 7}
VEHICLE_CLASS_NAMES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


def _camera_proc(cam: dict, camera_config: dict, redis_host: str, redis_port: int,
                 stream_key: str) -> None:
    """Child process: owns ONE camera end to end. Builds its own Core, detectors,
    OCR, tracker/analytics stages, decoder and Redis publisher — no shared state."""
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
    from detectors.base import openvino_model_path
    from pipeline.config import load_worker_config, model_config_from_worker_config
    from pipeline.ocr_stabilizer import OcrStabilizer
    from pipeline.output_sinks import AsyncAnalyticsDispatcher, build_analytics_sink
    from pipeline.types import FramePacket
    from stream_fleet_sdk import build_post_detection_stages, consume_packet

    name, uri = cam["name"], cam["uri"]
    camera_configs = {name: camera_config}
    worker_config = load_worker_config()
    mc = model_config_from_worker_config(worker_config)

    shared_core()  # per-process singleton Core + CACHE_DIR
    veh = OpenVINOYOLODetector(
        openvino_model_path("vehicle"), "vehicle", device=VEHICLE_DEVICE, imgsz=640,
        confidence=float(os.getenv("VEHICLE_CONF", "0.3")),
        class_ids=VEHICLE_CLASS_IDS, class_names=VEHICLE_CLASS_NAMES)
    plate = OpenVINOYOLODetector(
        openvino_model_path("license_plate"), "license_plate", device=PLATE_DEVICE, imgsz=224,
        confidence=float(os.getenv("PLATE_CONF", "0.25")), max_detections=1)
    veh.warmup()
    plate.warmup()

    stab = OcrStabilizer(min_confidence=0.0, min_length=4,
                         positional_min_character_ratio=mc.lp_ocr_stable_char_ratio)
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

    live_view = None
    try:
        if os.getenv("DISABLE_REDIS") == "1":
            raise RuntimeError("Redis disabled by image contract")
        import redis as _redis
        from live_view import LiveView
        live_r = _redis.Redis(host=redis_host, port=redis_port, socket_timeout=0.5)
        live_r.ping()
        live_view = LiveView(live_r, fps=int(os.getenv("STREAM_FPS", "15")), camera_configs=camera_configs)
    except Exception:  # noqa: BLE001
        live_view = None

    log.info("cam[%s] up: vehicle=%s plate=%s ocr=%s", name, veh.exec_devices,
             plate.exec_devices, async_ocr.actual_device)
    dec = FfmpegDecoder(uri, fps=INFER_FPS, name=name)
    fidx = 0
    last = time.time()
    report_n = 0
    for frame in dec.frames():
        fidx += 1
        vdets = veh.detect(frame)
        dets = list(vdets)
        for vd in vdets:
            x1, y1, x2, y2 = vd.bbox
            crop = frame[y1:y2, x1:x2]
            for pd in plate.detect(crop):
                pd.bbox = [x1 + pd.bbox[0], y1 + pd.bbox[1], x1 + pd.bbox[2], y1 + pd.bbox[3]]
                dets.append(pd)
        packet = FramePacket(index=fidx, name=name, frame=frame)
        packet.detections = dets
        consume_packet(packet, geo, tracker, analytics_stage, stab, async_ocr, False)
        analytics.publish_packets([packet])
        tnow = time.time()
        if live_view is not None and live_view.wanted(name, tnow):
            live_view.publish(name, frame, dets, tnow)
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

    from pipeline.processor_config import normalize_runtime_analytics
    from stream_fleet_sdk import load_cameras

    cams = load_cameras()
    if not cams:
        log.error("no usable cameras")
        return 1

    def build_cfg(c: dict) -> dict:
        analytics = (c["cfg"].get("analytics") or {}).copy()
        if not any(uc.get("enabled") for uc in analytics.values()):
            analytics = {"plate_detection": {"enabled": True, "lines": [], "zones": [], "masks": []}}
        cfg = {"camera_id": c["name"], "name": c["name"], "enabled": True,
               "processing": c["cfg"].get("processing") or {},
               "source": c["cfg"].get("source") or {}, "analytics": analytics}
        cfg["runtime_analytics"] = normalize_runtime_analytics(cfg)
        return cfg

    rhost = os.getenv("REDIS_HOST", "localhost")
    rport = int(os.getenv("REDIS_PORT", "6379"))
    skey = os.getenv("ANALYTICS_REDIS_STREAM", "traffic:analytics")

    from monitor import SystemMonitor
    SystemMonitor.from_env(interval=float(os.getenv("SYSTEM_MONITOR_INTERVAL", "5"))).start()

    ctx = mp.get_context("spawn")
    procs = []
    for c in cams:
        p = ctx.Process(target=_camera_proc, name=f"cam-{c['name']}", daemon=False,
                        args=(c, build_cfg(c), rhost, rport, skey))
        p.start()
        procs.append(p)
    log.info("started %d camera processes (shared-nothing, one ov.Core each) @ %dfps",
             len(procs), INFER_FPS)

    running = True

    def request_stop(signum, frame):  # noqa: ARG001
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    exit_code = 0
    try:
        while running:
            time.sleep(1.0)
            dead = [p for p in procs if not p.is_alive()]
            if dead:
                log.error("camera process %s exited (code %s); shutting down for supervisor restart",
                          dead[0].name, dead[0].exitcode)
                exit_code = 1
                break
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=3)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
