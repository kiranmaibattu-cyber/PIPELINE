"""DL Streamer metadata boundary for the authoritative V8 primary path.

DL Streamer owns VA-memory preprocessing, vehicle/person inference and local
GVA tracking.  This module translates its ROI metadata into Sentinel's stable
``Detection`` contract.  It deliberately does not create presences or events;
those decisions remain in the existing camera-local production policy.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import os
import socket
import threading
import time

import cv2
import numpy as np

from pipeline.structured_logging import log_event


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdmittedFrame:
    index: int
    pts: int
    received_at: float
    observed_at: float
    state: str
    reason: str


@dataclass(frozen=True)
class DlsFaceCandidate:
    """One copied face result before ``vapostproc`` drops tensor metadata."""

    bbox: tuple[int, int, int, int]
    embedding: np.ndarray
    detector_confidence: float
    track_id: int


class TcpMpegTsFeeder:
    """Reconnect a compressed MPEG-TS socket behind a permanent appsrc.

    Only compressed transport-stream bytes cross Python.  Decode remains in
    the existing VAAPI branch, and the parent graph's shared GVA elements are
    never removed when the camera server disappears.
    """

    def __init__(
        self,
        appsrc,
        host: str,
        port: int,
        *,
        camera_id: str,
        stream_session_id: str | None = None,
        retry_seconds: float = 1.0,
    ):
        self.appsrc = appsrc
        self.host = str(host)
        self.port = int(port)
        self.camera_id = str(camera_id)
        self.stream_session_id = (
            str(stream_session_id) if stream_session_id else None
        )
        self.retry_seconds = max(0.2, float(retry_seconds))
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._lock = threading.Lock()
        self.connections = 0
        self.disconnects = 0
        self.bytes_received = 0
        self._thread = threading.Thread(
            target=self._run,
            name=f"sentinel-tcp-feed-{self.host}-{self.port}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _wait_for_capacity(self) -> bool:
        # One producer, <=64 KiB per receive: queue usage is bounded by
        # max-bytes plus one chunk. Avoid a blocking push-buffer so close()
        # can cancel this wait even if a native downstream stage is stuck.
        maximum = int(self.appsrc.get_property("max-bytes"))
        if maximum <= 0:
            raise RuntimeError("TCP appsrc requires a positive queue byte bound")
        while int(self.appsrc.get_property("current-level-bytes")) >= maximum:
            if self._stop.wait(.01):
                return False
        return not self._stop.is_set()

    def _run(self) -> None:
        from gi.repository import Gst

        while not self._stop.is_set():
            stream = None
            try:
                stream = socket.create_connection(
                    (self.host, self.port), timeout=5.0
                )
                # Some file-backed or long-GOP senders pause briefly while
                # reopening a loop.  Decoder freshness still degrades after
                # ten seconds; keep the compressed socket alive long enough
                # to distinguish that pause from a closed connection.
                stream.settimeout(30.0)
                with self._lock:
                    self._socket = stream
                    self.connections += 1
                    connection_number = self.connections
                log_event(
                    logger,
                    logging.INFO,
                    "tcp_source_connected",
                    "TCP MPEG-TS source connected",
                    camera_id=self.camera_id,
                    stream_session_id=self.stream_session_id,
                    connection_number=connection_number,
                )
                while not self._stop.is_set():
                    if not self._wait_for_capacity():
                        break
                    payload = stream.recv(64 * 1024)
                    if not payload:
                        raise ConnectionError("TCP MPEG-TS source closed")
                    buffer = Gst.Buffer.new_allocate(None, len(payload), None)
                    buffer.fill(0, payload)
                    flow = self.appsrc.emit("push-buffer", buffer)
                    if flow not in {Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING}:
                        raise RuntimeError(f"appsrc push failed: {flow.value_nick}")
                    self.bytes_received += len(payload)
            except (OSError, ConnectionError, RuntimeError) as exc:
                self.disconnects += 1
                if not self._stop.is_set():
                    log_event(
                        logger,
                        logging.WARNING,
                        "tcp_source_disconnected",
                        "TCP MPEG-TS source disconnected; retry scheduled",
                        camera_id=self.camera_id,
                        stream_session_id=self.stream_session_id,
                        retry_reason=type(exc).__name__,
                        retry_detail=str(exc),
                        retry_backoff_seconds=self.retry_seconds,
                        disconnect_count=self.disconnects,
                    )
            finally:
                with self._lock:
                    if self._socket is stream:
                        self._socket = None
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            self._stop.wait(self.retry_seconds)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            stream = self._socket
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._thread.join(timeout=6.0)


class DlsPtsRateGate:
    """Drop surplus live buffers without rewriting their source PTS.

    ``gvafpsthrottle`` is a pacing element: on the qualified Intel build it
    queues a 25-FPS source and releases every frame at the requested rate.  It
    therefore makes a 4-FPS analytics graph process stale video with PTS that
    advance at only 4/25 real time.  This probe performs selection only; an
    accepted VA surface keeps its original timestamp and pixel storage.
    """

    def __init__(self, fps: float, gst_second: int):
        if fps <= 0 or gst_second <= 0:
            raise ValueError("DL Streamer PTS rate gate requires positive FPS")
        self.interval = max(1, int(round(float(gst_second) / float(fps))))
        self._lock = threading.Lock()
        self._next_pts: int | None = None
        self._last_pts: int | None = None
        self.accepted = 0
        self.dropped = 0
        self._trace_rate = os.getenv("SENTINEL_DLS_TRACE_RATE_GATE", "0") == "1"
        self._received = 0

    def reset(self) -> None:
        with self._lock:
            self._next_pts = None
            self._last_pts = None

    def probe(self, _pad, info):
        from gi.repository import Gst

        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        pts = int(buffer.pts)
        if pts == (1 << 64) - 1:
            self.dropped += 1
            return Gst.PadProbeReturn.DROP
        with self._lock:
            self._received += 1
            if self._trace_rate and self._received % 20 == 0:
                logger.info("DL Streamer rate-gate input", extra={
                    "event": "dls_rate_gate_input",
                    "graph_scope": _pad.get_parent_element().get_parent().get_name(),
                    "pts_ns": pts,
                    "previous_pts_ns": self._last_pts,
                    "next_accept_pts_ns": self._next_pts,
                    "received": self._received,
                    "accepted": self.accepted,
                    "dropped": self.dropped,
                })
            if self._last_pts is not None and pts < self._last_pts:
                # New media epoch after a reconnect or looping qualification
                # source.  Accept its first frame immediately.
                self._next_pts = None
            self._last_pts = pts
            if self._next_pts is None:
                self._next_pts = pts + self.interval
                self.accepted += 1
                return Gst.PadProbeReturn.OK
            if pts < self._next_pts:
                self.dropped += 1
                return Gst.PadProbeReturn.DROP
            while self._next_pts <= pts:
                self._next_pts += self.interval
            self.accepted += 1
            return Gst.PadProbeReturn.OK


class DlsMotionPolicyGate:
    """Apply Sentinel's temporal motion policy before ``gvadetect``.

    ``gvamotiondetect`` supplies only the instantaneous VA-memory motion vote.
    The existing ``SurfaceSwitch`` still owns confirmation, release and the
    five-second safety sweep.  Dropped buffers never reach inference, tracking
    or BGR conversion.
    """

    def __init__(self, switch, telemetry, source_clock, fps: float, gst_second: int,
                 maximum_pending: int = 2):
        self.switch = switch
        self.telemetry = telemetry
        self.source_clock = source_clock
        self.fps = float(fps)
        self.gst_second = int(gst_second)
        self._lock = threading.RLock()
        self._capacity = threading.Condition(self._lock)
        self.maximum_pending = max(1, int(maximum_pending))
        self._closed = False
        self._generation = 0
        self._records: dict[int, deque[AdmittedFrame]] = {}
        self._quiet: deque[AdmittedFrame] = deque()
        self._index = 0
        self._last_state = "TRACKING"
        self._last_reason = "active"
        self.decoded = 0
        self.inferred = 0
        self.quiet = 0
        self.telemetry.set_gauge("dls_pending_admissions", 0)

    def reset_session(self, source_clock) -> None:
        """Discard decoder-epoch state while retaining the installed probe."""
        with self._lock:
            self.source_clock = source_clock
            self._generation += 1
            self._records.clear()
            self._quiet.clear()
            self._index = 0
            self._last_state = "TRACKING"
            self._last_reason = "reconnect"
            self.telemetry.set_gauge("dls_pending_admissions", 0)
            self._capacity.notify_all()

    def close(self) -> None:
        with self._capacity:
            self._closed = True
            self._capacity.notify_all()

    def _reserve(self, record) -> bool:
        """Backpressure BEFORE inference; callbacks never wait for sink space.

        The non-dropping appsink must have at least maximum_pending slots.
        A reservation includes every admitted buffer until the camera has
        actually pulled its output, including native in-flight work.
        """
        with self._capacity:
            generation = self._generation
            while (not self._closed and generation == self._generation
                   and self.pending_admissions >= self.maximum_pending):
                self._capacity.wait()
            if self._closed or generation != self._generation:
                return False
            self._records.setdefault(record.pts, deque()).append(record)
            return True

    def probe(self, pad, info):
        from gi.repository import Gst
        from gstgva import VideoFrame

        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        pts = int(buffer.pts)
        if pts == (1 << 64) - 1:
            return Gst.PadProbeReturn.DROP
        received_at = time.time()
        caps = pad.get_current_caps()
        candidate = False
        if caps is not None:
            frame = VideoFrame(buffer, caps=caps)
            candidate = any(roi.label() == "motion" for roi in frame.regions())

        with self._lock:
            self._index += 1
            self.decoded += 1
            observed_at = self.source_clock.observe(
                pts, received_at, clock_hz=float(self.gst_second)
            )
            was_tracking = bool(self.switch.tracking)
            state, requested = self.switch.update(
                candidate, self.fps, pts / float(self.gst_second)
            )
            if was_tracking:
                reason = "active"
            elif requested and candidate:
                reason = "motion"
            elif requested:
                reason = "sweep"
            else:
                reason = "quiet"
            record = AdmittedFrame(
                self._index, pts, received_at, observed_at, state, reason
            )
            self._last_state = state
            self._last_reason = reason
            if requested:
                if not self._reserve(record):
                    return Gst.PadProbeReturn.DROP
                self.telemetry.set_gauge(
                    "dls_pending_admissions", self.pending_admissions
                )
                # The inference result is counted only after it reaches the
                # camera worker successfully.
                self.telemetry.frame(inferred=None)
                return Gst.PadProbeReturn.OK
            self.quiet += 1
            self._quiet.append(record)
            self.telemetry.frame(inferred=False)
            return Gst.PadProbeReturn.DROP

    def take(self, pts: int) -> AdmittedFrame:
        with self._lock:
            records = self._records.get(int(pts))
            if not records:
                raise RuntimeError("DL Streamer output has no admission record")
            record = records.popleft()
            if not records:
                self._records.pop(int(pts), None)
            self.inferred += 1
            self._capacity.notify_all()
            self.telemetry.set_gauge(
                "dls_pending_admissions", self.pending_admissions
            )
            return record

    def drain_quiet(self) -> list[AdmittedFrame]:
        with self._lock:
            values = list(self._quiet)
            self._quiet.clear()
            return values

    @property
    def decoded_frames(self) -> int:
        with self._lock:
            return int(self.decoded)

    @property
    def pending_admissions(self) -> int:
        with self._lock:
            return sum(len(values) for values in self._records.values())

    def observe_models(
        self,
        vehicle_hit: bool,
        smoke_hit: bool,
        *,
        fully_evaluated: bool = True,
    ) -> bool:
        with self._lock:
            return self.switch.observe_models(
                vehicle_hit,
                smoke_hit,
                fully_evaluated=fully_evaluated,
            )

    @property
    def state(self) -> str:
        with self._lock:
            return self._last_state

    @property
    def reason(self) -> str:
        with self._lock:
            return self._last_reason


class DlsFaceMetadataBridge:
    """Copy fused DL Streamer face results while their tensors are valid.

    GVA ROI rectangles survive the final VA-to-BGR conversion, but raw
    ``gvaclassify`` tensors do not.  This probe runs on ``gvaclassify.src`` and
    stores only bounded, camera-local NumPy copies keyed by source PTS.
    """

    def __init__(self, inference_interval: int = 3, maximum_pending: int = 16):
        self.inference_interval = max(1, int(inference_interval))
        self.maximum_pending = max(2, int(maximum_pending))
        self._lock = threading.RLock()
        self._frames: dict[int, tuple[bool, list[DlsFaceCandidate]]] = {}
        self._order: deque[int] = deque()
        self._frame_index = 0

    @staticmethod
    def _contains(person_rect, face_rect) -> bool:
        cx = float(face_rect.x) + float(face_rect.w) / 2.0
        cy = float(face_rect.y) + float(face_rect.h) / 2.0
        return (
            float(person_rect.x) <= cx <= float(person_rect.x + person_rect.w)
            and float(person_rect.y) <= cy <= float(person_rect.y + person_rect.h)
        )

    def probe(self, pad, info):
        from gi.repository import Gst
        from gstgva import VideoFrame

        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        pts = int(buffer.pts)
        caps = pad.get_current_caps()
        if pts == (1 << 64) - 1 or caps is None:
            return Gst.PadProbeReturn.OK
        frame = VideoFrame(buffer, caps=caps)
        regions = list(frame.regions())
        people = [region for region in regions if region.label() == "person"]
        candidates: list[DlsFaceCandidate] = []
        for face in (region for region in regions if region.label() == "face"):
            feature = None
            for tensor in face.tensors():
                if tensor.layer_name() != "feat":
                    continue
                data = tensor.data()
                if data is not None and int(data.size) == 512:
                    feature = np.asarray(data, dtype=np.float32).reshape(-1).copy()
                    break
            if feature is None or not np.isfinite(feature).all():
                continue
            norm = float(np.linalg.norm(feature))
            if norm <= 1e-12:
                continue
            feature /= norm
            face_rect = face.rect()
            owners = [person for person in people if self._contains(person.rect(), face_rect)]
            if not owners:
                continue
            owner = min(owners, key=lambda region: region.rect().w * region.rect().h)
            track_id = owner.object_id()
            if track_id is None:
                continue
            candidates.append(DlsFaceCandidate(
                bbox=(
                    int(face_rect.x), int(face_rect.y),
                    int(face_rect.x + face_rect.w), int(face_rect.y + face_rect.h),
                ),
                embedding=feature,
                detector_confidence=float(face.confidence()),
                track_id=int(track_id),
            ))
        with self._lock:
            self._frame_index += 1
            # GVA evaluates the first buffer and then every Nth buffer.
            evaluated = (self._frame_index - 1) % self.inference_interval == 0
            self._frames[pts] = (evaluated, candidates)
            self._order.append(pts)
            while len(self._order) > self.maximum_pending:
                old = self._order.popleft()
                self._frames.pop(old, None)
        return Gst.PadProbeReturn.OK

    def take(self, pts: int) -> tuple[bool, list[DlsFaceCandidate]]:
        with self._lock:
            value = self._frames.pop(int(pts), (False, []))
            try:
                self._order.remove(int(pts))
            except ValueError:
                pass
            return value

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._order.clear()
            self._frame_index = 0


class DlsFaceExtractor:
    """Adapt fused GVA AdaFace metadata to ``FaceSamplePipeline``.

    Detection, alignment and embedding stay in the DL Streamer cascade.
    SixDRepNet remains the production pose guardrail and runs only for a face
    that the existing adaptive scheduler actually requests.
    """

    dimension = 512
    model_id = "face-embedding-model-v1"
    embedding_space = "face-embedding-model-v1:512:bgr-aligned-112"
    metadata_driven = True

    def __init__(self, pose_model: str, telemetry=None):
        from detectors.backends.openvino_face import OpenVINOHeadPoseEstimator

        self.telemetry = telemetry
        self._pose = OpenVINOHeadPoseEstimator(pose_model, telemetry=telemetry)
        self._evaluated = False
        self._candidates: dict[int, DlsFaceCandidate] = {}
        self._suppressions: dict[str, int] = {}
        self.max_pitch = float(os.getenv("HEAD_POSE_MAX_PITCH_DEGREES", "25"))
        self.max_yaw = float(os.getenv("HEAD_POSE_MAX_YAW_DEGREES", "35"))
        self.max_roll = float(os.getenv("HEAD_POSE_MAX_ROLL_DEGREES", "25"))
        self.pose_context = float(os.getenv("HEAD_POSE_CONTEXT_RATIO", "0.20"))

    @property
    def evaluated(self) -> bool:
        return self._evaluated

    def warmup(self) -> None:
        self._pose.warmup()

    def prepare(self, evaluated: bool, candidates: list[DlsFaceCandidate]) -> None:
        self._evaluated = bool(evaluated)
        self._candidates = {candidate.track_id: candidate for candidate in candidates}

    def drain_suppressions(self) -> dict[str, int]:
        result, self._suppressions = self._suppressions, {}
        return result

    def _suppress(self, reason: str) -> None:
        self._suppressions[reason] = self._suppressions.get(reason, 0) + 1

    @staticmethod
    def _axis_quality(angle: float, preferred: float, maximum: float) -> float:
        value = abs(float(angle))
        if value <= preferred:
            return 1.0
        if value >= maximum:
            return 0.0
        return 1.0 - (value - preferred) / max(1e-6, maximum - preferred)

    def _pose_quality(self, pitch: float, yaw: float, roll: float) -> float:
        return float(
            0.5 * self._axis_quality(yaw, min(15.0, self.max_yaw * 0.6), self.max_yaw)
            + 0.3 * self._axis_quality(pitch, min(12.0, self.max_pitch * 0.6), self.max_pitch)
            + 0.2 * self._axis_quality(roll, min(12.0, self.max_roll * 0.6), self.max_roll)
        )

    def _crop(self, frame: np.ndarray, bbox, context: float = 0.0) -> np.ndarray:
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = [int(round(value)) for value in bbox]
        pad_x = int(round(max(1, x2 - x1) * context))
        pad_y = int(round(max(1, y2 - y1) * context))
        return frame[
            max(0, y1 - pad_y):min(height, y2 + pad_y),
            max(0, x1 - pad_x):min(width, x2 + pad_x),
        ]

    def extract(self, frame: np.ndarray, people: list):
        from detectors.backends.openvino_face import FaceSample

        samples = []
        for person in people:
            track_id = person.metadata.get("track_id")
            candidate = self._candidates.get(int(track_id)) if track_id is not None else None
            if candidate is None:
                continue
            face_width = float(candidate.bbox[2] - candidate.bbox[0])
            face_crop = self._crop(frame, candidate.bbox)
            pose_crop = self._crop(frame, candidate.bbox, self.pose_context)
            if face_crop.size == 0 or pose_crop.size == 0:
                self._suppress("face_crop_empty")
                continue
            try:
                pitch, yaw, roll = self._pose.estimate(pose_crop)
            except (ValueError, RuntimeError):
                self._suppress("head_pose_invalid")
                continue
            if (abs(pitch) > self.max_pitch or abs(yaw) > self.max_yaw
                    or abs(roll) > self.max_roll):
                self._suppress("head_pose_out_of_range")
                continue
            pose_quality = self._pose_quality(pitch, yaw, roll)
            gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
            sharpness = min(1.0, float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 1200.0)
            exposure = max(0.0, 1.0 - abs(float(gray.mean()) - 127.5) / 127.5)
            size = min(1.0, max(0.0, (face_width - 24.0) / 80.0))
            confidence = min(1.0, max(0.0, (candidate.detector_confidence - 0.4) / 0.55))
            visual_quality = 0.4 * confidence + 0.25 * size + 0.25 * sharpness + 0.1 * exposure
            quality = float(0.7 * visual_quality + 0.3 * pose_quality)
            chip = cv2.resize(face_crop, (112, 112), interpolation=cv2.INTER_LINEAR)
            samples.append(FaceSample(
                bbox=candidate.bbox,
                embedding=candidate.embedding.copy(),
                detector_confidence=candidate.detector_confidence,
                quality=quality,
                track_id=int(track_id),
                chip=chip,
                pitch_degrees=float(pitch),
                yaw_degrees=float(yaw),
                roll_degrees=float(roll),
                pose_quality=pose_quality,
            ))
        self._candidates = {}
        return samples


def detections_from_sample(sample, class_names: dict[int, str]):
    """Translate GVA ROI/tracking metadata into Sentinel detections."""
    from gstgva import VideoFrame
    from pipeline.types import Detection

    label_to_id = {
        ("person" if name == "pedestrian" else name): class_id
        for class_id, name in class_names.items()
    }
    frame = VideoFrame(sample.get_buffer(), caps=sample.get_caps())
    detections = []
    for roi in frame.regions():
        gva_label = str(roi.label() or "")
        class_id = label_to_id.get(gva_label)
        if class_id is None:
            # Motion ROIs and the model's intentionally excluded "others"
            # class are not primary Sentinel observations.
            continue
        rect = roi.rect()
        track_id = roi.object_id()
        if track_id is None:
            raise RuntimeError("gvatrack did not attach a track ID")
        class_name = class_names[class_id]
        bbox = [
            int(rect.x),
            int(rect.y),
            int(rect.x + rect.w),
            int(rect.y + rect.h),
        ]
        detections.append(
            Detection(
                bbox=bbox,
                class_id=int(class_id),
                class_name=class_name,
                confidence=float(roi.confidence()),
                model_name="vehicle",
                metadata={
                    "track_id": int(track_id),
                    "raw_bbox": list(bbox),
                    "predicted": False,
                    "tracker_backend": "gvatrack",
                },
            )
        )
    return detections


def hazards_from_sample(sample):
    """Translate a DL Streamer fire branch sample into Sentinel detections."""
    from gstgva import VideoFrame
    from pipeline.types import Detection

    frame = VideoFrame(sample.get_buffer(), caps=sample.get_caps())
    detections = []
    for roi in frame.regions():
        label = str(roi.label() or "")
        if label not in {"fire", "smoke"}:
            continue
        rect = roi.rect()
        detections.append(Detection(
            bbox=[
                int(rect.x), int(rect.y),
                int(rect.x + rect.w), int(rect.y + rect.h),
            ],
            class_id=0 if label == "fire" else 1,
            class_name=label,
            confidence=float(roi.confidence()),
            model_name="smoke_fire",
            metadata={"inference_backend": "dlstreamer-gvadetect"},
        ))
    return detections


class GvaTrackerMetadataStage:
    """Enrich authoritative GVA IDs without performing a second association."""

    def __init__(self, target_fps: float, retention_seconds: float = 20.0):
        self.target_fps = max(1.0, float(target_fps))
        self.retention_frames = max(1, round(retention_seconds * self.target_fps))
        self._tracks: dict[tuple[str, int], dict] = {}

    def process(self, packets) -> None:
        for packet in packets:
            matched = set()
            for detection in packet.detections:
                if detection.model_name != "vehicle":
                    continue
                track_id = detection.metadata.get("track_id")
                if track_id is None:
                    raise RuntimeError("primary GVA detection is missing track_id")
                key = (packet.name, int(track_id))
                matched.add(key)
                state = self._tracks.setdefault(
                    key,
                    {
                        "first_index": int(packet.index),
                        "last_index": int(packet.index),
                        "hits": 0,
                    },
                )
                state["last_index"] = int(packet.index)
                state["hits"] += 1
                x1, y1, x2, y2 = detection.bbox
                detection.metadata.update(
                    {
                        "track_hits": int(state["hits"]),
                        "track_age": max(
                            1, int(packet.index) - int(state["first_index"]) + 1
                        ),
                        "track_missed": 0,
                        "track_centroid": (
                            int((x1 + x2) / 2), int((y1 + y2) / 2)
                        ),
                    }
                )
            cutoff = int(packet.index) - self.retention_frames
            for key, state in list(self._tracks.items()):
                if int(state["last_index"]) < cutoff:
                    self._tracks.pop(key, None)
            packet.analytics_state["adaptive_tracker_observation"] = {
                "detector_evaluated": True,
                "active_tracks": len(matched),
                "matched_tracks": len(matched),
                "predicted_tracks": 0,
                "retained_tracker_memory": len(self._tracks),
                "backend": "gvatrack",
            }

    def clear(self) -> None:
        self._tracks.clear()
