"""SDK-native fleet worker: Voyager vehicle->plate cascade (AIPU) + Python OCR.

The detection cascade runs inside the Voyager SDK (HW decode + vehicle + plate +
NMS across all streams). We consume FrameResults and do only what can't run on the
AIPU: CCT OCR on plate crops (async OpenVINO/iGPU) + tracking + analytics.
All network/model artifacts are in-repo (config/cascade/*.yaml, models/cascade-build).
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys
import threading
import time

import cv2
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stream_fleet_sdk")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CAMERAS_FILE = os.getenv("CAMERAS_FILE", f"{REPO}/config/cameras.json")
CASCADE_YAML = os.getenv("CASCADE_YAML", f"{REPO}/config/cascade/vehicle-plate-cascade.yaml")
BUILD_ROOT = os.getenv("CASCADE_BUILD_ROOT", f"{REPO}/models/cascade-build")
INFER_FPS = int(os.getenv("INFER_FPS", "10"))
AIPU_CORES = int(os.getenv("AIPU_CORES", "4"))
# The AIPU allows only ONE cascade stream, so all cameras go through one inference
# process. To beat the ~110 cam-frames/s single-thread consume ceiling, the
# dispatcher fans detections out to CONSUMERS worker processes (track+OCR+analytics),
# routed by hash(camera). CONSUMERS=1 keeps the simple inline path.
CONSUMERS = max(1, int(os.getenv("CONSUMERS", "1")))
VEHICLE_CLASS_IDS = {2, 3, 5, 7}  # COCO car, motorcycle, bus, truck
VEHICLE_CLASS_NAMES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
STREAM_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")

# Optional per-consumer breakdown (PROFILE_CONSUMER=1): where the consume loop spends
# its per-frame budget — geometry filter / tracking / OCR-bind / traffic analytics.
_PROF_CONSUMER = os.getenv("PROFILE_CONSUMER") == "1"
_PROF = {"geo": 0.0, "track": 0.0, "ocr": 0.0, "analytics": 0.0, "n": 0}


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


def _source_arg(uri: str) -> str:
    """Voyager source string. Local files loop so the pilot runs continuously."""
    return uri if uri.startswith(STREAM_SCHEMES) else f"loop:{uri}"


def build_post_detection_stages(camera_configs, model_config):
    """The pipeline stages run inline per frame: geometry filter + vehicle
    tracking + analytics. OCR is NOT a stage anymore — it runs async off the
    consume loop, feeding a track-keyed OcrStabilizer (see main)."""
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
    """Crop a plate box (frame coords) from the native-res source frame for OCR."""
    h, w = frame.shape[:2]
    x1 = max(0, min(w, bbox[0])); x2 = max(0, min(w, bbox[2]))
    y1 = max(0, min(h, bbox[1])); y2 = max(0, min(h, bbox[3]))
    if x2 <= x1 or y2 <= y1:
        return None
    return frame[y1:y2, x1:x2]


def crop_rois_nv12(image, boxes):
    """Extract BGR plate crops directly from the NV12 GStreamer buffer, reading ONLY
    the plate-ROI bytes (the VAAPI surface is uncached, so cost scales with bytes
    touched — this is ~100x cheaper than asarray-ing the whole 1080p frame, at full
    native resolution). Maps the buffer once, views the Y/UV planes zero-copy, and
    converts only the small crops. Returns one BGR crop (or None) per box."""
    from gi.repository import Gst   # lazy: gi is initialized by the SDK at stream start
    sample = image._b
    if sample is None:           # already materialized (e.g. live-view path) -> fall back
        bgr = image.asarray("BGR")
        return [_crop(bgr, b) for b in boxes]
    bi = image._buffer_info
    W, H = bi.width, bi.height
    ypitch, uvpitch = bi.pitch[0], bi.pitch[1]
    yoff, uvoff = bi.buffer_offset[0], bi.buffer_offset[1]
    yh = (uvoff - yoff) // ypitch                      # padded Y height (e.g. 1088)
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return [None] * len(boxes)
    try:
        raw = info.data
        Y = np.frombuffer(raw, np.uint8, count=yh * ypitch, offset=yoff).reshape(yh, ypitch)
        UV = np.frombuffer(raw, np.uint8, count=(H // 2) * uvpitch, offset=uvoff).reshape(H // 2, uvpitch)
        out = []
        for (bx1, by1, bx2, by2) in boxes:
            x1 = max(0, bx1 & ~1); y1 = max(0, by1 & ~1)
            x2 = min(W, (bx2 + 1) & ~1); y2 = min(H, (by2 + 1) & ~1)
            if x2 <= x1 or y2 <= y1:
                out.append(None); continue
            rh, rw = y2 - y1, x2 - x1
            nv = np.empty((rh + rh // 2, rw), np.uint8)
            nv[:rh] = Y[y1:y2, x1:x2]                   # reads only ROI luma bytes
            nv[rh:] = UV[y1 // 2:y2 // 2, x1:x2]        # reads only ROI chroma bytes
            out.append(cv2.cvtColor(nv, cv2.COLOR_YUV2BGR_NV12))
        return out
    finally:
        buf.unmap(info)


def _parent_vehicle(plate, vehicles):
    """Match a plate to the vehicle whose box contains its center (index-free, so
    it survives geometry filtering / tracker reordering)."""
    cx = (plate.bbox[0] + plate.bbox[2]) / 2.0
    cy = (plate.bbox[1] + plate.bbox[3]) / 2.0
    for v in vehicles:
        vb = v.bbox
        if vb[0] <= cx <= vb[2] and vb[1] <= cy <= vb[3]:
            return v
    return None


class _FrameShape:
    """Frame stand-in for consumer processes: analytics only needs .shape, not
    pixels (OCR crops are pre-attached by the dispatcher)."""
    __slots__ = ("shape",)

    def __init__(self, h, w):
        self.shape = (h, w, 3)


def consume_packet(packet, geo, tracker, analytics_stage, stab, async_ocr, ocr_in_cascade):
    """Post-detection consume for one packet: track vehicles, bind plates to their
    track_id, fire async OCR (or take cascade LPRNet text), tag the stable read,
    run analytics. Shared by the inline loop and the consumer processes. OCR crop
    comes from the dispatcher (plate.metadata['_crop']) or, inline, from the frame."""
    p = _PROF if _PROF_CONSUMER else None
    if p: t = time.perf_counter()
    geo.process([packet])
    if p: p["geo"] += time.perf_counter() - t; t = time.perf_counter()
    tracker.process([packet])
    if p: p["track"] += time.perf_counter() - t; t = time.perf_counter()
    vehicles = [d for d in packet.detections
                if d.model_name == "vehicle" and d.metadata.get("track_id") is not None]
    frame = packet.frame
    has_pixels = hasattr(frame, "dtype")
    name, fidx = packet.name, packet.index
    for plate in packet.detections:
        if plate.model_name != "license_plate":
            continue
        veh = _parent_vehicle(plate, vehicles)
        if veh is None:
            continue
        tid = int(veh.metadata["track_id"])
        plate.parent_id = tid
        if ocr_in_cascade:
            raw = plate.metadata.get("ocr_raw_text")
            if raw:
                stab.observe(name, tid, raw, plate.confidence, fidx)
        elif async_ocr is not None and stab.should_ocr(name, tid):
            crop = plate.metadata.pop("_crop", None)
            if crop is None and has_pixels:
                crop = _crop(frame, plate.bbox)
            if crop is not None and getattr(crop, "size", 0):
                async_ocr.submit(crop, (name, tid, fidx, plate.confidence, plate.bbox[2] - plate.bbox[0]))
                stab.note_ocr_submit(name, tid)
        plate.metadata.pop("_crop", None)   # never publish the raw crop
        txt = stab.confirmed_text(name, tid)
        if txt:
            plate.metadata["ocr_text"] = txt          # confirmed only -> 1 plate_read event/track
        prov = stab.text_for(name, tid)
        if prov:
            plate.metadata["ocr_provisional"] = prov  # live-view overlay (may still flicker)
    if p: p["ocr"] += time.perf_counter() - t; t = time.perf_counter()
    analytics_stage.process([packet])
    if p: p["analytics"] += time.perf_counter() - t


def _scalar(v, default=0.0):
    """SDK meta exposes secondary score/class_id as arrays (e.g. score[0]) and
    primary as scalars — normalize both to a plain Python number."""
    try:
        return v[0] if hasattr(v, "__len__") and not isinstance(v, (str, bytes)) else v
    except Exception:  # noqa: BLE001
        return default


def detections_from_result(frame_result) -> list:
    """Convert the SDK cascade FrameResult into our Detection list:
    vehicle boxes (frame coords) + their plate boxes (ROI-local -> frame coords,
    parent_id = vehicle index). For the all-Axelera 3-stage network, also pull the
    LPRNet OCR text off each plate (license_plate_ocr secondary task)."""
    from pipeline.types import Detection

    dets: list = []
    meta = frame_result.meta
    vmeta = None
    for key in ("vehicle_detections", "detections"):
        try:
            vmeta = meta[key]
            break
        except Exception:  # noqa: BLE001
            continue
    if vmeta is None:
        return dets

    for vidx, vobj in enumerate(vmeta.objects):
        vb = [int(vobj.box[0]), int(vobj.box[1]), int(vobj.box[2]), int(vobj.box[3])]
        cls = int(_scalar(getattr(vobj, "class_id", 2), 2))
        if cls not in VEHICLE_CLASS_IDS:
            continue
        dets.append(Detection(
            bbox=vb, class_id=cls, class_name=VEHICLE_CLASS_NAMES.get(cls, "vehicle"),
            confidence=float(_scalar(getattr(vobj, "score", 0.0))), model_name="vehicle",
        ))
        try:
            tasks = vobj.secondary_task_names
        except Exception:  # noqa: BLE001
            tasks = []
        if "plate_detections" not in tasks:
            continue
        try:
            plates = vobj.get_secondary_objects("plate_detections")
        except Exception:  # noqa: BLE001
            plates = []
        for pobj in plates:
            pb = pobj.box
            # The SDK returns secondary (plate) boxes already in absolute FRAME
            # coordinates (verified: plate box sits inside the vehicle box), so use
            # them directly — do NOT add the vehicle offset.
            plate_det = Detection(
                bbox=[int(pb[0]), int(pb[1]), int(pb[2]), int(pb[3])],
                class_id=0, class_name="license_plate",
                confidence=float(_scalar(getattr(pobj, "score", 0.0))),
                model_name="license_plate", parent_id=vidx,
            )
            # all-Axelera path: LPRNet text already produced on the AIPU
            try:
                if "license_plate_ocr" in pobj.secondary_task_names:
                    ocr_meta = pobj.get_secondary_meta("license_plate_ocr")
                    text = ocr_meta.get_result() if hasattr(ocr_meta, "get_result") else None
                    if text:
                        plate_det.metadata["ocr_raw_text"] = str(text)
            except Exception:  # noqa: BLE001
                pass
            dets.append(plate_det)
    return dets


def _consumer_proc(in_q, camera_configs, ocr_device, ocr_in_cascade,
                   redis_host, redis_port, stream_key):
    """Child process: owns its cameras' tracking + async OCR + analytics. Reads
    (frame_idx, camera, detections, (h,w)) from in_q; plate crops ride on each
    plate's metadata['_crop']. None on the queue = shutdown."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pipeline.config import load_worker_config, model_config_from_worker_config
    from pipeline.ocr_stabilizer import OcrStabilizer
    from pipeline.output_sinks import AsyncAnalyticsDispatcher, build_analytics_sink
    from pipeline.types import FramePacket

    worker_config = load_worker_config()
    mc = model_config_from_worker_config(worker_config)
    geo, tracker, analytics_stage = build_post_detection_stages(camera_configs, mc)
    stab = OcrStabilizer(min_confidence=0.4, confirm_min_reads=4,
                         positional_min_character_ratio=0.6)
    async_ocr = None
    if not ocr_in_cascade:
        from detectors.base import openvino_model_path
        from detectors.backends.openvino_ocr_async import AsyncOCR
        async_ocr = AsyncOCR(openvino_model_path("ocr"), device=ocr_device,
                             on_result=lambda text, ud: stab.observe(ud[0], ud[1], text, ud[3], ud[2], plate_width=ud[4] if len(ud) > 4 else 0))
    sink = build_analytics_sink(worker_config, redis_host=redis_host, redis_port=redis_port,
                                stream_key=stream_key)
    analytics = AsyncAnalyticsDispatcher(sink=sink, camera_configs=camera_configs,
                                         max_queue_size=4000).start()
    from monitor import WorkerMetricsMonitor
    if analytics.sink is not None:
        WorkerMetricsMonitor.from_env(
            analytics, interval=float(os.getenv("METRICS_INTERVAL", "2"))).start()
    import queue as _queue
    last_prof = time.time()
    while True:
        try:
            item = in_q.get(timeout=3)
        except _queue.Empty:
            if os.getppid() == 1:   # dispatcher died -> we're orphaned -> exit (no zombies)
                break
            continue
        if item is None:
            break
        frame_idx, name, dets, (h, w) = item
        packet = FramePacket(index=frame_idx, name=name, frame=_FrameShape(h, w))
        packet.detections = dets
        consume_packet(packet, geo, tracker, analytics_stage, stab, async_ocr, ocr_in_cascade)
        analytics.publish_packets([packet])
        if frame_idx % 200 == 0:        # bound stabilizer memory (multiprocess path)
            stab.prune(frame_idx)
        if _PROF_CONSUMER:
            _PROF["n"] += 1
            now = time.time()
            if now - last_prof >= 10.0 and _PROF["n"]:
                n = _PROF["n"]
                ocr_stat = (f" | ocr_sub={async_ocr.submitted} drop={async_ocr.dropped}"
                            if async_ocr is not None else "")
                log.info("consumer[%d] %.0f f/s | per-frame ms: geo=%.2f track=%.2f ocr=%.2f analytics=%.2f%s",
                         os.getpid(), n / (now - last_prof), _PROF["geo"]/n*1e3,
                         _PROF["track"]/n*1e3, _PROF["ocr"]/n*1e3, _PROF["analytics"]/n*1e3, ocr_stat)
                for k in ("geo", "track", "ocr", "analytics"):
                    _PROF[k] = 0.0
                _PROF["n"] = 0
                last_prof = now


def _restart_worker(stream, procs=None) -> None:
    """Re-exec the worker in place (host-native hot-reload on config change). The PID
    survives execv so consumer children would NOT self-orphan — terminate them first;
    stop the SDK stream to release the Metis before the new image re-acquires it."""
    log.warning("re-execing worker: %s", " ".join(sys.argv))
    for p in (procs or []):
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        stream.stop()
    except Exception:  # noqa: BLE001
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _start_config_watcher(active_mode, cameras_file):
    """Poll backend-mode + cameras.json off the hot path; set the returned Event when
    either changes. The loops check the Event (cheap) and re-exec — the blocking HTTP
    poll never touches the single-threaded dispatcher."""
    from pipeline.backend_mode import make_config_watcher
    tick = make_config_watcher(active_mode, cameras_file)
    ev = threading.Event()

    def loop():
        while not ev.is_set():
            try:
                if tick():
                    ev.set()
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)

    threading.Thread(target=loop, daemon=True).start()
    return ev


def run_dispatcher(stream, by_stream, camera_configs, ocr_device, ocr_in_cascade,
                   live_view, n_cams, restart_event=None):
    """One process owns the AIPU stream; fan detections out to CONSUMERS processes
    (routed by hash(camera)). Per frame the dispatcher does only: decode->numpy,
    extract detections, crop plates, enqueue. Live view stays here (it has frames)."""
    import multiprocessing as mp

    rhost = os.getenv("REDIS_HOST", "localhost")
    rport = int(os.getenv("REDIS_PORT", "6379"))
    skey = os.getenv("ANALYTICS_REDIS_STREAM", "traffic:analytics")
    ctx = mp.get_context("spawn")
    queues = [ctx.Queue(maxsize=240) for _ in range(CONSUMERS)]
    procs = [ctx.Process(target=_consumer_proc, daemon=True,
                         args=(queues[i], camera_configs, ocr_device, ocr_in_cascade, rhost, rport, skey))
             for i in range(CONSUMERS)]
    for p in procs:
        p.start()
    log.info("dispatcher: %d consumer processes, routing %d cameras by hash", CONSUMERS, n_cams)

    cam_to_q = {name: queues[hash(name) % CONSUMERS] for name in by_stream.values()}
    prof = os.getenv("PROFILE_DISP") == "1"
    td = tc = tq = 0.0
    frame_idx = 0
    last = time.time()
    report_n = 0
    drops = 0
    for fr in stream:
        name = by_stream.get(getattr(fr, "stream_id", 0))
        if name is None:
            continue
        if restart_event is not None and restart_event.is_set():
            _restart_worker(stream, procs)
        frame_idx += 1
        t = time.perf_counter()
        dets = detections_from_result(fr)
        if prof: td += time.perf_counter() - t; t = time.perf_counter()
        plates = [d for d in dets if d.model_name == "license_plate"]
        img = fr.image
        bi = img._buffer_info
        shape = (bi.height, bi.width)
        tnow = time.time()
        viewing = live_view.wanted(name, tnow)
        if viewing:
            # viewer attached -> need the full frame to draw on (rare, on-demand)
            frame = img.asarray("BGR")
            for p in plates:
                c = _crop(frame, p.bbox)
                if c is not None and c.size:
                    p.metadata["_crop"] = c.copy()
        elif plates:
            # fast path: read ONLY the plate ROI bytes from the NV12 buffer
            for p, c in zip(plates, crop_rois_nv12(img, [p.bbox for p in plates])):
                if c is not None and c.size:
                    p.metadata["_crop"] = c
        if prof: tc += time.perf_counter() - t; t = time.perf_counter()
        q = cam_to_q[name]
        try:
            q.put_nowait((frame_idx, name, dets, shape))
        except Exception:  # queue.Full — that consumer is behind; drop the frame
            drops += 1
        if prof: tq += time.perf_counter() - t
        if viewing:
            live_view.publish(name, frame, dets, tnow)
        report_n += 1
        if tnow - last >= 10.0:
            extra = ""
            if prof and report_n:
                extra = f" | per-frame ms: extract={td/report_n*1e3:.1f} roicrop={tc/report_n*1e3:.1f} dispatch={tq/report_n*1e3:.1f}"
                td = tc = tq = 0.0
            log.info("dispatcher: %.0f cam-frames/s in (%.1f fps/cam x%d) | consumer-drops=%d%s",
                     report_n / (tnow - last), report_n / (tnow - last) / n_cams, n_cams, drops, extra)
            last = tnow
            report_n = 0
            drops = 0


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from axelera.app.config import HardwareCaps, HardwareEnable, SystemConfig
    from axelera.app.stream import create_inference_stream

    from detectors.backends.openvino_ocr_async import AsyncOCR
    from pipeline.backend_mode import apply_backend_mode
    from pipeline.config import load_worker_config, model_config_from_worker_config
    from pipeline.ocr_stabilizer import OcrStabilizer
    from pipeline.output_sinks import AsyncAnalyticsDispatcher, build_analytics_sink
    from pipeline.processor_config import normalize_runtime_analytics
    from pipeline.types import FramePacket

    cams = load_cameras()
    if not cams:
        log.error("no usable cameras in %s", CAMERAS_FILE)
        return 1
    by_stream = {i: c["name"] for i, c in enumerate(cams)}
    network = os.path.basename(CASCADE_YAML)
    log.info("SDK fleet: %d cameras, network=%s, aipu_cores=%d @ %d fps",
             len(cams), network, AIPU_CORES, INFER_FPS)

    active_mode = apply_backend_mode()

    camera_configs = {}
    for c in cams:
        analytics = (c["cfg"].get("analytics") or {}).copy()
        if not any(uc.get("enabled") for uc in analytics.values()):
            analytics = {"plate_detection": {"enabled": True, "lines": [], "zones": [], "masks": []}}
        cfg = {"camera_id": c["name"], "name": c["name"], "enabled": True,
               "processing": c["cfg"].get("processing") or {},
               "source": c["cfg"].get("source") or {}, "analytics": analytics}
        cfg["runtime_analytics"] = normalize_runtime_analytics(cfg)
        camera_configs[c["name"]] = cfg

    worker_config = load_worker_config()
    mc = model_config_from_worker_config(worker_config)
    # OCR runs off the AIPU on OpenVINO; MULTI spreads crops across the iGPU and the
    # otherwise-idle NPU (override with OCR_DEVICE=GPU / NPU / AUTO:GPU,NPU).
    ocr_device = os.getenv("OCR_DEVICE", "MULTI:GPU,NPU") if mc.ocr_backend == "openvino" else "CPU"
    # 3-stage network ("...-lpr") does OCR on the AIPU via LPRNet -> feed that text
    # straight into the stabilizer (no Python OCR). (endswith, not "in" —
    # "alpr-vehicle-plate" contains "lpr" but has no LPRNet.)
    ocr_in_cascade = "lpr" in network.lower() or os.getenv("OCR_IN_CASCADE") == "1"

    log.info("OCR: %s | consumers=%d", "in-cascade (LPRNet/AIPU)" if ocr_in_cascade
             else f"async OpenVINO on {ocr_device}", CONSUMERS)
    # Inline consume components (CONSUMERS==1 only); for >1 they live in the
    # consumer processes spawned by run_dispatcher.
    geo_stage = tracker_stage = analytics_stage = stab = async_ocr = analytics = None
    if CONSUMERS == 1:
        geo_stage, tracker_stage, analytics_stage = build_post_detection_stages(camera_configs, mc)
        stab = OcrStabilizer(min_confidence=0.4, confirm_min_reads=4,
                             positional_min_character_ratio=0.6)
        if not ocr_in_cascade:
            from detectors.base import openvino_model_path
            async_ocr = AsyncOCR(
                openvino_model_path("ocr"), device=ocr_device,
                on_result=lambda text, ud: stab.observe(ud[0], ud[1], text, ud[3], ud[2], plate_width=ud[4] if len(ud) > 4 else 0))
        # Product outputs (Azure Event Hub, Kafka, …) come from worker config;
        # build_analytics_sink always adds a Redis debug tap for the operator UI.
        sink = build_analytics_sink(
            worker_config,
            redis_host=os.getenv("REDIS_HOST", "redis"),
            redis_port=int(os.getenv("REDIS_PORT", "6379")),
            stream_key=os.getenv("ANALYTICS_REDIS_STREAM", "traffic:analytics"))
        analytics = AsyncAnalyticsDispatcher(sink=sink, camera_configs=camera_configs,
                                             max_queue_size=4000).start()

    from monitor import SystemMonitor, WorkerMetricsMonitor
    SystemMonitor.from_env(interval=float(os.getenv("SYSTEM_MONITOR_INTERVAL", "5"))).start()
    if analytics is not None:
        WorkerMetricsMonitor.from_env(
            analytics, interval=float(os.getenv("METRICS_INTERVAL", "2"))).start()

    # On-demand annotated live view (MJPEG via redis; no mediamtx).
    from live_view import LiveView
    import redis as _redis
    try:
        live_r = _redis.Redis(host=os.getenv("REDIS_HOST", "localhost"),
                              port=int(os.getenv("REDIS_PORT", "6379")), socket_timeout=0.5)
        live_r.ping()
    except Exception:  # noqa: BLE001
        live_r = None
    # Publish live frames at up to ~15 fps so the throttle never drops a processed
    # frame (the dispatcher delivers a viewed camera at ~the inference rate, ~10 fps,
    # which is the real ceiling) — gives smooth motion instead of choppy 3 fps.
    live_view = LiveView(live_r, fps=int(os.getenv("STREAM_FPS", "15")),
                         camera_configs=camera_configs)

    system_config = SystemConfig(
        allow_hardware_codec=True,
        hardware_caps=HardwareCaps(
            vaapi=HardwareEnable.enable,
            opencl=HardwareEnable.detect,
            opengl=HardwareEnable.detect,
        ),
    )
    stream = create_inference_stream(
        system_config=system_config,
        network=CASCADE_YAML,                 # in-repo pipeline definition
        build_root=pathlib.Path(BUILD_ROOT),  # in-repo compiled artifacts
        sources=[_source_arg(c["uri"]) for c in cams],
        pipe_type="gst",
        aipu_cores=AIPU_CORES,
        low_latency=True,
        specified_frame_rate=INFER_FPS,
    )
    log.info("inference stream up; consuming…")

    # Hot-reload: re-exec on backend-mode change or cameras.json edit (polled off-thread).
    restart_event = _start_config_watcher(active_mode, CAMERAS_FILE)

    if CONSUMERS > 1:
        try:
            run_dispatcher(stream, by_stream, camera_configs, ocr_device, ocr_in_cascade,
                           live_view, len(cams), restart_event)
        except Exception as exc:  # SDK stream terminated (RTSP drop / inference timeout)
            log.error("inference stream terminated (%s); exiting for supervisor restart", exc)
            os._exit(1)  # consumers self-terminate via getppid()==1; start.sh re-launches
        return 0

    frame_idx = 0
    last_report = time.time()
    report_n = 0
    _dbg_p = _dbg_txt = 0
    try:
        for fr in stream:
            name = by_stream.get(getattr(fr, "stream_id", 0))
            if name is None:
                continue
            if restart_event is not None and restart_event.is_set():
                _restart_worker(stream, None)
            frame = fr.image.asarray("BGR")
            frame_idx += 1
            if frame_idx == 1:
                log.info("frame res = %s (OCR crops come from this)", frame.shape)
            packet = FramePacket(index=frame_idx, name=name, frame=frame)
            packet.detections = detections_from_result(fr)
            consume_packet(packet, geo_stage, tracker_stage, analytics_stage,
                           stab, async_ocr, ocr_in_cascade)
            analytics.publish_packets([packet])
            tnow = time.time()
            if live_view.wanted(name, tnow):               # draw only when a viewer is watching
                live_view.publish(name, frame, packet.detections, tnow)
            if frame_idx % 200 == 0:
                stab.prune(frame_idx)
            report_n += 1
            _dbg_p += sum(1 for d in packet.detections if d.model_name == "license_plate")
            _dbg_txt += sum(1 for d in packet.detections if d.metadata.get("ocr_text"))
            now = time.time()
            if now - last_report >= 10.0:
                dt = now - last_report
                log.info("SDK inference: %.0f cam-frames/s (%.1f fps/cam x%d) | plate=%d ocr_text=%d",
                         report_n / dt, report_n / dt / len(cams), len(cams), _dbg_p, _dbg_txt)
                last_report = now
                report_n = 0
                _dbg_p = _dbg_txt = 0
    finally:
        try:
            if async_ocr is not None:
                async_ocr.wait()
            stream.stop()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
