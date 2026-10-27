"""Surface-gated camera path (optional): VAAPI decode -> GPU fixed-slots gate
on VA surfaces -> GPU vehicle/smoke inference from the matched BGR frame
on wake. Reuse BGR for frame packets, plate crops, and downstream stages. Same trackers, events,
and sinks as the stock path - the motion switch only decides which frames
the models examine.

Enabled internally by the CV runtime's built-in motion gate. Unsupported source
geometry/codecs fall back before consuming input. Decoder
interruptions reconnect with camera-health updates. Motion/sweep triggers start
inference on the next decoded frame. Only inference and scheduled quiet scene
frames enter the BGR converter, retaining their exact source timestamp.
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from collections import deque, Counter
from pathlib import Path
from typing import Deque

log = logging.getLogger(__name__)

_GATE_LIB_CANDIDATES = [
    Path(os.getenv("FINALCOMBO_DIR", "/opt/finalcombo")) / "libgpu_motion_gate_fixed_slots.so",
    Path("/home/admin1/Downloads/motion_gate_package/working_pipeline/libgpu_motion_gate_fixed_slots.so"),
]
_BRIDGE_CANDIDATES = [
    Path(os.getenv("FINALCOMBO_DIR", "/opt/finalcombo")) / "libva_y_copy_bridge.so",
    Path("/home/admin1/motion_gate_benchmark/libva_y_copy_bridge.so"),
]
_OCV_CANDIDATES = [
    Path(os.getenv("OCV_LIB", "/opt/finalcombo/ocvlib")),
    Path("/home/admin1/motion_gate_benchmark/opencv-local/root/usr/lib/x86_64-linux-gnu"),
]

FULL_W, FULL_H = 1920, 1080


class UnsupportedSurfaceSource(ValueError):
    """Source cannot use the fixed 16:9 surface graphs; use stock VAAPI."""


def paired_pts(va_sample, bgr_sample) -> int:
    """Reject missing or mismatched timestamps rather than attach wrong pixels."""
    a = int(va_sample.get_buffer().pts)
    b = int(bgr_sample.get_buffer().pts)
    if a == (1 << 64) - 1 or a != b:
        raise RuntimeError(f"surface/BGR timestamps do not match: {a}/{b}")
    return a


def _probe_source(uri):
    import json
    import subprocess
    from decode import _network_input_options
    if '://' in uri and not uri.startswith(('rtsp://', 'rtsps://')):
        raise UnsupportedSurfaceSource('surface path supports RTSP and local H.264/H.265 files')
    result = subprocess.run(
        ['ffprobe', '-v', 'error', *_network_input_options(uri, probe=True),
         '-select_streams', 'v:0', '-show_entries', 'stream=codec_name,width,height',
         '-of', 'json', uri], capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise RuntimeError('surface source probe failed')
    streams = json.loads(result.stdout).get('streams') or []
    if not streams:
        raise RuntimeError('surface source has no video')
    info = streams[0]
    if (info.get('width'), info.get('height')) != (FULL_W, FULL_H):
        raise UnsupportedSurfaceSource('surface path requires 1920x1080 video')
    codec = info.get('codec_name')
    if codec not in ('h264', 'hevc'):
        raise UnsupportedSurfaceSource('surface path requires H.264 or H.265 video')
    return 'h264' if codec == 'h264' else 'h265'



def _first_existing(paths) -> str:
    for path in paths:
        if path.exists():
            return str(path)
    raise RuntimeError("surface stack missing")


class VaBridge:
    """Minimal VA->OpenCL bridge wrapper (acquire/release shared images)."""

    def __init__(self, decoder_pointer: int) -> None:
        self.lib = ctypes.CDLL(_first_existing(_BRIDGE_CANDIDATES))
        lib = self.lib
        lib.va_y_copy_create_from_decoder.argtypes = [
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t]
        lib.va_y_copy_create_from_decoder.restype = ctypes.c_void_p
        lib.va_y_copy_cl_context.argtypes = [ctypes.c_void_p]
        lib.va_y_copy_cl_context.restype = ctypes.c_size_t
        lib.va_y_copy_acquire_image.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.va_y_copy_acquire_image.restype = ctypes.c_size_t
        lib.va_y_copy_release_image.argtypes = [ctypes.c_size_t]
        lib.va_y_copy_destroy.argtypes = [ctypes.c_void_p]
        lib.va_y_copy_last_error.restype = ctypes.c_char_p
        lib.va_sample_surface_id.argtypes = [ctypes.c_size_t,
                                             ctypes.POINTER(ctypes.c_uint32)]
        lib.va_sample_surface_id.restype = ctypes.c_int
        self.handle = lib.va_y_copy_create_from_decoder(decoder_pointer, 180, 320)
        if not self.handle:
            raise RuntimeError(self.error())

    def error(self) -> str:
        value = self.lib.va_y_copy_last_error()
        return value.decode(errors="replace") if value else "bridge error"

    def context(self) -> int:
        value = self.lib.va_y_copy_cl_context(self.handle)
        if not value:
            raise RuntimeError(self.error())
        return value

    def surface_id(self, sample) -> int:
        value = ctypes.c_uint32()
        if self.lib.va_sample_surface_id(hash(sample), ctypes.byref(value)) != 0:
            raise RuntimeError(self.error())
        return value.value

    def acquire(self, sid: int) -> int:
        value = self.lib.va_y_copy_acquire_image(self.handle, sid)
        if not value:
            raise RuntimeError(self.error())
        return value

    def release(self, image: int) -> None:
        self.lib.va_y_copy_release_image(image)

    def close(self) -> None:
        if self.handle:
            self.lib.va_y_copy_destroy(self.handle)
            self.handle = None


class SurfaceSwitch:
    """4/14 confirm, 6-miss release, 5s sweep over bool gate votes."""

    def __init__(self, confirm_win: int = 14, confirm_need: int = 4,
                 release_misses: int = 6, sweep_seconds: float = 5.0,
                 start_active: bool = True) -> None:
        self.hist: Deque[bool] = deque(maxlen=confirm_win)
        self.confirm_need = confirm_need
        self.release_misses = release_misses
        self.sweep_seconds = sweep_seconds
        self.start_active = bool(start_active)
        self.reset()

    def reset(self) -> None:
        self.hist.clear()
        self.tracking = self.start_active
        self.miss = 0
        self.quiet_frames = 0
        self.switches = 0
        self.sweeps = 0
        self.sweep_hits = 0
        self.last_sweep_at = None

    def update(self, candidate: bool, fps: float, timestamp: float | None = None) -> tuple[str, bool]:
        if timestamp is not None and self.last_sweep_at is None:
            self.last_sweep_at = timestamp
        if not self.tracking:
            self.hist.append(bool(candidate))
            if sum(self.hist) >= self.confirm_need:
                self.tracking = True
                self.miss = 0
                self.quiet_frames = 0
                self.switches += 1
                return "TRACKING", True
            self.quiet_frames += 1
            sweep_due = (timestamp - self.last_sweep_at >= self.sweep_seconds
                         if timestamp is not None else
                         self.quiet_frames >= max(1, int(round(self.sweep_seconds * fps))))
            if sweep_due:
                self.last_sweep_at = timestamp
                self.quiet_frames = 0
                self.sweeps += 1
                return "MOTION", True
            return "MOTION", False
        self.last_sweep_at = timestamp
        return "TRACKING", True

    def observe_models(self, vehicle_hit: bool, smoke_hit: bool,
                       fully_evaluated: bool = True) -> bool:
        if vehicle_hit or smoke_hit:
            return self.observe(True)
        if not fully_evaluated:
            # Preserve confirmed-negative progress when a periodic model was
            # intentionally skipped; skipped is neither positive nor negative.
            return False
        return self.observe(False)

    def observe(self, detected: bool) -> bool:
        """Feed the detector outcome back. Returns True on release transition."""
        if not self.tracking:
            if detected:
                self.sweep_hits += 1
                self.tracking = True
                self.miss = 0
                self.quiet_frames = 0
                self.switches += 1
            return False
        if detected:
            self.miss = 0
            return False
        self.miss += 1
        if self.miss >= self.release_misses:
            self.tracking = False
            self.hist.clear()
            self.miss = 0
            self.quiet_frames = 0
            return True
        return False


class SurfaceMotionScheduler:
    """Calculate motion only when it can affect the switch decision.

    Active frames are governed by model outcomes. After the sixth fully
    evaluated negative, seed the native gate from that same retained frame;
    otherwise its next optical-flow comparison would span the active interval.
    The seed is a baseline, not a motion vote or another model evaluation.
    """

    def __init__(self, switch, bridge, gate, stats=None):
        self.switch = switch
        self.bridge = bridge
        self.gate = gate
        self.stats = stats if stats is not None else Counter()

    def _candidate(self, sample):
        image = self.bridge.acquire(self.bridge.surface_id(sample))
        try:
            return self.gate.update(image)
        finally:
            self.bridge.release(image)

    def update(self, sample, fps, timestamp):
        if self.switch.tracking:
            self.stats['motion_skipped_active'] += 1
            candidate = False  # The tracking branch ignores motion votes.
        else:
            candidate = self._candidate(sample)
            self.stats['motion_updates'] += 1
        return self.switch.update(candidate, fps, timestamp)

    def observe_models(self, sample, vehicle_hit, smoke_hit,
                       fully_evaluated=True):
        released = self.switch.observe_models(
            vehicle_hit, smoke_hit, fully_evaluated=fully_evaluated)
        if released:
            self.gate.reset()
            self._candidate(sample)
            self.stats['motion_primes'] += 1
        return released


class NextFrameMotionScheduler(SurfaceMotionScheduler):
    """Defer a motion/sweep wake by one input frame, never an active frame.

    Pending state belongs to this decoder session and is discarded on reconnect.
    A final-frame trigger is counted but cannot produce an inferred next frame.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending = False

    def update(self, sample, fps, timestamp):
        pending, self.pending = self.pending, False
        was_tracking = self.switch.tracking
        state, requested = super().update(sample, fps, timestamp)
        if pending:
            self.stats['deferred_starts'] += 1
            self.switch.last_sweep_at = timestamp
            return state, True
        if requested and not was_tracking:
            self.pending = True
            self.stats['deferred_trigger_frames'] += 1
            return state, False
        return state, requested

    def next_frame_required(self, current_inferred):
        """True only when any possible current model outcome needs the next frame."""
        return self.pending or (
            current_inferred and self.switch.tracking
            and self.switch.miss < self.switch.release_misses - 1)


class SelectiveBgrConverter:
    """Convert explicitly selected retained VA samples using the decoder display.

    There is no continuously running conversion branch. push-sample retains the
    selected GstBuffer while conversion runs, and PTS validates the returned
    pixels before any model or evidence consumer sees them.
    """

    def __init__(self, decoder, caps, stats):
        from gi.repository import Gst
        self.stats = stats
        self.pipeline = Gst.parse_launch(
            'appsrc name=source format=time block=true max-buffers=1 max-bytes=0 ! '
            'vaapipostproc format=bgrx ! videoconvert ! '
            'video/x-raw,format=BGR,width=1920,height=1080 ! '
            'appsink name=bgrsink sync=false max-buffers=1 drop=false '
            'wait-on-eos=false enable-last-sample=false')
        try:
            context = decoder.get_context('gst.vaapi.Display')
            if context is None:
                raise RuntimeError('surface decoder display unavailable')
            self.pipeline.set_context(context)
            self.source = self.pipeline.get_by_name('source')
            self.sink = self.pipeline.get_by_name('bgrsink')
            self.pending_pts = None
            self.source.set_property('caps', caps)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('selected BGR converter failed')
        except BaseException:
            self.close()
            raise

    def submit(self, sample):
        """Queue at most one selected frame; GStreamer converts asynchronously."""
        from gi.repository import Gst
        if self.pending_pts is not None:
            raise RuntimeError('selected BGR conversion already pending')
        started = time.perf_counter()
        if self.source.emit('push-sample', sample) != Gst.FlowReturn.OK:
            raise RuntimeError('selected BGR submission failed')
        self.pending_pts = int(sample.get_buffer().pts)
        self.submit_seconds = time.perf_counter() - started

    def take(self, sample, *, measured=True):
        from gi.repository import Gst
        if self.pending_pts != int(sample.get_buffer().pts):
            raise RuntimeError('selected BGR request belongs to a different frame')
        started = time.perf_counter()
        bgr = self.sink.emit('try-pull-sample', 10 * Gst.SECOND)
        if bgr is None:
            # Do not expose decoder debug text, which can include source credentials.
            raise RuntimeError('selected BGR conversion failed or timed out')
        paired_pts(sample, bgr)
        self.pending_pts = None
        if measured:
            self.stats['bgr_wait_seconds'] += time.perf_counter() - started + self.submit_seconds
            self.stats['bgr_converted_frames'] += 1
            self.stats['matched_pairs'] += 1
        return bgr

    def convert(self, sample, *, measured=True):
        self.submit(sample)
        return self.take(sample, measured=measured)

    def close(self):
        from gi.repository import Gst
        self.pipeline.set_state(Gst.State.NULL)


def _map_bgr(sample):
    from gi.repository import Gst, GstVideo
    import numpy as np
    video = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
    if (video.width, video.height) != (FULL_W, FULL_H):
        raise RuntimeError('unexpected BGR dimensions')
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        raise RuntimeError('BGR map failed')
    try:
        return np.ndarray((video.height, video.width, 3), dtype=np.uint8,
                          buffer=info.data, offset=video.offset[0],
                          strides=(video.stride[0], 3, 1)).copy()
    finally:
        buf.unmap(info)


def run_surface_camera(cam: dict, camera_config: dict, redis_host: str,
                       redis_port: int, stream_key: str, *, benchmark: bool = False,
                       frame_limit: int | None = None, on_ready=None,
                       on_frame=None) -> dict:
    """Run every VA input through scheduling; convert only selected frames.

    Benchmark mode disables JPEG evidence and changes pacing/finite-run lifecycle.
    Model inference and detection post-processing match the production path.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import time
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    gi.require_version("GstVideo", "1.0")
    Gst.init(None)

    import stream_fleet_openvino as sf
    from decode import FfmpegDecoder  # noqa: F401 (kept for parity reference)
    from detectors.backends.openvino_detector import (
        OpenVINOYOLODetector, shared_core)
    from detectors.backends.openvino_ocr_async import AsyncOCR
    from detectors.backends.openvino_smoke_fire import OpenVINOSmokeFireDetector
    from detectors.base import openvino_model_path
    from pipeline.config import load_worker_config, model_config_from_worker_config
    from pipeline.detection_quality import deduplicate_vehicles, plate_crop_is_usable
    from pipeline.motion_gate import MotionGateController  # noqa: F401 (spec parity)
    from pipeline.ocr_stabilizer import OcrStabilizer
    from pipeline.output_sinks import AsyncAnalyticsDispatcher, build_analytics_sink
    from pipeline.surface_gate import SurfaceGate
    from pipeline.surface_stream import SurfaceSwitch, VaBridge
    from pipeline.types import FramePacket

    from pipeline.surface_stream import FULL_W, FULL_H  # noqa: F401 (dims contract)

    name, uri = cam["name"], cam["uri"]
    codec = _probe_source(uri)
    # Private registry: the parent + camera processes must never share one
    # (concurrent scans corrupt it -> elements vanish -> parse failures).
    os.environ["GST_REGISTRY"] = f"/tmp/surface-registry-{name}-{os.getpid()}.bin"
    try:
        os.unlink(os.environ["GST_REGISTRY"])
    except OSError:
        pass
    camera_configs = {name: camera_config}
    worker_config = load_worker_config()
    mc = model_config_from_worker_config(worker_config)

    runtime = camera_config.get("runtime_analytics") or {}
    enable_plate = "plate_detection" in runtime
    enable_smoke_fire = "fire_smoke_detection" in runtime
    enable_face = "face_recognition" in runtime
    enable_scene = "scene_embeddings" in runtime
    enable_reid = "person_reid" in runtime
    enable_v6 = "v6_events" in runtime
    v6 = (camera_config.get("analytics") or {}).get("v6_events", {}).get("v6") or {}
    enabled_profiles = {profile for binding in v6.get("bindings") or []
                        for profile in binding.get("embedding_profile_ids") or []}
    proc = camera_config.get("processing") or {}
    gate_spec = proc.get("motion_gate") or {}
    if (int(gate_spec.get('smoke_every', 1)) != 1 or
        int(gate_spec.get('release_misses', 6)) != 6 or
        int(gate_spec.get('redet_every', 25)) != 25 or
        int(gate_spec.get('confirm_need', 4)) > int(gate_spec.get('confirm_win', 14))):
        raise ValueError('surface gate requires smoke_every=1, release_misses=6, redet_every=25 and confirm_need<=confirm_win')

    shared_core()
    vehicle_config = ((worker_config.get("models") or {}).get("vehicle") or {})
    plate_config = ((worker_config.get("models") or {}).get("plate") or {})
    veh = OpenVINOYOLODetector(
        openvino_model_path("vehicle_yolo26n_fp16"), "vehicle",
        device=sf.VEHICLE_DEVICE, imgsz=640, performance_hint="LATENCY",
        confidence=float(os.getenv("VEHICLE_CONF", str(vehicle_config.get("minimum_confidence", 0.3)))),
        class_ids=sf.VEHICLE_CLASS_IDS, class_names=sf.VEHICLE_CLASS_NAMES)
    plate = None
    if enable_plate:
        plate = OpenVINOYOLODetector(
            openvino_model_path("license_plate"), "license_plate", device=sf.PLATE_DEVICE, imgsz=224,
            confidence=float(os.getenv("PLATE_CONF", str(plate_config.get("confidence", 0.35)))),
            max_detections=1, performance_hint="LATENCY")
    smoke_fire = None
    if enable_smoke_fire:
        smoke_config = ((worker_config.get("models") or {}).get("smoke_fire") or {})
        smoke_fire = OpenVINOSmokeFireDetector(
            openvino_model_path("smoke_fire"),
            device=str(smoke_config.get("device") or sf.SMOKE_FIRE_DEVICE),
            imgsz=int(smoke_config.get("imgsz") or 320), processing_interval=1,
            confidence=float(os.getenv("SMOKE_CONF", str(smoke_config.get("confidence", 0.35)))),
            iou_threshold=float(smoke_config.get("iou_threshold", 0.45)),
        )
    veh.warmup() if hasattr(veh, "warmup") else None
    if plate is not None:
        plate.warmup()

    face_pipeline = None
    face_extractor = None
    if enable_face:
        from detectors.backends.openvino_face import OpenVINOFaceExtractor
        from pipeline.face_samples import FaceSamplePipeline
        face_extractor = OpenVINOFaceExtractor(
            os.path.join(sf.FACE_MODELS_DIR, "scrfd_500m.onnx"),
            os.path.join(sf.FACE_MODELS_DIR, "adaface_ir101_int8.xml"),
            os.path.join(sf.FACE_MODELS_DIR, "best_a0_fp16.xml"),
        )
        face_extractor.warmup()
        face_pipeline = FaceSamplePipeline(
            name, os.getenv("EDGE_ID", "unknown"), face_extractor, camera_config,
            enable_legacy_delivery=False,
        )
    body_extractor = None
    gait_extractor = None
    if enable_reid:
        from pipeline.person_reid import OpenVINOBodyExtractor, OpenVINOGaitExtractor
        if "transreid-ssl-v18.1" in enabled_profiles:
            body_extractor = OpenVINOBodyExtractor(
                os.path.join(sf.REID_MODELS_DIR, "transreid_ssl_int8.xml"), sf.BODY_REID_DEVICE)
            body_extractor.warmup()
        if "gaitbase-v18.1" in enabled_profiles:
            gait_extractor = OpenVINOGaitExtractor(
                os.path.join(sf.REID_MODELS_DIR, "gaitbase_int8.xml"), gait_device=sf.GAIT_DEVICE)
            gait_extractor.warmup()
    scene_extractor = None
    if enable_scene:
        from pipeline.scene_embeddings import OpenVINOSceneExtractor
        scene_extractor = OpenVINOSceneExtractor(sf.SCENE_MODEL_PATH, device=sf.SCENE_EMBEDDER_DEVICE)
        scene_extractor.warmup()
    from pipeline.runtime_telemetry import CameraTelemetry
    fps = int(proc.get('fps', sf.INFER_FPS))
    telemetry_devices = {
        "decode": "GPU/VAAPI",
        "vehicle_person_detection": sf.VEHICLE_DEVICE,
    }
    if plate is not None:
        telemetry_devices["anpr_plate_detection"] = sf.PLATE_DEVICE
        telemetry_devices["anpr_ocr"] = os.getenv("OCR_DEVICE", "MULTI:GPU,NPU")
    if smoke_fire is not None:
        telemetry_devices["fire_smoke_detection"] = sf.SMOKE_FIRE_DEVICE
    if face_pipeline is not None:
        telemetry_devices.update({
            "face_detection": os.getenv("FACE_DETECTOR_DEVICE", "GPU"),
            "face_embedding": os.getenv("FACE_EMBEDDER_DEVICE", "NPU"),
            "face_pose": os.getenv("HEAD_POSE_DEVICE", "NPU"),
        })
    if body_extractor is not None:
        telemetry_devices["body_embedding"] = sf.BODY_REID_DEVICE
    if gait_extractor is not None:
        telemetry_devices["gait_embedding"] = sf.GAIT_DEVICE
    if scene_extractor is not None:
        telemetry_devices["scene_embedding"] = sf.SCENE_EMBEDDER_DEVICE
    telemetry = CameraTelemetry(
        name, target_fps=fps, devices=telemetry_devices,
        use_case_catalog={str(item["use_case_id"]): str(item["app"])
                          for item in v6.get("bindings") or []},
    )
    if face_extractor is not None:
        face_extractor.telemetry = telemetry
        face_extractor._pose.telemetry = telemetry
    v6_pipeline = None
    if enable_v6:
        from pipeline.v6_events import V6EventPipeline
        v6_pipeline = V6EventPipeline(name, camera_config, scene=scene_extractor,
                                      body=body_extractor, gait=gait_extractor,
                                      telemetry=telemetry)
    crossing_pipeline = None
    if "vehicle_entry_exit_counts" in runtime and not enable_v6:
        from pipeline.vehicle_crossings import VehicleCrossingPipeline
        crossing_pipeline = VehicleCrossingPipeline(name, camera_config)

    stab = OcrStabilizer(
        min_confidence=float(os.getenv("OCR_MIN_CONFIDENCE", str(plate_config.get("ocr_min_confidence", 0.25)))),
        min_plate_width=int(os.getenv("PLATE_MIN_WIDTH", str(plate_config.get("min_width", 16)))),
        confirm_min_reads=int(os.getenv("OCR_CONFIRM_MIN_READS", str(plate_config.get("confirm_min_reads", 1)))),
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
                ud[0], ud[1], text, confidence, ud[2], plate_width=ud[3]),
            telemetry=telemetry,
        )
        telemetry.devices["anpr_ocr"] = async_ocr.actual_device
    from pipeline.detection_quality import deduplicate_vehicles, plate_crop_is_usable
    from pipeline.types import FramePacket as _FramePacket
    geo, tracker, analytics_stage = sf.build_post_detection_stages(camera_configs, mc)
    if benchmark:
        analytics_stage.evidence_enabled = False
    sink = build_analytics_sink(worker_config, redis_host=redis_host, redis_port=redis_port,
                                stream_key=stream_key)
    analytics = AsyncAnalyticsDispatcher(sink=sink, camera_configs=camera_configs,
                                         max_queue_size=4000).start()
    sf.log.info("cam[%s] surface path up: vehicle=%s smoke_fire=%s plate=%s ocr=%s face=%s", name,
                veh.exec_devices, smoke_fire.exec_devices if smoke_fire is not None else "disabled",
                plate.exec_devices if plate is not None else "disabled",
                async_ocr.actual_device if async_ocr is not None else "disabled",
                "GPU+NPU" if face_pipeline is not None else "disabled")

    from pipeline.camera_state import publish_camera_state
    import json
    is_live = uri.startswith(('rtsp://', 'rtsps://'))
    stats = Counter()
    config_mtime = os.stat(sf.CAMERAS_FILE).st_mtime_ns if os.path.exists(sf.CAMERAS_FILE) else None
    last_config_check = 0.0
    measured_started = None
    ready = False
    fidx = 0
    startup_failures = 0
    max_startup_failures = int(os.getenv("SENTINEL_SURFACE_MAX_START_FAILURES", "3"))
    switch = SurfaceSwitch(
        confirm_win=int(gate_spec.get('confirm_win', 14)),
        confirm_need=int(gate_spec.get('confirm_need', 4)),
        release_misses=6, sweep_seconds=float(gate_spec.get('sweep_seconds', 5.0)))

    def build_pipeline():
        location = json.dumps(uri)
        if is_live:
            src = (f'rtspsrc location={location} protocols=tcp latency=100 ! '
                   f'rtp{codec}depay ! {codec}parse ! vaapi{codec}dec name=decoder')
        elif Path(uri).suffix.lower() in ('.mp4', '.mov', '.m4v'):
            src = f'filesrc location={location} ! qtdemux ! {codec}parse ! vaapi{codec}dec name=decoder'
        else:
            src = f'filesrc location={location} ! {codec}parse ! vaapi{codec}dec name=decoder'
        cap = '' if benchmark else ' ! videorate drop-only=true'
        rate_caps = '' if benchmark else f',framerate={fps}/1'
        desc = (src + cap + ' ! '
                f'video/x-raw(memory:VASurface),format=NV12,width=1920,height=1080{rate_caps} ! '
                'appsink name=vasink sync=false max-buffers=1 drop=false '
                'wait-on-eos=false enable-last-sample=false')
        pipeline = Gst.parse_launch(desc)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError('surface pipeline failed')
        return pipeline

    def pull_surface(pipeline, *, nonblocking=False):
        bus = pipeline.get_bus()
        def pull(sink):
            sample = sink.emit('try-pull-sample', 0 if nonblocking else 10 * Gst.SECOND)
            if sample is not None:
                return sample
            if nonblocking:
                return None
            msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if msg:
                # GStreamer debug strings can contain credentials: keep them out of logs.
                raise RuntimeError('surface decoder error')
            if sink.get_property('eos'):
                return None
            raise RuntimeError('surface decoder timeout')
        started_pull = time.perf_counter()
        va = pull(pipeline.get_by_name('vasink'))
        if measured_started is not None and va is not None:
            stats['surface_pull_seconds'] += time.perf_counter() - started_pull
        if va is not None and int(va.get_buffer().pts) == (1 << 64) - 1:
            raise RuntimeError('surface timestamp missing')
        return va

    pipeline = bridge = gate = motion = converter = sv = sb = None
    next_surface = None
    next_bgr_pending = False
    try:
        while True:
            try:
                if pipeline is None:
                    log.info('cam[%s] opening surface decoder', name)
                    pipeline = build_pipeline()
                    sv = pull_surface(pipeline)
                    if sv is None:
                        raise RuntimeError('surface decoder produced no frames')
                    log.info('cam[%s] binding surface GPU resources', name)
                    bridge = VaBridge(hash(pipeline.get_by_name('decoder')))
                    gate = SurfaceGate(bridge.context())
                    converter = SelectiveBgrConverter(
                        pipeline.get_by_name('decoder'), sv.get_caps(), stats)
                    # Explicit warm-up does not consume input frames. The first sample
                    # stays retained, is counted once and processed again after timing starts.
                    sid = bridge.surface_id(sv)
                    warm_frame = _map_bgr(converter.convert(sv, measured=False))
                    for _ in range(3):
                        veh.detect(warm_frame)
                        if smoke_fire is not None:
                            smoke_fire.detect(warm_frame)
                        image = bridge.acquire(sid)
                        try:
                            gate.update(image)
                        finally:
                            bridge.release(image)
                    gate.reset()
                    switch.reset()
                    motion = NextFrameMotionScheduler(switch, bridge, gate, stats)
                    if async_ocr is not None and not ready:
                        import numpy as np
                        async_ocr.submit(np.zeros((64, 128, 3), dtype=np.uint8), (name, -1, 0, 128))
                        async_ocr.wait()
                        async_ocr.submitted = async_ocr.dropped = 0
                    if v6_pipeline is not None:
                        v6_pipeline.camera_status('healthy', 'none')
                    if measured_started is None:
                        if on_ready is not None:
                            on_ready()
                        measured_started = time.perf_counter()
                else:
                    if next_surface is not None:
                        sv, next_surface = next_surface, None
                    else:
                        sv = pull_surface(pipeline)
                if sv is None:
                    if benchmark:
                        break
                    raise RuntimeError('surface source ended')
                pts = int(sv.get_buffer().pts)
                sb = None
                frame_received_at = time.time()
                frame_processing_started = time.perf_counter()
                if frame_received_at - last_config_check >= sf.CAMERA_CONFIG_RELOAD_INTERVAL_S:
                    last_config_check = frame_received_at
                    previous = config_mtime
                    config_mtime = sf._refresh_camera_config_if_changed(name, camera_config, config_mtime)
                    if config_mtime != previous:
                        if v6_pipeline is not None:
                            v6_pipeline.reconfigure(camera_config)
                        publish_camera_state(name, camera_config, 'surface')
                gate_state, gate_ran = motion.update(sv, fps, pts / Gst.SECOND)
                quiet_pixels = (not gate_ran and v6_pipeline is not None
                                and v6_pipeline.quiet_frame_needed(frame_received_at))
                if gate_ran or quiet_pixels:
                    if next_bgr_pending:
                        sb = converter.take(sv)
                        next_bgr_pending = False
                        stats['bgr_prefetched_frames'] += 1
                    else:
                        sb = converter.convert(sv)
                elif next_bgr_pending:
                    raise RuntimeError('selected BGR prefetch was not required')
                # The next frame is guaranteed to need inference after a pending
                # trigger, or when this active frame cannot reach six negatives.
                # Never wait for a future live frame before processing this one.
                next_required = motion.next_frame_required(gate_ran)
                if next_required and (frame_limit is None or fidx + 1 < frame_limit):
                    next_surface = pull_surface(pipeline, nonblocking=True)
                    if next_surface is not None:
                        converter.submit(next_surface)
                        next_bgr_pending = True
                        stats['bgr_prefetch_submissions'] += 1
            except RuntimeError:
                if benchmark:
                    raise
                if v6_pipeline is not None:
                    v6_pipeline.camera_status('unavailable', 'stream_disconnected')
                log.warning('cam[%s] surface source interrupted; reconnecting', name)
                # BGR model contexts are independent of the decoder and stay warm.
                sv = sb = next_surface = None
                next_bgr_pending = False
                motion = None
                if converter is not None:
                    converter.close()
                    converter = None
                log.info('cam[%s] releasing gate', name)
                if gate is not None:
                    gate.close()
                    gate = None
                log.info('cam[%s] releasing VA bridge', name)
                if bridge is not None:
                    bridge.close()
                    bridge = None
                log.info('cam[%s] closing surface decoder', name)
                if pipeline is not None:
                    pipeline.set_state(Gst.State.NULL)
                    pipeline = None
                if not ready:
                    startup_failures += 1
                    if startup_failures >= max_startup_failures:
                        raise UnsupportedSurfaceSource(
                            f"surface decoder failed during startup after {startup_failures} attempts")
                time.sleep(1)
                continue
            fidx += 1
            stats['frames'] += 1
            telemetry.frame(inferred=gate_ran)
            if not gate_ran:
                stats['quiet_frames'] += 1
                if v6_pipeline is not None:
                    frame = None
                    if quiet_pixels:
                        stats['bgr_for_quiet_scene'] += 1
                        frame = _map_bgr(sb)
                    with telemetry.measure("event_policy.quiet_lifecycle"):
                        v6_pipeline.process_quiet(frame_received_at, fidx, frame)
            else:
                stats['woken_frames'] += 1
                stats['bgr_for_inference'] += 1
                frame = _map_bgr(sb)
                with telemetry.measure("detection.vehicle_person", device=sf.VEHICLE_DEVICE):
                    vdets = veh.detect(frame)
                vdets = deduplicate_vehicles(vdets)
                dets = list(vdets)
                if plate is not None:
                    for vd in vdets:
                        if vd.class_name == "pedestrian":
                            continue
                        x1, y1, x2, y2 = vd.bbox
                        crop = frame[y1:y2, x1:x2]
                        with telemetry.measure("anpr.plate_detection", device=sf.PLATE_DEVICE):
                            for pd in plate.detect(crop):
                                pd.bbox = [x1 + pd.bbox[0], y1 + pd.bbox[1], x1 + pd.bbox[2], y1 + pd.bbox[3]]
                                plate_crop = sf._crop(frame, pd.bbox)
                                if not plate_crop_is_usable(
                                    plate_crop, width=pd.bbox[2] - pd.bbox[0], height=pd.bbox[3] - pd.bbox[1],
                                    min_width=int(os.getenv("PLATE_MIN_WIDTH", str(plate_config.get("min_width", 16)))),
                                    min_height=int(os.getenv("PLATE_MIN_HEIGHT", str(plate_config.get("min_height", 5)))),
                                    min_sharpness=float(os.getenv("PLATE_MIN_SHARPNESS", str(plate_config.get("min_sharpness", 0.0)))),
                                    min_aspect_ratio=float(os.getenv("PLATE_MIN_ASPECT", str(plate_config.get("min_aspect_ratio", 0.8)))),
                                    max_aspect_ratio=float(os.getenv("PLATE_MAX_ASPECT", str(plate_config.get("max_aspect_ratio", 12.0)))),
                                ):
                                    continue
                                dets.append(pd)
                smoke_dets = []
                stats['vehicle'] += 1
                stats['vehicle_detections'] += len(vdets)
                smoke_evaluated = sf.should_evaluate_fire_smoke(
                    smoke_fire, fidx, idle_sweep=gate_state == "MOTION"
                )
                if smoke_evaluated:
                    try:
                        with telemetry.measure("fire_smoke.detection", device=sf.SMOKE_FIRE_DEVICE):
                            smoke_dets = smoke_fire.detect(frame)
                        stats['smoke'] += 1
                        stats['smoke_detections'] += len(smoke_dets)
                        dets.extend(smoke_dets)
                    except RuntimeError as exc:
                        smoke_evaluated = False
                        sf.log.warning("cam[%s] fire/smoke inference skipped: %s", name, exc)
                released = motion.observe_models(
                    sv, bool(vdets), bool(smoke_dets),
                    fully_evaluated=(smoke_fire is None or smoke_evaluated),
                )
                packet = _FramePacket(index=fidx, name=name, frame=frame,
                                      frame_observed_at=frame_received_at,
                                      frame_received_at=frame_received_at)
                packet.analytics_state["motion_gate"] = gate_state
                packet.analytics_state['fire_smoke_evaluated'] = smoke_evaluated
                packet.detections = dets
                with telemetry.measure("tracking_geometry_and_ocr_submit"):
                    sf.consume_packet(packet, geo, tracker, analytics_stage, stab, async_ocr)
                if crossing_pipeline is not None:
                    crossing_pipeline.process(packet)
                tracked_people = [
                    d for d in packet.detections
                    if d.model_name == "vehicle" and d.class_name == "pedestrian"
                    and d.metadata.get("track_id") is not None
                ]
                face_samples = []
                if face_pipeline is not None:
                    with telemetry.measure("person_identity.face_pipeline"):
                        face_samples = face_pipeline.process(packet, tracked_people)
                if v6_pipeline is not None:
                    with telemetry.measure("event_policy.active_frame"):
                        v6_pipeline.process(packet, face_samples)
                if not enable_v6:
                    analytics.publish_packets([packet])
                stats['packets'] += 1
                if released:
                    tracker._trackers_by_source.clear()
            telemetry.observe_latency(
                "pipeline.frame_processing_total",
                time.perf_counter() - frame_processing_started,
            )
            if not ready:
                publish_camera_state(name, camera_config, 'surface')
                ready = True
            if on_frame is not None:
                on_frame(fidx, pts, gate_ran)
            if fidx % 200 == 0:
                stab.prune(fidx)
            telemetry.maybe_flush()
            if sb is None:
                stats['bgr_skipped_frames'] += 1
            if frame_limit is not None and fidx >= frame_limit:
                break
            # Retain source and selected conversion until all consumers finish.
            sv = sb = None
        if async_ocr is not None:
            async_ocr.wait()
            stats['ocr_submitted'] = async_ocr.submitted
            stats['ocr_dropped'] = async_ocr.dropped
        elapsed = time.perf_counter() - measured_started
        stats['pending_trigger_at_end'] = int(motion.pending)
        return {'counts': dict(stats), 'frames': fidx, 'processing_seconds': elapsed,
                'fps': fidx / elapsed, 'switches': switch.switches, 'sweeps': switch.sweeps,
                'smoke_enabled': smoke_fire is not None}
    finally:
        telemetry.close()
        analytics.close()
        sv = sb = next_surface = None
        if converter is not None:
            converter.close()
        if gate is not None:
            gate.close()
        if bridge is not None:
            bridge.close()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
