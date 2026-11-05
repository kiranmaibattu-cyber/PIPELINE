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
import json
import logging
import os
import re
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


def dlstreamer_model_instance_id(kind: str, camera_name: str) -> str:
    """Resolve the GVA model-sharing boundary without sharing camera state.

    DL Streamer shares a compiled inference instance only between elements in
    the same process that use the same ``model-instance-id``.  Grouped camera
    workers therefore use one stable ID per model kind, while gvatrack and all
    Sentinel policy objects remain separate elements/objects per camera.
    """
    clean_kind = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(kind)).strip("-")
    scope = os.getenv("SENTINEL_DLSTREAMER_MODEL_SHARE_SCOPE", "").strip()
    if scope:
        clean_scope = re.sub(
            r"[^A-Za-z0-9_.-]+", "-", scope
        ).strip("-")
        shard = "0"
        raw_shards = os.getenv(
            "SENTINEL_DLSTREAMER_CAMERA_MODEL_SHARDS", ""
        ).strip()
        if raw_shards:
            try:
                shard = str(json.loads(raw_shards).get(str(camera_name), 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                shard = "0"
        clean_shard = re.sub(r"[^A-Za-z0-9_.-]+", "-", shard).strip("-")
        return (
            f"{clean_kind}-shared-{clean_scope or 'group'}-"
            f"shard-{clean_shard or '0'}"
        )
    clean_camera = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", str(camera_name)
    ).strip("-")
    return f"{clean_kind}-{clean_camera or 'camera'}"


class UnsupportedSurfaceSource(ValueError):
    """Source cannot use the fixed 16:9 surface graphs; use stock VAAPI."""


class SourcePtsClock:
    """Anchor media PTS to the edge receive clock at decoder ingress.

    Downstream model latency must not move event time. A backwards PTS jump is
    treated as a new media epoch (file loop, sender restart, or reconnect).
    """

    def __init__(self) -> None:
        self.anchor_pts: int | None = None
        self.anchor_wall: float | None = None
        self.last_pts: int | None = None

    def observe(self, pts: int, received_at: float, *, clock_hz: float) -> float:
        pts = int(pts)
        received_at = float(received_at)
        if clock_hz <= 0:
            raise ValueError("source PTS clock must be positive")
        if self.anchor_pts is None or (
            self.last_pts is not None and pts < self.last_pts
        ):
            self.anchor_pts = pts
            self.anchor_wall = received_at
        self.last_pts = pts
        return float(self.anchor_wall) + (pts - int(self.anchor_pts)) / clock_hz


class SourcePtsSampler:
    """Select at most ``fps`` surfaces per second using source media PTS.

    GStreamer's ``videorate`` does not reliably negotiate a live MPEG-TS TCP
    stream while its buffers are still VA surfaces on the deployed Intel
    stack. Sampling at the appsink keeps decode zero-copy and keeps the
    tracker/policy input cadence based on source time rather than model delay.
    """

    def __init__(self, fps: float, *, clock_hz: float) -> None:
        if fps <= 0 or clock_hz <= 0:
            raise ValueError("source sampler fps and clock must be positive")
        self.interval = max(1, int(round(float(clock_hz) / float(fps))))
        self.next_pts: int | None = None
        self.last_pts: int | None = None

    def accept(self, pts: int) -> bool:
        pts = int(pts)
        if self.last_pts is not None and pts < self.last_pts:
            self.next_pts = None
        self.last_pts = pts
        if self.next_pts is not None and pts < self.next_pts:
            return False
        self.next_pts = pts + self.interval
        return True


def paired_pts(va_sample, bgr_sample) -> int:
    """Reject missing or mismatched timestamps rather than attach wrong pixels."""
    a = int(va_sample.get_buffer().pts)
    b = int(bgr_sample.get_buffer().pts)
    if a == (1 << 64) - 1 or a != b:
        raise RuntimeError(f"surface/BGR timestamps do not match: {a}/{b}")
    return a


def _probe_source(uri, *, require_fixed_surface: bool = True):
    import json
    import subprocess
    from decode import _network_input_options
    if '://' in uri and not uri.startswith(('rtsp://', 'rtsps://', 'tcp://')):
        raise UnsupportedSurfaceSource(
            'surface path supports RTSP, MPEG-TS over TCP, and local H.264/H.265 files'
        )
    # The V8 TCP input contract is MPEG-TS carrying H.264. Do not consume a
    # separate connection with ffprobe: several production TCP publishers hand
    # each client a continuous transport stream and cannot reliably transfer
    # immediately from a probe client to the real decoder client. The parser
    # enforces H.264 and the first decoded VA sample is checked for the required
    # geometry below.
    if uri.startswith('tcp://'):
        return 'h264'
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
    if (require_fixed_surface
            and (info.get('width'), info.get('height')) != (FULL_W, FULL_H)):
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
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None


class VAMemorySelectiveBgrConverter:
    """Materialize explicitly selected DL Streamer VAMemory samples.

    Unlike the historical ``VASurface`` converter this adapter consumes the
    modern ``memory:VAMemory`` buffers produced by ``va*dec``/``vapostproc``.
    The buffer itself carries the VA allocation, so the conversion graph can
    negotiate the same device without reaching into a decoder-private display.
    Source PTS is checked before CPU pixels are exposed to policy/evidence.
    """

    def __init__(self, caps, stats, *, label="primary"):
        from gi.repository import Gst

        structure = caps.get_structure(0)
        self.width = int(structure.get_value("width"))
        self.height = int(structure.get_value("height"))
        self.stats = stats
        self.label = str(label)
        self.pipeline = Gst.parse_launch(
            'appsrc name=source format=time block=true max-buffers=1 max-bytes=0 ! '
            'vapostproc ! video/x-raw,format=BGRx ! videoconvert ! '
            'video/x-raw,format=BGR ! '
            'appsink name=bgrsink sync=false max-buffers=1 drop=false '
            'wait-on-eos=false enable-last-sample=false'
        )
        self.source = self.pipeline.get_by_name('source')
        self.sink = self.pipeline.get_by_name('bgrsink')
        self.source.set_property('caps', caps)
        self.pending_pts = None
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError('selected VAMemory BGR converter failed')

    def submit(self, sample):
        from gi.repository import Gst

        if self.pending_pts is not None:
            raise RuntimeError('selected VAMemory conversion already pending')
        started = time.perf_counter()
        if self.source.emit('push-sample', sample) != Gst.FlowReturn.OK:
            raise RuntimeError('selected VAMemory BGR submission failed')
        self.pending_pts = int(sample.get_buffer().pts)
        self.submit_seconds = time.perf_counter() - started

    def take(self, sample, *, measured=True):
        from gi.repository import Gst

        source_pts = int(sample.get_buffer().pts)
        if self.pending_pts != source_pts:
            raise RuntimeError('selected VAMemory request belongs to another frame')
        started = time.perf_counter()
        bgr = self.sink.emit('try-pull-sample', 10 * Gst.SECOND)
        if bgr is None:
            raise RuntimeError('selected VAMemory conversion failed or timed out')
        paired_pts(sample, bgr)
        self.pending_pts = None
        if measured:
            prefix = f'bgr_{self.label}'
            self.stats[f'{prefix}_wait_seconds'] += (
                time.perf_counter() - started + self.submit_seconds
            )
            self.stats[f'{prefix}_converted_frames'] += 1
            self.stats[f'{prefix}_matched_pairs'] += 1
        return bgr

    def convert(self, sample, *, measured=True):
        self.submit(sample)
        return self.take(sample, measured=measured)

    def close(self):
        from gi.repository import Gst

        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None


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


def _map_bgr_any(sample):
    """Copy a DL Streamer BGR sample while preserving its source geometry."""
    from gi.repository import Gst, GstVideo
    import numpy as np
    video = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
    buf = sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        raise RuntimeError('DL Streamer BGR map failed')
    try:
        return np.ndarray(
            (video.height, video.width, 3),
            dtype=np.uint8,
            buffer=info.data,
            offset=video.offset[0],
            strides=(video.stride[0], 3, 1),
        ).copy()
    finally:
        buf.unmap(info)


def _geometry_only_frame(sample):
    """Return a zero-byte ndarray carrying only source H/W geometry."""
    import numpy as np

    structure = sample.get_caps().get_structure(0)
    width = int(structure.get_value("width"))
    height = int(structure.get_value("height"))
    return np.empty((height, width, 0), dtype=np.uint8)


def run_surface_camera(cam: dict, camera_config: dict, redis_host: str,
                       redis_port: int, stream_key: str, *, benchmark: bool = False,
                       frame_limit: int | None = None, on_ready=None,
                       on_frame=None, scene_channel=None,
                       scene_execution_devices: str | None = None,
                       stream_session_id: str | None = None,
                       handoff_client=None, gst_coordinator=None) -> dict:
    """Run every VA input through scheduling; convert only selected frames.

    Benchmark mode disables JPEG evidence and changes pacing/finite-run lifecycle.
    Model inference and detection post-processing match the production path.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    name, uri = cam["name"], cam["uri"]
    dls_authoritative = os.getenv(
        "SENTINEL_DLSTREAMER_ENABLED", "true"
    ).lower() in {"1", "true", "yes", "on"}
    dls_secondaries = bool(
        dls_authoritative
        and os.getenv("SENTINEL_DLSTREAMER_SECONDARIES", "false").lower()
        in {"1", "true", "yes", "on"}
    )
    # GStreamer reads its registry location during Gst.init().  Selecting the
    # camera-private registry afterwards can reuse a hardware-less scan that
    # blacklisted the legacy VAAPI decoder.  Set it first so every camera child
    # discovers VAAPI against its actual /dev/dri access.
    registry_was_unset = "GST_REGISTRY" not in os.environ
    os.environ.setdefault(
        "GST_REGISTRY", f"/tmp/surface-registry-{name}-{os.getpid()}.bin"
    )
    os.environ.setdefault("GST_VAAPI_ALL_DRIVERS", "1")
    if registry_was_unset:
        try:
            os.unlink(os.environ["GST_REGISTRY"])
        except OSError:
            pass
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

    codec = _probe_source(uri, require_fixed_surface=not dls_authoritative)
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
    vehicle_confidence = float(os.getenv(
        "VEHICLE_CONF", str(vehicle_config.get("minimum_confidence", 0.3))
    ))
    zero_copy_vehicle = (not dls_authoritative) and os.getenv(
        "SENTINEL_ZERO_COPY_VEHICLE", "false"
    ).lower() in {"1", "true", "yes", "on"}
    zero_copy_shadow_enabled = (not dls_authoritative) and os.getenv(
        "SENTINEL_ZERO_COPY_SHADOW", "false"
    ).lower() in {"1", "true", "yes", "on"}
    zero_copy_wake_guard = zero_copy_vehicle and os.getenv(
        "SENTINEL_ZERO_COPY_WAKE_GUARD", "true"
    ).lower() in {"1", "true", "yes", "on"}
    # The BGR detector is omitted only when zero-copy owns detections and no
    # parity or quiet-mode wake guard was requested.  The wake guard runs only
    # when a periodic sweep's zero-copy result is empty; it preserves the
    # existing 0.30 collection gate for small/borderline subjects without
    # paying for two detectors on normal active frames.
    veh = None
    if (not dls_authoritative
            and (not zero_copy_vehicle
                 or zero_copy_shadow_enabled
                 or zero_copy_wake_guard)):
        veh = OpenVINOYOLODetector(
            openvino_model_path("vehicle_person_qaft_int8/model"), "vehicle",
            device=sf.VEHICLE_DEVICE, imgsz=640, performance_hint="LATENCY",
            confidence=vehicle_confidence,
            class_ids=sf.VEHICLE_CLASS_IDS, class_names=sf.VEHICLE_CLASS_NAMES)
    plate = None
    if enable_plate:
        if dls_secondaries:
            from pipeline.dls_secondary import DlsPlateDetector
            plate = DlsPlateDetector(
                openvino_model_path("license_plate"),
                "/opt/traffic-pilot/config/plate_raw_model_proc.json",
                device=sf.PLATE_DEVICE,
                confidence=float(os.getenv(
                    "PLATE_CONF", str(plate_config.get("confidence", 0.35))
                )),
                owner_id=name,
            )
        else:
            plate = OpenVINOYOLODetector(
                openvino_model_path("license_plate"), "license_plate", device=sf.PLATE_DEVICE, imgsz=224,
                confidence=float(os.getenv("PLATE_CONF", str(plate_config.get("confidence", 0.35)))),
                max_detections=1, performance_hint="LATENCY")
    dls_fire_enabled = bool(dls_authoritative and enable_smoke_fire and enable_v8)
    smoke_fire = None
    if enable_smoke_fire and not dls_fire_enabled:
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
        from pipeline.face_samples import FaceSamplePipeline
        if dls_authoritative:
            from pipeline.dlstreamer_bridge import DlsFaceExtractor
            face_extractor = DlsFaceExtractor(
                os.path.join(sf.FACE_MODELS_DIR, "best_a0_fp16.xml")
            )
        else:
            from detectors.backends.openvino_face import OpenVINOFaceExtractor
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
            if dls_secondaries:
                from pipeline.dls_secondary import DlsBodyExtractor
                body_extractor = DlsBodyExtractor(
                    os.path.join(sf.REID_MODELS_DIR, "transreid_ssl_int8.xml"),
                    "/opt/traffic-pilot/config/body_reid_model_proc.json",
                    sf.BODY_REID_DEVICE,
                    owner_id=name,
                )
            else:
                body_extractor = OpenVINOBodyExtractor(
                    os.path.join(sf.REID_MODELS_DIR, "transreid_ssl_int8.xml"), sf.BODY_REID_DEVICE)
            body_extractor.warmup()
        if "gaitbase-v18.1" in enabled_profiles:
            if dls_secondaries:
                from pipeline.dls_secondary import DlsGaitExtractor
                gait_extractor = DlsGaitExtractor(
                    os.path.join(
                        sf.REID_MODELS_DIR,
                        "gaitbase_dlstreamer_montage.xml",
                    ),
                    "/opt/traffic-pilot/config/gait_montage_model_proc.json",
                    sf.GAIT_DEVICE,
                    owner_id=name,
                )
            else:
                gait_extractor = OpenVINOGaitExtractor(
                    os.path.join(sf.REID_MODELS_DIR, "gaitbase_int8.xml"), gait_device=sf.GAIT_DEVICE)
            gait_extractor.warmup()
    scene_extractor = None
    if enable_scene:
        if dls_secondaries:
            from pipeline.dls_secondary import DlsSceneExtractor
            scene_extractor = DlsSceneExtractor(
                os.path.join(
                    os.path.dirname(sf.SCENE_MODEL_PATH),
                    "siglip2-base-patch16-224-static.xml",
                ),
                "/opt/traffic-pilot/config/scene_siglip_model_proc.json",
                sf.SCENE_EMBEDDER_DEVICE,
                owner_id=name,
            )
        elif scene_channel is not None:
            from pipeline.shared_scene_inference import SharedSceneExtractorClient
            scene_extractor = SharedSceneExtractorClient(
                scene_channel, scene_execution_devices or sf.SCENE_EMBEDDER_DEVICE
            )
        else:
            from pipeline.scene_embeddings import OpenVINOSceneExtractor
            scene_extractor = OpenVINOSceneExtractor(
                sf.SCENE_MODEL_PATH, device=sf.SCENE_EMBEDDER_DEVICE
            )
        scene_extractor.warmup()
    from pipeline.runtime_telemetry import CameraTelemetry
    fps = int(proc.get('fps', sf.INFER_FPS))
    telemetry_devices = {
        "decode": "GPU/VAAPI-DLStreamer" if dls_authoritative else "GPU/VAAPI",
        "vehicle_person_detection": (
            "GPU/DLStreamer-VA" if dls_authoritative else
            "GPU/VA-RemoteTensor" if zero_copy_vehicle else sf.VEHICLE_DEVICE
        ),
    }
    if dls_authoritative:
        telemetry_devices["object_tracking"] = "CPU/gvatrack"
    if plate is not None:
        telemetry_devices["anpr_plate_detection"] = (
            plate.exec_devices if dls_secondaries else sf.PLATE_DEVICE
        )
        telemetry_devices["anpr_ocr"] = os.getenv("OCR_DEVICE", "MULTI:GPU,NPU")
    if zero_copy_wake_guard:
        telemetry_devices["vehicle_person_wake_guard"] = sf.VEHICLE_DEVICE
    if dls_fire_enabled:
        telemetry_devices["fire_smoke_detection"] = "GPU/DLStreamer-OpenCV-BGR"
    elif smoke_fire is not None:
        telemetry_devices["fire_smoke_detection"] = smoke_fire.exec_devices
    if face_pipeline is not None:
        telemetry_devices.update({
            "face_detection": os.getenv("FACE_DETECTOR_DEVICE", "GPU"),
            "face_embedding": os.getenv("FACE_EMBEDDER_DEVICE", "NPU"),
            "face_pose": os.getenv("HEAD_POSE_DEVICE", "NPU"),
        })
    if body_extractor is not None:
        telemetry_devices["body_embedding"] = (
            body_extractor.actual_device
            if dls_secondaries else sf.BODY_REID_DEVICE
        )
    if gait_extractor is not None:
        telemetry_devices["gait_embedding"] = (
            gait_extractor.actual_device
            if dls_secondaries else sf.GAIT_DEVICE
        )
    if scene_extractor is not None:
        telemetry_devices["scene_embedding"] = (
            scene_extractor.actual_device
            if dls_secondaries else sf.SCENE_EMBEDDER_DEVICE
        )
    telemetry = CameraTelemetry(
        name, target_fps=fps, devices=telemetry_devices,
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
        if dls_secondaries:
            from pipeline.dls_secondary import DlsAsyncOCR
            async_ocr = DlsAsyncOCR(
                openvino_model_path("ocr"),
                "/opt/traffic-pilot/config/ocr_raw_model_proc.json",
                device=os.getenv("OCR_DEVICE", "MULTI:GPU,NPU"),
                on_result=lambda text, confidence, ud: stab.observe(
                    ud[0], ud[1], text, confidence, ud[2], plate_width=ud[3]
                ),
                telemetry=telemetry,
                owner_id=name,
            )
        else:
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
    if dls_authoritative:
        from pipeline.dlstreamer_bridge import GvaTrackerMetadataStage
        tracker = GvaTrackerMetadataStage(fps)
    if benchmark:
        analytics_stage.evidence_enabled = False
    sink = build_analytics_sink(worker_config, redis_host=redis_host, redis_port=redis_port,
                                stream_key=stream_key)
    analytics = AsyncAnalyticsDispatcher(sink=sink, camera_configs=camera_configs,
                                         max_queue_size=4000).start()

    # Compile and warm every ordinary OpenVINO model before initializing the
    # legacy VAAPI GStreamer plugin. On this Intel runtime, initializing VAAPI
    # first can make a subsequent GPU model compilation abort inside the native
    # GPU plugin (observed most often for the plate detector). The zero-copy
    # RemoteTensor graph is intentionally created later, after the decoder owns
    # its VA display.
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst
    # GStreamer 1.28's Python override no longer accepts ``None`` here.  An
    # empty argv works on both the historical 1.24 runtime and DL Streamer.
    Gst.init([])
    if dls_authoritative:
        required_elements = (
            'gvafpsthrottle', 'gvamotiondetect', 'gvadetect', 'gvatrack',
            'gvainference',
            f'va{codec}dec', 'vapostproc',
        )
        missing = [
            element for element in required_elements
            if Gst.ElementFactory.find(element) is None
        ]
        if missing:
            raise UnsupportedSurfaceSource(
                'required DL Streamer elements are missing: ' + ', '.join(missing)
            )

    sf.log.info("cam[%s] surface path up: vehicle=%s tracker=%s smoke_fire=%s plate=%s ocr=%s face=%s secondaries=%s", name,
                ("GPU/DLStreamer-VA" if dls_authoritative else
                 "GPU/VA-RemoteTensor" if zero_copy_vehicle else veh.exec_devices),
                "gvatrack" if dls_authoritative else "sentinel-centroid",
                ("GPU/DLStreamer-OpenCV-BGR" if dls_fire_enabled else
                 smoke_fire.exec_devices if smoke_fire is not None else "disabled"),
                plate.exec_devices if plate is not None else "disabled",
                async_ocr.actual_device if async_ocr is not None else "disabled",
                "GPU+NPU" if face_pipeline is not None else "disabled",
                "DLStreamer" if dls_secondaries else "direct-openvino")

    from pipeline.camera_state import publish_camera_state
    from pipeline.runtime_status import DecoderRuntimeStatus
    # Analytics initialization is complete even if the live source is currently
    # unavailable. Decoder health and reconnect state are reported separately.
    runtime_backend = 'dlstreamer' if dls_authoritative else 'surface'
    publish_camera_state(name, camera_config, runtime_backend)
    decoder_status = DecoderRuntimeStatus(
        name, str(stream_session_id or "unknown")
    )
    is_rtsp = uri.startswith(('rtsp://', 'rtsps://'))
    is_tcp = uri.startswith('tcp://')
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

    # Keep the primary shared GVA graph at its current fixed batch. The QAFT
    # dynamic source is packaged separately for execution paths supporting it;
    # fixing this copy's batch preserves all trained quantizers and class IDs.
    vehicle_model_path = (
        os.getenv(
            "SENTINEL_GVA_VEHICLE_MODEL",
            "/models/traffic/openvino/vehicle_person_qaft_int8/model_static_b1.xml",
        )
        if dls_authoritative
        else openvino_model_path("vehicle_person_qaft_int8/model")
    )
    vehicle_model_proc = os.getenv(
        "SENTINEL_GVA_VEHICLE_MODEL_PROC",
        "/opt/traffic-pilot/config/vehicle_yolo26_model_proc.json",
    )
    dls_gate = None
    dls_fps_gate = None
    dls_fire_fps_gate = None
    dls_face_interval = max(1, int(os.getenv("SENTINEL_GVA_FACE_INTERVAL", "3")))
    dls_face_metadata = None
    if dls_authoritative and enable_face:
        from pipeline.dlstreamer_bridge import DlsFaceMetadataBridge
        dls_face_metadata = DlsFaceMetadataBridge(dls_face_interval)
    dls_fire_clock = SourcePtsClock()
    dls_fire_state = {
        "index": 0,
        "last_evaluated_monotonic": 0.0,
        "last_hit": False,
    }
    dls_fire_converter = None
    tcp_feeder = None

    native_trace = None

    def build_pipeline():
        nonlocal native_trace
        nonlocal tcp_feeder
        location = json.dumps(uri)
        if is_tcp:
            from urllib.parse import urlsplit
            address = urlsplit(uri)
            if not address.hostname or not address.port:
                raise UnsupportedSurfaceSource('TCP source requires a host and port')
            decoder = f'va{codec}dec' if dls_authoritative else f'vaapi{codec}dec'
            if dls_authoritative:
                # Keep the analytics bin alive across camera outages.  A
                # bounded reconnecting feeder supplies only compressed TS;
                # VAAPI still owns decode and all decoded surfaces.
                src = (
                    'appsrc name=tcpappsrc is-live=true format=bytes '
                    'block=false max-bytes=1048576 leaky-type=none '
                    'caps="video/mpegts,systemstream=(boolean)true,'
                    # The feeder honors this bounded queue without discarding
                    # arbitrary compressed TS chunks. Keep demux timing defaults;
                    # disabling skew correction did not resolve timestamp stalls.
                    'packetsize=(int)188" ! tsdemux name=tcpdemux ! '
                    f'{codec}parse name=videoparser ! {decoder} name=decoder'
                )
            else:
                src = (
                    f'tcpclientsrc host={json.dumps(address.hostname)} '
                    f'port={address.port} timeout=30 ! tsdemux ! '
                    f'{codec}parse ! {decoder} name=decoder'
                )
        elif is_rtsp:
            decoder = f'va{codec}dec' if dls_authoritative else f'vaapi{codec}dec'
            src = (f'rtspsrc location={location} protocols=tcp latency=100 ! '
                   f'rtp{codec}depay ! {codec}parse ! {decoder} name=decoder')
        elif Path(uri).suffix.lower() in ('.mp4', '.mov', '.m4v'):
            decoder = f'va{codec}dec' if dls_authoritative else f'vaapi{codec}dec'
            src = f'filesrc location={location} ! qtdemux ! {codec}parse ! {decoder} name=decoder'
        else:
            decoder = f'va{codec}dec' if dls_authoritative else f'vaapi{codec}dec'
            src = f'filesrc location={location} ! {codec}parse ! {decoder} name=decoder'
        # Keep the live path entirely as VA surfaces. ``videorate`` before the
        # appsink stalls on some live MPEG-TS senders, so source-PTS selection
        # is performed by ``pull_selected_surface`` below. A one-buffer leaky
        # live sink prevents model latency from building a stale decode queue;
        # recorded benchmarks retain every surface.
        # An admitted DL Streamer frame has already advanced motion, tracker
        # and temporal-policy state.  Dropping it at the final appsink leaves
        # an unmatched admission record and can skip scene/evidence gates.
        # Quiet frames are dropped earlier by ``DlsMotionPolicyGate``; apply
        # bounded backpressure here so every admitted PTS reaches policy once.
        sink_drop = (
            'false' if benchmark or dls_authoritative else 'true'
        )
        if dls_authoritative:
            if dls_gate is None:
                raise RuntimeError('DL Streamer motion policy gate is unavailable')
            tracker_type = os.getenv(
                'SENTINEL_GVA_TRACKER_TYPE', 'zero-term'
            )
            # Eight requests covers the qualified eight-camera native graph.
            # Four requests causes the fifth shared GVA branch to fail its
            # PLAYING transition on the deployed Intel stack.
            nireq = min(8, max(1, int(os.getenv('SENTINEL_GVA_NIREQ', '8'))))
            face_chain = ""
            if dls_face_metadata is not None:
                face_detector_model = os.path.join(
                    sf.FACE_MODELS_DIR, "dlstreamer", "scrfd_top1x_u8.xml"
                )
                face_detector_proc = os.getenv(
                    "SENTINEL_GVA_FACE_MODEL_PROC",
                    "/opt/traffic-pilot/config/scrfd_top1_proc.json",
                )
                face_embedding_model = os.path.join(
                    sf.FACE_MODELS_DIR, "dlstreamer", "face_align_u8.xml"
                )
                face_nireq = min(8, max(1, int(os.getenv(
                    "SENTINEL_GVA_FACE_NIREQ", str(nireq)
                ))))
                face_chain = (
                    f'gvadetect name=facedet model={json.dumps(face_detector_model)} '
                    f'model-proc={json.dumps(face_detector_proc)} device=GPU '
                    'pre-process-backend=va-surface-sharing '
                    'inference-region=roi-list object-class=person '
                    f'inference-interval={dls_face_interval} threshold=0.55 '
                    f'nireq={face_nireq} '
                    f'model-instance-id={json.dumps(dlstreamer_model_instance_id("face-det", name))} ! '
                    f'gvaclassify name=faceclass model={json.dumps(face_embedding_model)} '
                    'device=NPU pre-process-backend=opencv object-class=face '
                    f'nireq={face_nireq} '
                    f'model-instance-id={json.dumps(dlstreamer_model_instance_id("face-embed", name))} ! '
                )
            fire_branch = ""
            if dls_fire_enabled:
                fire_model = openvino_model_path("smoke_fire")
                fire_model_proc = os.getenv(
                    "SENTINEL_GVA_FIRE_MODEL_PROC",
                    "/opt/traffic-pilot/config/fire_rgb_model_proc.json",
                )
                fire_fps = max(0.2, float(os.getenv(
                    "SENTINEL_GVA_FIRE_FPS", "2"
                )))
                fire_nireq = min(8, max(1, int(os.getenv(
                    "SENTINEL_GVA_FIRE_NIREQ", "2"
                ))))
                source_head = (
                    # Put every decoder surface through VA post-processing so
                    # all camera branches negotiate the process-wide VA
                    # display context required by a shared GVA model instance.
                    src + ' ! vapostproc ! '
                    'video/x-raw(memory:VAMemory),format=NV12 ! '
                    'tee name=source '
                )
                primary_head = 'source. ! queue max-size-buffers=2 ! '
                fire_branch = (
                    ' source. ! queue leaky=downstream max-size-buffers=2 '
                    'max-size-bytes=0 max-size-time=0 ! '
                    'identity name=firefpsgate ! '
                    # Keep fire/smoke off the shared VA preprocessor.  The
                    # qualified DL Streamer cascade uses this bounded 320x320
                    # system-memory branch; running primary, face and fire VA
                    # preprocessors concurrently eventually exhausts iHD
                    # vaRenderPicture resources on the deployed Intel stack.
                    'vapostproc add-borders=true ! '
                    'video/x-raw,format=BGRx,width=320,height=320 ! '
                    'videoconvert ! video/x-raw,format=BGR ! '
                    f'gvadetect name=firedet model={json.dumps(fire_model)} '
                    f'model-proc={json.dumps(fire_model_proc)} device=GPU '
                    'pre-process-backend=opencv threshold=0.35 '
                    f'nireq={fire_nireq} '
                    f'model-instance-id={json.dumps(dlstreamer_model_instance_id("fire", name))} ! '
                    'appsink name=firesink sync=false async=false '
                    'max-buffers=2 drop=true '
                    'wait-on-eos=false enable-last-sample=false'
                )
            else:
                source_head = (
                    src + ' ! vapostproc ! '
                    'video/x-raw(memory:VAMemory),format=NV12 ! '
                )
                primary_head = ''
            desc = (
                source_head + primary_head +
                'identity name=fpsgate ! '
                'gvamotiondetect name=motion pixel-diff-threshold=15 '
                'motion-threshold=0.05 block-size=64 confirm-frames=1 '
                'min-persistence=1 max-miss=0 min-rel-area=0.0005 ! '
                'identity name=policygate ! '
                # Every admitted PTS must produce exactly one tracked output.
                # Backpressure this small queue instead of silently discarding
                # an admission after the policy gate recorded it.
                'queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! '
                f'gvadetect name=primarydet model={json.dumps(vehicle_model_path)} '
                f'model-proc={json.dumps(vehicle_model_proc)} device=GPU '
                'pre-process-backend=va-surface-sharing '
                f'threshold={vehicle_confidence} nireq={nireq} '
                'batch-size=1 '
                f'model-instance-id={json.dumps(dlstreamer_model_instance_id("vehicle", name))} ! '
                f'gvatrack tracking-type={tracker_type} ! '
                + face_chain +
                f'appsink name=vasink sync=false async=false '
                # Capacity is reserved upstream before inference, not inside
                # GVA's shared completion callback. Never drop an admission.
                f'max-buffers={dls_gate.maximum_pending} drop={sink_drop} '
                'wait-on-eos=false enable-last-sample=false'
                + fire_branch
            )
        else:
            desc = (src + ' ! '
                    'video/x-raw(memory:VASurface),format=NV12,width=1920,height=1080 ! '
                    f'appsink name=vasink sync=false max-buffers=1 drop={sink_drop} '
                    'wait-on-eos=false enable-last-sample=false')
        pipeline = (
            gst_coordinator.create_branch(name, desc)
            if gst_coordinator is not None
            else Gst.parse_launch(desc)
        )
        if dls_authoritative and os.getenv("SENTINEL_DLS_TRACE_PROGRESS", "0") == "1":
            from pipeline.native_progress import NativeProgressTrace
            if native_trace is not None:
                native_trace.close()
            native_trace = NativeProgressTrace(name, stream_session_id)
            native_trace.attach(pipeline, Gst)
        if dls_authoritative:
            fps_gate_pad = pipeline.get_by_name('fpsgate').get_static_pad('sink')
            fps_gate_pad.add_probe(Gst.PadProbeType.BUFFER, dls_fps_gate.probe)
            gate_pad = pipeline.get_by_name('policygate').get_static_pad('sink')
            gate_pad.add_probe(Gst.PadProbeType.BUFFER, dls_gate.probe)
            if dls_fire_fps_gate is not None:
                fire_fps_pad = pipeline.get_by_name(
                    'firefpsgate'
                ).get_static_pad('sink')
                fire_fps_pad.add_probe(
                    Gst.PadProbeType.BUFFER, dls_fire_fps_gate.probe
                )
            if dls_face_metadata is not None:
                face_pad = pipeline.get_by_name('faceclass').get_static_pad('src')
                face_pad.add_probe(
                    Gst.PadProbeType.BUFFER, dls_face_metadata.probe
                )
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError('surface pipeline failed')
        if is_tcp and dls_authoritative and tcp_feeder is None:
            from urllib.parse import urlsplit
            from pipeline.dlstreamer_bridge import TcpMpegTsFeeder
            address = urlsplit(uri)
            tcp_feeder = TcpMpegTsFeeder(
                pipeline.get_by_name('tcpappsrc'),
                address.hostname,
                address.port,
                camera_id=name,
                stream_session_id=stream_session_id,
            )
            tcp_feeder.start()
        return pipeline

    def drain_dls_fire(pipeline) -> int:
        nonlocal dls_fire_converter
        if not dls_fire_enabled or pipeline is None:
            return 0
        sink = pipeline.get_by_name('firesink')
        drained = 0
        while True:
            sample = sink.emit('try-pull-sample', 0)
            if sample is None:
                break
            drained += 1
            received_at = time.time()
            pts = int(sample.get_buffer().pts)
            if pts == (1 << 64) - 1:
                continue
            from pipeline.dlstreamer_bridge import hazards_from_sample
            observed_at = dls_fire_clock.observe(
                pts, received_at, clock_hz=float(Gst.SECOND)
            )
            with telemetry.measure(
                "fire_smoke.detection.metadata_bridge",
                device="GPU/DLStreamer",
            ):
                detections = hazards_from_sample(sample)
            telemetry.count("fire_model_system_memory_frames")
            fire_pixels_required = (
                v8_pipeline is None
                or v8_pipeline.hazard_pixels_required(detections, observed_at)
            )
            if fire_pixels_required:
                if dls_fire_converter is None:
                    dls_fire_converter = True
                with telemetry.measure(
                    "frame_access.fire_evidence_bgr",
                    device="CPU/map",
                ):
                    frame = _map_bgr_any(sample)
                stats['bgr_for_fire_policy'] += 1
                telemetry.count("fire_bgr_materialized_frames")
            else:
                frame = _geometry_only_frame(sample)
                stats['bgr_fire_metadata_only_frames'] += 1
                telemetry.count("fire_bgr_avoided_frames")
            dls_fire_state["index"] += 1
            dls_fire_state["last_evaluated_monotonic"] = time.monotonic()
            dls_fire_state["last_hit"] = bool(detections)
            if detections:
                dls_gate.observe_models(False, True, fully_evaluated=True)
            packet = _FramePacket(
                index=f"{dls_fire_state['index']}-{pts}",
                name=name,
                frame=frame,
                frame_observed_at=observed_at,
                frame_received_at=received_at,
            )
            packet.detections = detections
            packet.analytics_state["fire_smoke_evaluated"] = True
            with telemetry.measure("fire_smoke.policy"):
                v8_pipeline.process_hazards(packet)
            stats['smoke'] += 1
            stats['smoke_detections'] += len(detections)
        return drained

    def pull_surface(
        pipeline, *, nonblocking=False, timeout_seconds: float = 10.0
    ):
        bus = pipeline.get_bus()
        def pull(sink):
            if nonblocking or not dls_authoritative:
                timeout_ns = 0 if nonblocking else int(
                    max(0.1, float(timeout_seconds)) * Gst.SECOND
                )
                sample = sink.emit('try-pull-sample', timeout_ns)
                if sample is not None:
                    return sample
                if nonblocking:
                    return None
            else:
                # A quiet DL Streamer camera intentionally drops buffers before
                # gvadetect, so the inference appsink may be silent while the
                # decoder remains healthy. Wake periodically to advance quiet
                # lifecycle/health state and time out only when decode progress
                # itself stops.
                last_progress = time.monotonic()
                decoded = dls_gate.decoded_frames
                while True:
                    sample = sink.emit(
                        'try-pull-sample', int(0.5 * Gst.SECOND)
                    )
                    drain_dls_fire(pipeline)
                    _consume_dls_quiet()
                    if sample is not None:
                        return sample
                    current = dls_gate.decoded_frames
                    if current != decoded:
                        decoded = current
                        last_progress = time.monotonic()
                    if time.monotonic() - last_progress < float(timeout_seconds):
                        continue
                    break
            msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if msg:
                # Preserve the native failure reason for reconnect diagnostics,
                # but redact source URLs/tokens before it reaches status or logs.
                from pipeline.structured_logging import sanitize_text
                error, debug = msg.parse_error()
                detail = sanitize_text(
                    f"{error}; {debug or ''}", limit=1024
                )
                raise RuntimeError(f'surface decoder error: {detail}')
            if sink.get_property('eos'):
                return None
            raise RuntimeError('surface decoder timeout')
        started_pull = time.perf_counter()
        if native_trace is not None:
            native_trace.phase("await_primary_output")
        va = pull(pipeline.get_by_name('vasink'))
        if native_trace is not None and va is not None:
            native_trace.mark("camera.consumed", int(va.get_buffer().pts))
            native_trace.phase("frame_processing")
        drain_dls_fire(pipeline)
        if measured_started is not None and va is not None:
            stats['surface_pull_seconds'] += time.perf_counter() - started_pull
        if va is not None and int(va.get_buffer().pts) == (1 << 64) - 1:
            raise RuntimeError('surface timestamp missing')
        return va

    pts_sampler = None if (benchmark or dls_authoritative) else SourcePtsSampler(
        fps, clock_hz=float(Gst.SECOND)
    )

    def pull_selected_surface(
        pipeline, *, nonblocking=False, timeout_seconds: float = 10.0
    ):
        """Return the next surface admitted at the configured source FPS."""
        while True:
            sample = pull_surface(
                pipeline,
                nonblocking=nonblocking,
                timeout_seconds=timeout_seconds,
            )
            if sample is None or pts_sampler is None:
                return sample
            if pts_sampler.accept(int(sample.get_buffer().pts)):
                return sample
            stats['source_rate_dropped_frames'] += 1
            if nonblocking:
                return None

    pipeline = bridge = gate = motion = converter = sv = sb = None
    surface_vehicle_detector = shadow_comparator = None
    source_clock = SourcePtsClock()
    if dls_authoritative:
        from pipeline.dlstreamer_bridge import DlsMotionPolicyGate, DlsPtsRateGate
        dls_fps_gate = DlsPtsRateGate(fps, int(Gst.SECOND))
        dls_gate = DlsMotionPolicyGate(
            switch, telemetry, source_clock, fps, int(Gst.SECOND)
        )
        if dls_fire_enabled:
            dls_fire_fps_gate = DlsPtsRateGate(
                max(0.2, float(os.getenv("SENTINEL_GVA_FIRE_FPS", "2"))),
                int(Gst.SECOND),
            )

    def _consume_dls_quiet() -> int:
        if not dls_authoritative or dls_gate is None:
            return 0
        if dls_fps_gate is not None:
            telemetry.set_gauge(
                "dls_rate_gate_accepted_frames", dls_fps_gate.accepted
            )
            telemetry.set_gauge(
                "dls_rate_gate_dropped_frames", dls_fps_gate.dropped
            )
        if dls_fire_fps_gate is not None:
            telemetry.set_gauge(
                "fire_rate_gate_accepted_frames", dls_fire_fps_gate.accepted
            )
            telemetry.set_gauge(
                "fire_rate_gate_dropped_frames", dls_fire_fps_gate.dropped
            )
        if tcp_feeder is not None:
            telemetry.set_gauge(
                "tcp_feeder_connections", tcp_feeder.connections
            )
            telemetry.set_gauge(
                "tcp_feeder_disconnects", tcp_feeder.disconnects
            )
            telemetry.set_gauge(
                "tcp_feeder_bytes_received", tcp_feeder.bytes_received
            )
        records = dls_gate.drain_quiet()
        for quiet_record in records:
            stats['frames'] += 1
            stats['quiet_frames'] += 1
            decoder_status.frame(quiet_record.received_at)
            if v8_pipeline is not None:
                with telemetry.measure("event_policy.quiet_lifecycle"):
                    v8_pipeline.process_quiet(
                        quiet_record.observed_at,
                        quiet_record.index,
                    )
        return len(records)
    next_surface = None
    next_bgr_pending = False
    try:
        while True:
            try:
                if pipeline is None:
                    log.info('cam[%s] opening surface decoder', name)
                    decoder_status.increment(
                        'decoder_starts', state='connecting',
                        reason='gstreamer_started', decoder_pid=None,
                        last_ffmpeg_exit_code=None, retry_reason=None,
                        retry_backoff_seconds=0.0, sanitized_stderr=None,
                    )
                    pipeline = build_pipeline()
                    # A live H.264 client may connect just after an IDR and
                    # cannot produce a decoded surface until the next one.
                    # Keep ordinary stall detection at ten seconds, but allow
                    # the first surface (including after reconnect) one full
                    # long-GOP acquisition window.
                    first_surface_timeout = min(60.0, max(
                        10.0,
                        float(os.getenv(
                            'SENTINEL_SURFACE_FIRST_FRAME_TIMEOUT_SECONDS',
                            '35',
                        )),
                    ))
                    sv = pull_selected_surface(
                        pipeline, timeout_seconds=first_surface_timeout
                    )
                    if sv is None:
                        raise RuntimeError('surface decoder produced no frames')
                    caps = sv.get_caps()
                    info = caps.get_structure(0)
                    width = int(info.get_value('width'))
                    height = int(info.get_value('height'))
                    fmt = str(info.get_value('format'))
                    if dls_authoritative:
                        features = caps.get_features(0)
                        if (fmt != 'NV12'
                                or not features.contains('memory:VAMemory')):
                            raise UnsupportedSurfaceSource(
                                'DL Streamer output must remain NV12 VAMemory after tracking'
                            )
                        converter = VAMemorySelectiveBgrConverter(
                            caps, stats, label="primary"
                        )
                        log.info(
                            'cam[%s] DL Streamer retained-surface path ready at %dx%d',
                            name, width, height,
                        )
                    else:
                        features = caps.get_features(0)
                        if (width, height) != (FULL_W, FULL_H):
                            raise UnsupportedSurfaceSource(
                                'surface path requires 1920x1080 video'
                            )
                        if fmt != 'NV12' or not features.contains('memory:VASurface'):
                            raise UnsupportedSurfaceSource(
                                'surface path requires NV12 VA surfaces'
                            )
                        log.info('cam[%s] binding surface GPU resources', name)
                        bridge = VaBridge(hash(pipeline.get_by_name('decoder')))
                        gate = SurfaceGate(bridge.context())
                        if zero_copy_vehicle or zero_copy_shadow_enabled:
                            try:
                                from pipeline.surface_vehicle import SurfaceVehicleDetector
                                surface_vehicle_detector = SurfaceVehicleDetector(
                                    hash(pipeline.get_by_name('decoder')),
                                    vehicle_model_path,
                                    confidence=(
                                        vehicle_confidence if zero_copy_vehicle else
                                        float(os.getenv(
                                            "SENTINEL_ZERO_COPY_SHADOW_CONFIDENCE",
                                            str(vehicle_confidence),
                                        ))
                                    ),
                                    class_ids=sf.VEHICLE_CLASS_IDS,
                                    class_names=sf.VEHICLE_CLASS_NAMES,
                                )
                                if zero_copy_shadow_enabled and not zero_copy_vehicle:
                                    from pipeline.zero_copy_shadow import ZeroCopyShadowComparator
                                    shadow_comparator = ZeroCopyShadowComparator(
                                        name, telemetry=telemetry
                                    )
                                telemetry.devices[
                                    "zero_copy_vehicle_detection"
                                    if zero_copy_vehicle else "zero_copy_shadow_detection"
                                ] = surface_vehicle_detector.exec_devices
                            except RuntimeError as exc:
                                if zero_copy_vehicle:
                                    raise UnsupportedSurfaceSource(
                                        f'zero-copy vehicle detector unavailable: {exc}'
                                    ) from exc
                                log.warning(
                                    'cam[%s] zero-copy shadow unavailable: %s', name, exc
                                )
                                surface_vehicle_detector = shadow_comparator = None
                        converter = SelectiveBgrConverter(
                            pipeline.get_by_name('decoder'), sv.get_caps(), stats)
                        # Explicit warm-up does not consume input frames. The first sample
                        # stays retained, is counted once and processed again after timing starts.
                        sid = bridge.surface_id(sv)
                        warm_frame = _map_bgr(converter.convert(sv, measured=False))
                        if surface_vehicle_detector is not None:
                            try:
                                surface_vehicle_detector.detect_surface(sid)
                            except RuntimeError as exc:
                                if zero_copy_vehicle:
                                    raise UnsupportedSurfaceSource(
                                        f'zero-copy vehicle warm-up failed: {exc}'
                                    ) from exc
                                log.warning(
                                    'cam[%s] zero-copy shadow warm-up failed; '
                                    'authoritative detector remains active: %s',
                                    name, exc,
                                )
                                surface_vehicle_detector.close()
                                surface_vehicle_detector = None
                                if shadow_comparator is not None:
                                    shadow_comparator.close()
                                    shadow_comparator = None
                        for _ in range(3):
                            if veh is not None:
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
                    if v8_pipeline is not None:
                        v8_pipeline.camera_status('healthy', 'none')
                    if measured_started is None:
                        if on_ready is not None:
                            on_ready()
                        measured_started = time.perf_counter()
                else:
                    if next_surface is not None:
                        sv, next_surface = next_surface, None
                    else:
                        sv = pull_selected_surface(pipeline)
                if sv is None:
                    if benchmark:
                        break
                    raise RuntimeError('surface source ended')
                pts = int(sv.get_buffer().pts)
                sb = None
                dls_record = dls_gate.take(pts) if dls_authoritative else None
                if dls_record is not None:
                    frame_received_at = dls_record.received_at
                    frame_observed_at = dls_record.observed_at
                else:
                    frame_received_at = time.time()
                    frame_observed_at = source_clock.observe(
                        pts, frame_received_at, clock_hz=float(Gst.SECOND)
                    )
                frame_processing_started = time.perf_counter()
                if frame_received_at - last_config_check >= sf.CAMERA_CONFIG_RELOAD_INTERVAL_S:
                    last_config_check = frame_received_at
                    previous = config_mtime
                    config_mtime = sf._refresh_camera_config_if_changed(name, camera_config, config_mtime)
                    if config_mtime != previous:
                        if v8_pipeline is not None:
                            v8_pipeline.reconfigure(camera_config)
                        publish_camera_state(name, camera_config, runtime_backend)
                if dls_authoritative:
                    gate_state = dls_record.state
                    gate_ran = True
                    quiet_pixels = False
                    # Detection/tracking metadata is inspected first. The
                    # retained VA sample is converted later only when an
                    # unchanged policy/evidence consumer requires pixels.
                    sb = None
                else:
                    gate_state, gate_ran = motion.update(
                        sv, fps, pts / Gst.SECOND
                    )
                    quiet_pixels = (not gate_ran and v8_pipeline is not None
                                    and v8_pipeline.quiet_frame_needed(frame_observed_at))
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
                        next_surface = pull_selected_surface(
                            pipeline, nonblocking=True
                        )
                        if next_surface is not None:
                            converter.submit(next_surface)
                            next_bgr_pending = True
                            stats['bgr_prefetch_submissions'] += 1
            except RuntimeError as exc:
                if benchmark:
                    raise
                from pipeline.structured_logging import sanitize_text
                safe_error = sanitize_text(exc, limit=512)
                if v8_pipeline is not None:
                    v8_pipeline.camera_status('unavailable', 'stream_disconnected')
                decoder_status.increment(
                    'decoder_errors', state='unavailable',
                    reason='stream_disconnected',
                    retry_reason=type(exc).__name__,
                    retry_backoff_seconds=1.0,
                    sanitized_stderr=safe_error,
                )
                decoder_status.increment(
                    'reconnects', state='retrying',
                    reason='stream_disconnected',
                    retry_reason=type(exc).__name__,
                    retry_backoff_seconds=1.0,
                )
                log.warning(
                    'cam[%s] surface source interrupted; reconnecting: %s',
                    name, safe_error,
                )
                permanent_tcp_branch = bool(
                    dls_authoritative and is_tcp and tcp_feeder is not None
                )
                # BGR model contexts are independent of the decoder and stay warm.
                sv = sb = next_surface = None
                next_bgr_pending = False
                motion = None
                if converter is not None and not permanent_tcp_branch:
                    converter.close()
                    converter = None
                if (dls_fire_converter is not None
                        and not permanent_tcp_branch):
                    dls_fire_converter.close()
                    dls_fire_converter = None
                # Native consumers retain resources created from the decoder's
                # VA display. Destroy them before the bridge and decoder.
                if surface_vehicle_detector is not None:
                    surface_vehicle_detector.close()
                    surface_vehicle_detector = None
                if shadow_comparator is not None:
                    shadow_comparator.close()
                    shadow_comparator = None
                log.info('cam[%s] releasing gate', name)
                if gate is not None:
                    gate.close()
                    gate = None
                log.info('cam[%s] releasing VA bridge', name)
                if bridge is not None:
                    bridge.close()
                    bridge = None
                log.info('cam[%s] closing surface decoder', name)
                restart_in_place = bool(
                    not permanent_tcp_branch
                    and dls_authoritative
                    and gst_coordinator is not None
                    and pipeline is not None
                )
                if restart_in_place:
                    pipeline.restart()
                if (pipeline is not None and not restart_in_place
                        and not permanent_tcp_branch):
                    pipeline.set_state(Gst.State.NULL)
                    pipeline = None
                source_clock = SourcePtsClock()
                dls_fire_clock = SourcePtsClock()
                dls_fire_state.update(
                    index=0, last_evaluated_monotonic=0.0, last_hit=False
                )
                if dls_authoritative:
                    switch.reset()
                    if dls_fps_gate is not None:
                        dls_fps_gate.reset()
                    if dls_fire_fps_gate is not None:
                        dls_fire_fps_gate.reset()
                    if dls_face_metadata is not None:
                        dls_face_metadata.clear()
                    if restart_in_place or permanent_tcp_branch:
                        dls_gate.reset_session(source_clock)
                    else:
                        from pipeline.dlstreamer_bridge import DlsMotionPolicyGate
                        dls_gate = DlsMotionPolicyGate(
                            switch, telemetry, source_clock, fps, int(Gst.SECOND)
                        )
                    if hasattr(tracker, 'clear'):
                        tracker.clear()
                if pts_sampler is not None:
                    pts_sampler.next_pts = None
                    pts_sampler.last_pts = None
                if not ready:
                    startup_failures += 1
                    if startup_failures >= max_startup_failures:
                        raise UnsupportedSurfaceSource(
                            f"surface decoder failed during startup after {startup_failures} attempts")
                time.sleep(1)
                continue
            if dls_authoritative:
                _consume_dls_quiet()
                fidx = dls_record.index
            else:
                fidx += 1
            stats['frames'] += 1
            # Decoder freshness is based on when this process received a real
            # frame, not model execution or source PTS.  Motion-gated quiet
            # frames are healthy decoder progress and must not look stale.
            decoder_status.frame(frame_received_at)
            if dls_authoritative:
                telemetry.inference_succeeded()
            else:
                telemetry.frame(inferred=gate_ran)
            if not gate_ran:
                stats['quiet_frames'] += 1
                if v8_pipeline is not None:
                    frame = None
                    if quiet_pixels:
                        stats['bgr_for_quiet_scene'] += 1
                        frame = _map_bgr(sb)
                    with telemetry.measure("event_policy.quiet_lifecycle"):
                        v8_pipeline.process_quiet(frame_observed_at, fidx, frame)
            else:
                stats['woken_frames'] += 1
                if dls_authoritative:
                    from pipeline.dlstreamer_bridge import detections_from_sample
                    with telemetry.measure(
                        "detection.vehicle_person.metadata_bridge"
                    ):
                        raw_vdets = detections_from_sample(
                            sv, sf.VEHICLE_CLASS_NAMES
                        )
                    pixels_required = (
                        not enable_v8
                        or v8_pipeline.primary_pixels_required(
                            frame_observed_at, raw_vdets
                        )
                    )
                    if pixels_required:
                        with telemetry.measure(
                            "frame_conversion.primary_selective_bgr",
                            device="GPU/VAAPI",
                        ):
                            sb = converter.convert(sv)
                            frame = _map_bgr_any(sb)
                        stats['bgr_for_inference'] += 1
                        telemetry.count("primary_bgr_materialized_frames")
                    else:
                        frame = _geometry_only_frame(sv)
                        stats['bgr_metadata_only_frames'] += 1
                        telemetry.count("primary_bgr_avoided_frames")
                elif zero_copy_vehicle:
                    frame = _map_bgr(sb)
                    stats['bgr_for_inference'] += 1
                    try:
                        with telemetry.measure(
                            "detection.vehicle_person",
                            device=surface_vehicle_detector.exec_devices,
                        ):
                            raw_vdets = surface_vehicle_detector.detect_surface(
                                bridge.surface_id(sv)
                            )
                    except RuntimeError as exc:
                        raise UnsupportedSurfaceSource(
                            f'zero-copy vehicle inference failed: {exc}'
                        ) from exc
                    if (
                        zero_copy_wake_guard
                        and gate_state == "MOTION"
                        and not raw_vdets
                    ):
                        with telemetry.measure(
                            "detection.vehicle_person_wake_guard",
                            device=sf.VEHICLE_DEVICE,
                        ):
                            raw_vdets = veh.detect(frame)
                        stats['zero_copy_wake_guard_checks'] += 1
                        if raw_vdets:
                            stats['zero_copy_wake_guard_hits'] += 1
                else:
                    frame = _map_bgr(sb)
                    stats['bgr_for_inference'] += 1
                    with telemetry.measure(
                        "detection.vehicle_person", device=sf.VEHICLE_DEVICE
                    ):
                        raw_vdets = veh.detect(frame)
                    if surface_vehicle_detector is not None:
                        try:
                            with telemetry.measure(
                                "detection.zero_copy_shadow",
                                device=surface_vehicle_detector.exec_devices,
                            ):
                                shadow_vdets = surface_vehicle_detector.detect_surface(
                                    bridge.surface_id(sv)
                                )
                            shadow_comparator.submit_authoritative(pts, raw_vdets)
                            shadow_comparator.submit_shadow(pts, shadow_vdets)
                        except RuntimeError as exc:
                            log.warning(
                                'cam[%s] zero-copy shadow disabled after error: %s',
                                name, exc,
                            )
                            surface_vehicle_detector.close()
                            surface_vehicle_detector = None
                            shadow_comparator.close()
                            shadow_comparator = None
                vdets = raw_vdets
                vdets = deduplicate_vehicles(vdets)
                dets = list(vdets)
                if plate is not None:
                    for vd in vdets:
                        if vd.class_name == "pedestrian":
                            continue
                        x1, y1, x2, y2 = vd.bbox
                        crop = frame[y1:y2, x1:x2]
                        if native_trace is not None:
                            native_trace.phase("plate_detection")
                        with telemetry.measure("anpr.plate_detection", device=sf.PLATE_DEVICE):
                            plate_results = plate.detect(crop)
                            if dls_secondaries:
                                for stage, seconds in plate.last_timings.items():
                                    telemetry.observe_latency("anpr.plate_" + stage, seconds)
                                telemetry.observe_latency(
                                    "anpr.plate_scheduler_queue_wait",
                                    plate.engine.queue_wait_seconds,
                                )
                                telemetry.observe_latency(
                                    "anpr.plate_executor_wall_time",
                                    plate.engine.device_inference_seconds,
                                )
                            for pd in plate_results:
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
                        if native_trace is not None:
                            native_trace.phase("frame_processing")
                smoke_dets = []
                stats['vehicle'] += 1
                stats['vehicle_detections'] += len(vdets)
                smoke_evaluated = sf.should_evaluate_fire_smoke(
                    smoke_fire, fidx, idle_sweep=gate_state == "MOTION"
                )
                if smoke_evaluated:
                    try:
                        with telemetry.measure(
                            "fire_smoke.detection", device=smoke_fire.exec_devices
                        ):
                            smoke_dets = smoke_fire.detect(frame)
                        stats['smoke'] += 1
                        stats['smoke_detections'] += len(smoke_dets)
                        dets.extend(smoke_dets)
                    except RuntimeError as exc:
                        smoke_evaluated = False
                        sf.log.warning("cam[%s] fire/smoke inference skipped: %s", name, exc)
                if dls_authoritative:
                    fire_recent = bool(
                        dls_fire_enabled
                        and time.monotonic()
                        - float(dls_fire_state["last_evaluated_monotonic"])
                        <= 1.0
                    )
                    released = dls_gate.observe_models(
                        bool(vdets),
                        bool(smoke_dets) or (
                            fire_recent and bool(dls_fire_state["last_hit"])
                        ),
                        fully_evaluated=(
                            not dls_fire_enabled
                            or fire_recent
                        ),
                    )
                else:
                    released = motion.observe_models(
                        sv, bool(vdets), bool(smoke_dets),
                        fully_evaluated=(smoke_fire is None or smoke_evaluated),
                    )
                packet = _FramePacket(index=fidx, name=name, frame=frame,
                                      frame_observed_at=frame_observed_at,
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
                    if dls_face_metadata is not None:
                        face_evaluated, face_candidates = dls_face_metadata.take(pts)
                        face_extractor.prepare(face_evaluated, face_candidates)
                    with telemetry.measure("person_identity.face_pipeline"):
                        face_samples = face_pipeline.process(packet, tracked_people)
                if v8_pipeline is not None:
                    if native_trace is not None:
                        native_trace.phase("event_policy")
                    with telemetry.measure("event_policy.active_frame"):
                        v8_pipeline.process(packet, face_samples)
                if not enable_v8:
                    analytics.publish_packets([packet])
                stats['packets'] += 1
                if released:
                    if hasattr(tracker, 'clear'):
                        tracker.clear()
                    elif hasattr(tracker, '_trackers_by_source'):
                        tracker._trackers_by_source.clear()
            telemetry.observe_latency(
                "pipeline.frame_processing_total",
                time.perf_counter() - frame_processing_started,
            )
            if not ready:
                publish_camera_state(name, camera_config, runtime_backend)
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
        stats['pending_trigger_at_end'] = int(
            False if dls_authoritative else motion.pending
        )
        result = {
            'counts': dict(stats),
            'frames': fidx,
            'processing_seconds': elapsed,
            'fps': fidx / elapsed,
            'switches': switch.switches,
            'sweeps': switch.sweeps,
            'smoke_enabled': smoke_fire is not None,
        }
        if shadow_comparator is not None:
            result['zero_copy_shadow'] = shadow_comparator.snapshot()
        return result
    finally:
        if native_trace is not None:
            native_trace.close()
        if dls_gate is not None:
            dls_gate.close()
        if async_ocr is not None and hasattr(async_ocr, "close"):
            async_ocr.close()
        for extractor in (body_extractor, gait_extractor, scene_extractor):
            if extractor is not None and hasattr(extractor, "close"):
                extractor.close()
        telemetry.close()
        analytics.close()
        sv = sb = next_surface = None
        if converter is not None:
            converter.close()
        if dls_fire_converter is not None:
            dls_fire_converter.close()
        if surface_vehicle_detector is not None:
            surface_vehicle_detector.close()
        if shadow_comparator is not None:
            shadow_comparator.close()
        if gate is not None:
            gate.close()
        if bridge is not None:
            bridge.close()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if tcp_feeder is not None:
            tcp_feeder.close()
