"""DL Streamer execution adapters for Sentinel's gated secondary analytics.

These adapters move model execution into persistent GVA pipelines while
preserving the existing camera-local policy interfaces.  They intentionally do
not decide whether an observation becomes a V8 event; crop quality, temporal
confirmation, association, best-candidate selection and deduplication remain in
``V8EventPipeline``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from detectors.backends.openvino_detector import letterbox, yolo_detections
from detectors.backends.openvino_ocr_async import decode_logits, resolve_ocr_queue_depth
from pipeline.person_reid import (
    BODY_DIMENSIONS,
    BODY_EMBEDDING_SPACE,
    BODY_MODEL_ID,
    BODY_MODEL_VERSION,
    GAIT_DIMENSIONS,
    GAIT_EMBEDDING_SPACE,
    GAIT_MODEL_ID,
    GAIT_MODEL_VERSION,
    OpenVINOGaitExtractor,
    _l2,
)
from pipeline.scene_embeddings import normalize_scene_embedding


_ENGINE_REGISTRY_LOCK = threading.RLock()
_ENGINE_REGISTRY: dict[tuple, dict[str, object]] = {}


@dataclass
class _InferenceJob:
    owner_id: str
    image: np.ndarray
    submitted_at: float = field(default_factory=time.perf_counter)
    completed: threading.Event = field(default_factory=threading.Event)
    result: np.ndarray | None = None
    error: BaseException | None = None
    queue_wait_seconds: float = 0.0
    device_inference_seconds: float = 0.0


def secondary_model_instance_id(name: str) -> str:
    """Return a stable, model-specific GVA singleton boundary."""
    value = re.sub(r"[^a-z0-9_.-]+", "-", str(name).lower()).strip("-")
    return f"sentinel-{value or 'secondary'}"


class DlsRawImageInferencer:
    """Run one image model through a persistent ``gvainference`` pipeline."""

    def __init__(
        self,
        model_path: str,
        model_proc: str,
        *,
        width: int,
        height: int,
        device: str,
        name: str,
        expected_elements: int,
        reshape: bool = False,
        timeout_seconds: float = 30.0,
    ) -> None:
        model = Path(model_path)
        proc = Path(model_proc)
        if not model.is_file():
            raise FileNotFoundError(f"DL Streamer model is missing: {model}")
        if not proc.is_file():
            raise FileNotFoundError(f"DL Streamer model-proc is missing: {proc}")
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init([])
        self.Gst = Gst
        self.width = int(width)
        self.height = int(height)
        self.timeout_seconds = float(timeout_seconds)
        self.device = str(device)
        self.actual_device = f"{self.device}/DLStreamer"
        self.expected_elements = int(expected_elements)
        self.name = str(name)
        self._trace_jobs = os.getenv("SENTINEL_DLS_TRACE_JOBS", "0") == "1"
        self._condition = threading.Condition()
        self._owner_queues: dict[str, deque[_InferenceJob]] = {}
        self._owner_order: deque[str] = deque()
        self._last_metrics: dict[str, tuple[float, float]] = {}
        self._closed = False
        self._maximum_pending_per_owner = max(1, int(os.getenv(
            "SENTINEL_DLS_SECONDARY_PENDING_PER_CAMERA", "1"
        )))
        self._sequence = 0
        reshape_options = (
            f" reshape=true reshape-width={self.width} reshape-height={self.height}"
            if reshape else ""
        )
        description = (
            "appsrc name=source is-live=true block=true format=time "
            f"caps=video/x-raw,format=BGR,width={self.width},height={self.height},framerate=1/1 ! "
            f"gvainference name=infer model={json.dumps(str(model))} "
            f"model-proc={json.dumps(str(proc))} device={self.device} "
            f"model-instance-id={json.dumps(secondary_model_instance_id(name))} "
            "pre-process-backend=opencv nireq=1"
            f"{reshape_options} ! "
            "appsink name=result sync=false max-buffers=1 drop=false "
            "wait-on-eos=false enable-last-sample=false"
        )
        self.pipeline = Gst.parse_launch(description)
        self.source = self.pipeline.get_by_name("source")
        self.sink = self.pipeline.get_by_name("result")
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError(f"could not start DL Streamer secondary {name}")

        self._worker = threading.Thread(
            target=self._run,
            name=f"dls-secondary-{secondary_model_instance_id(name)}",
            daemon=True,
        )
        self._worker.start()

    def infer(self, image: np.ndarray, *, owner_id: str = "default") -> np.ndarray:
        if image is None or image.size == 0:
            raise ValueError("DL Streamer input image is empty")
        if image.shape[:2] != (self.height, self.width):
            image = cv2.resize(image, (self.width, self.height), cv2.INTER_LINEAR)
        image = np.ascontiguousarray(image, dtype=np.uint8)
        job = _InferenceJob(str(owner_id), image)
        with self._condition:
            if self._closed:
                raise RuntimeError("DL Streamer secondary is closed")
            jobs = self._owner_queues.setdefault(job.owner_id, deque())
            if len(jobs) >= self._maximum_pending_per_owner:
                raise RuntimeError(
                    "DL Streamer secondary per-camera queue is full"
                )
            jobs.append(job)
            if job.owner_id not in self._owner_order:
                self._owner_order.append(job.owner_id)
            self._condition.notify()
        if not job.completed.wait(self.timeout_seconds):
            raise TimeoutError("DL Streamer secondary scheduling timed out")
        if job.error is not None:
            raise job.error
        if job.result is None:
            raise RuntimeError("DL Streamer secondary completed without output")
        return job.result

    def _take_job(self) -> _InferenceJob | None:
        with self._condition:
            while not self._closed and not self._owner_order:
                self._condition.wait()
            if self._closed and not self._owner_order:
                return None
            owner = self._owner_order.popleft()
            jobs = self._owner_queues[owner]
            job = jobs.popleft()
            if jobs:
                self._owner_order.append(owner)
            else:
                self._owner_queues.pop(owner, None)
            return job

    def _run(self) -> None:
        while True:
            job = self._take_job()
            if job is None:
                return
            job.queue_wait_seconds = time.perf_counter() - job.submitted_at
            started = time.perf_counter()
            job_sequence = self._sequence
            try:
                job.result = self._infer_direct(job.image)
            except BaseException as exc:  # propagate native/model failures
                job.error = exc
            finally:
                job.device_inference_seconds = time.perf_counter() - started
                if self._trace_jobs or job.error is not None:
                    logging.getLogger(__name__).log(
                        logging.WARNING if job.error else logging.INFO,
                        "DL Streamer secondary job completed",
                        extra={
                            "event": "dls_secondary_job",
                            "camera_id": job.owner_id,
                            "model_name": self.name,
                            "job_sequence": job_sequence,
                            "queue_wait_ms": round(job.queue_wait_seconds * 1000, 3),
                            "executor_wall_ms": round(job.device_inference_seconds * 1000, 3),
                            "successful": job.error is None,
                            "error_type": type(job.error).__name__ if job.error else None,
                        },
                    )
                with self._condition:
                    self._last_metrics[job.owner_id] = (
                        job.queue_wait_seconds,
                        job.device_inference_seconds,
                    )
                job.completed.set()

    def _infer_direct(self, image: np.ndarray) -> np.ndarray:
        Gst = self.Gst
        buffer = Gst.Buffer.new_allocate(None, int(image.nbytes), None)
        buffer.fill(0, image.tobytes())
        buffer.pts = self._sequence * Gst.SECOND
        buffer.dts = buffer.pts
        buffer.duration = Gst.SECOND
        self._sequence += 1
        flow = self.source.emit("push-buffer", buffer)
        if flow != Gst.FlowReturn.OK:
            raise RuntimeError(f"DL Streamer appsrc rejected input: {flow}")
        sample = self.sink.emit(
            "try-pull-sample", int(self.timeout_seconds * Gst.SECOND)
        )
        if sample is None:
            message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if message is not None:
                error, _ = message.parse_error()
                raise RuntimeError(
                    f"DL Streamer secondary inference failed: {error.message}"
                )
            raise TimeoutError("DL Streamer secondary inference timed out")
        from gstgva import VideoFrame

        frame = VideoFrame(sample.get_buffer(), caps=sample.get_caps())
        tensors = list(frame.tensors())
        if not tensors:
            raise RuntimeError("DL Streamer secondary produced no output tensor")
        observed_sizes = []
        for tensor in tensors:
            data = tensor.data()
            if data is not None:
                result = np.asarray(data)
                observed_sizes.append(int(result.size))
                if result.size == self.expected_elements:
                    return result.copy()
        raise RuntimeError(
            "DL Streamer secondary output tensor size mismatch: "
            f"expected {self.expected_elements}, observed {observed_sizes}"
        )

    def close(self) -> None:
        if self.pipeline is not None:
            with self._condition:
                self._closed = True
                pending = [
                    job for jobs in self._owner_queues.values() for job in jobs
                ]
                self._owner_queues.clear()
                self._owner_order.clear()
                self._condition.notify_all()
            for job in pending:
                job.error = RuntimeError("DL Streamer secondary closed")
                job.completed.set()
            self._worker.join(timeout=5)
            self.pipeline.set_state(self.Gst.State.NULL)
            self.pipeline = None


class _SharedDlsInferencerHandle:
    """Reference-counted camera-local handle to one process-local GVA engine."""

    def __init__(self, key: tuple, engine: DlsRawImageInferencer,
                 owner_id: str) -> None:
        self._key = key
        self._engine = engine
        self._owner_id = str(owner_id)
        self._closed = False
        self.queue_wait_seconds = 0.0
        self.device_inference_seconds = 0.0

    @property
    def actual_device(self) -> str:
        return self._engine.actual_device

    def infer(self, image: np.ndarray) -> np.ndarray:
        if self._closed:
            raise RuntimeError("DL Streamer secondary handle is closed")
        try:
            result = self._engine.infer(image, owner_id=self._owner_id)
        except TypeError as exc:
            # Small test doubles and legacy internal executors may not yet
            # expose owner-aware scheduling; production engines always do.
            if "owner_id" not in str(exc):
                raise
            result = self._engine.infer(image)
        condition = getattr(self._engine, "_condition", _ENGINE_REGISTRY_LOCK)
        with condition:
            metrics = getattr(self._engine, "_last_metrics", {}).get(
                self._owner_id, (0.0, 0.0)
            )
        self.queue_wait_seconds, self.device_inference_seconds = metrics
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        engine = None
        with _ENGINE_REGISTRY_LOCK:
            entry = _ENGINE_REGISTRY.get(self._key)
            if entry is None:
                return
            entry["references"] = int(entry["references"]) - 1
            if int(entry["references"]) <= 0:
                engine = entry["engine"]
                _ENGINE_REGISTRY.pop(self._key, None)
        if engine is not None:
            engine.close()


def acquire_dls_inferencer(
    model_path: str,
    model_proc: str,
    *,
    width: int,
    height: int,
    device: str,
    name: str,
    expected_elements: int,
    reshape: bool = False,
    timeout_seconds: float = 30.0,
    owner_id: str = "default",
) -> _SharedDlsInferencerHandle:
    """Acquire one stateless secondary engine shared by this graph worker.

    Camera-local ANPR consensus, identity selection, gait buffers and event
    policy remain outside this registry.  Only the model executor is shared.
    Construction is serialized because concurrent creation of identical Intel
    GVA model instances from camera threads can crash inside the native plugin.
    """
    key = (
        str(Path(model_path).resolve()),
        str(Path(model_proc).resolve()),
        int(width), int(height), str(device), str(name),
        int(expected_elements), bool(reshape), float(timeout_seconds),
    )
    with _ENGINE_REGISTRY_LOCK:
        entry = _ENGINE_REGISTRY.get(key)
        if entry is None:
            engine = DlsRawImageInferencer(
                model_path,
                model_proc,
                width=width,
                height=height,
                device=device,
                name=name,
                expected_elements=expected_elements,
                reshape=reshape,
                timeout_seconds=timeout_seconds,
            )
            entry = {"engine": engine, "references": 0}
            _ENGINE_REGISTRY[key] = entry
        entry["references"] = int(entry["references"]) + 1
        return _SharedDlsInferencerHandle(key, entry["engine"], owner_id)


class DlsBodyExtractor:
    model_id = BODY_MODEL_ID
    model_version = BODY_MODEL_VERSION
    dimension = BODY_DIMENSIONS
    embedding_space = BODY_EMBEDDING_SPACE

    def __init__(self, model_path: str, model_proc: str, device: str = "NPU",
                 owner_id: str = "default"):
        self.engine = acquire_dls_inferencer(
            model_path, model_proc, width=128, height=256,
            device=device, name="body-reid", expected_elements=self.dimension,
            owner_id=owner_id,
        )
        self.actual_device = self.engine.actual_device

    def warmup(self) -> None:
        self.embed([np.zeros((256, 128, 3), np.uint8)])

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        vectors = []
        for crop in crops:
            # Preserve the established TransReID sampling contract. The legacy
            # extractor uses Pillow bilinear before color conversion and
            # normalization; supplying the exact model size prevents GVA from
            # introducing a second resize.
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            rgb = np.asarray(
                Image.fromarray(rgb).resize((128, 256), Image.BILINEAR),
                dtype=np.uint8,
            )
            resized = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            vectors.append(_l2(self.engine.infer(resized), self.dimension))
        return (
            np.stack(vectors)
            if vectors else np.empty((0, self.dimension), np.float32)
        )

    def close(self) -> None:
        self.engine.close()


class DlsSceneExtractor:
    dimension = 768

    def __init__(self, model_path: str, model_proc: str, device: str = "GPU",
                 owner_id: str = "default"):
        self.engine = acquire_dls_inferencer(
            model_path, model_proc, width=224, height=224,
            device=device, name="scene-siglip", expected_elements=self.dimension,
            owner_id=owner_id,
        )
        self.actual_device = self.engine.actual_device
        self.scheduler_queue_wait_seconds = None
        self.scheduler_device_inference_seconds = None

    def warmup(self) -> None:
        self.embed(np.zeros((224, 224, 3), np.uint8))

    def embed(self, image: np.ndarray) -> np.ndarray:
        resized = cv2.resize(image, (224, 224), cv2.INTER_AREA)
        started = time.perf_counter()
        vector = normalize_scene_embedding(self.engine.infer(resized))
        total = time.perf_counter() - started
        self.scheduler_queue_wait_seconds = self.engine.queue_wait_seconds
        self.scheduler_device_inference_seconds = (
            self.engine.device_inference_seconds or total
        )
        return vector

    def close(self) -> None:
        self.engine.close()


class DlsPlateDetector:
    """NMS-baked plate detector executed by DL Streamer, policy-neutral."""

    def __init__(
        self,
        model_path: str,
        model_proc: str,
        *,
        device: str = "GPU",
        confidence: float = 0.35,
        owner_id: str = "default",
    ) -> None:
        self.confidence = float(confidence)
        self.engine = acquire_dls_inferencer(
            model_path, model_proc, width=224, height=224,
            device=device, name="plate-detector", expected_elements=300 * 6,
            owner_id=owner_id,
        )
        self.exec_devices = self.engine.actual_device

    def warmup(self) -> None:
        self.detect(np.zeros((224, 224, 3), np.uint8))

    def detect(self, image: np.ndarray):
        if image is None or image.size == 0:
            return []
        canvas, scale, pad_x, pad_y = letterbox(image, 224)
        output = self.engine.infer(canvas).reshape(1, 300, 6)
        return yolo_detections(
            output,
            image_shape=image.shape[:2],
            scale=scale,
            pad_x=pad_x,
            pad_y=pad_y,
            confidence=self.confidence,
            class_ids=None,
            class_names={0: "license_plate"},
            model_name="license_plate",
            max_detections=1,
        )

    def close(self) -> None:
        self.engine.close()


class DlsAsyncOCR:
    """Bounded asynchronous OCR facade backed by a DL Streamer pipeline."""

    def __init__(
        self,
        model_path: str,
        model_proc: str,
        *,
        device: str,
        on_result: Callable | None = None,
        telemetry=None,
        owner_id: str = "default",
    ) -> None:
        self.engine = acquire_dls_inferencer(
            model_path, model_proc, width=128, height=64,
            device=device, name="plate-ocr", expected_elements=15 * 37,
            owner_id=owner_id,
        )
        self.actual_device = self.engine.actual_device
        self.on_result = on_result
        self.telemetry = telemetry
        self.depth = resolve_ocr_queue_depth()
        self.jobs: queue.Queue = queue.Queue(maxsize=self.depth)
        self.submitted = 0
        self.dropped = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="dls-ocr", daemon=True
        )
        self._thread.start()

    def submit(self, crop_bgr: np.ndarray, userdata) -> None:
        if crop_bgr is None or crop_bgr.size == 0 or self._closed:
            return
        try:
            self.jobs.put_nowait((np.asarray(crop_bgr).copy(), userdata))
            self.submitted += 1
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                if job is None:
                    return
                crop, userdata = job
                resized = cv2.resize(crop, (128, 64), cv2.INTER_LINEAR)
                started = time.perf_counter()
                text, confidence = decode_logits(
                    self.engine.infer(resized).reshape(1, 15, 37)
                )
                if self.telemetry is not None:
                    self.telemetry.observe_latency(
                        "anpr.ocr", time.perf_counter() - started,
                        device=self.actual_device,
                    )
                if self.on_result is not None:
                    self.on_result(text, confidence, userdata)
            finally:
                self.jobs.task_done()

    def wait(self) -> None:
        self.jobs.join()

    def close(self) -> None:
        if self._closed:
            return
        self.wait()
        self._closed = True
        self.jobs.put(None)
        self._thread.join(timeout=5)
        self.engine.close()


class _DlsGaitCallable:
    def __init__(self, engine: DlsRawImageInferencer):
        self.engine = engine

    def __call__(self, inputs):
        sequence = np.asarray(inputs[0], dtype=np.float32)[0]
        montage = np.zeros((320, 264, 3), np.uint8)
        for index, silhouette in enumerate(sequence[:30]):
            row, column = divmod(index, 6)
            tile = np.clip(silhouette * 255.0, 0, 255).astype(np.uint8)
            montage[
                row * 64:(row + 1) * 64,
                column * 44:(column + 1) * 44,
            ] = tile[:, :, None]
        return {0: self.engine.infer(montage).reshape(1, 256, 16)}


class DlsGaitExtractor(OpenVINOGaitExtractor):
    model_id = GAIT_MODEL_ID
    model_version = GAIT_MODEL_VERSION
    dimension = GAIT_DIMENSIONS
    embedding_space = GAIT_EMBEDDING_SPACE

    def __init__(
        self,
        wrapper_model: str,
        model_proc: str,
        device: str = "NPU",
        min_sequence: int = 20,
        max_sequence: int = 60,
        minimum_motion_pixels: float = 2.0,
        owner_id: str = "default",
    ) -> None:
        from collections import defaultdict

        self.source = "background_subtraction+dlstreamer"
        self.engine = acquire_dls_inferencer(
            wrapper_model, model_proc, width=264, height=320,
            device=device, name="gaitbase-montage",
            expected_elements=256 * 16,
            owner_id=owner_id,
        )
        self.actual_device = self.engine.actual_device
        self.gait = _DlsGaitCallable(self.engine)
        self.gait_output = 0
        self.sequence_length = 30
        self.min_sequence = int(min_sequence)
        self.max_sequence = int(max_sequence)
        self.buffers = defaultdict(list)
        self.buffer_times = defaultdict(list)
        self.cache = {}
        self.backgrounds = {}
        self.minimum_motion_pixels = float(minimum_motion_pixels)
        self.centers = {}

    def close(self) -> None:
        self.engine.close()
