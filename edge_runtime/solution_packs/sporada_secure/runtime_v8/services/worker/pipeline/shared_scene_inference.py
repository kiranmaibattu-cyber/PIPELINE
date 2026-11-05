"""Fleet-level stateless SigLIP2 execution with camera-local scene policy.

Only the canonical 1x3x224x224 SigLIP2 input tensor crosses this boundary.
Sampling cadence, geometry crops, source timestamps, pressure decisions,
evidence pixels, event construction, and durable delivery remain in the camera
process.  There is at most one outstanding scene request per camera, and the
request identity rejects completions from an older camera process/session.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import mmap
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any
import uuid

import numpy as np

from pipeline.scene_embeddings import (
    SCENE_DIMENSIONS,
    SCENE_MODEL_ID,
    SCENE_MODEL_VERSION,
    normalize_scene_embedding,
    preprocess_scene_image,
)


log = logging.getLogger(__name__)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))


@dataclass
class SceneInferenceChannel:
    """Pickle-safe transport allocated by the fleet parent."""

    camera_id: str
    slot_path: str
    request_queue: Any
    response_queue: Any
    slot_lock: Any


class SceneInputSlot:
    """One fixed float32 CHW SigLIP2 input tensor per camera."""

    shape = (3, 224, 224)
    dtype = np.dtype(np.float32)

    def __init__(self, path: str, *, create: bool):
        self.path = Path(path)
        self.payload_size = int(np.prod(self.shape) * self.dtype.itemsize)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if create:
                os.ftruncate(fd, self.payload_size)
            elif os.fstat(fd).st_size != self.payload_size:
                raise ValueError(
                    f"scene slot {self.path} has size {os.fstat(fd).st_size}; "
                    f"expected {self.payload_size}"
                )
            self.mapping = mmap.mmap(fd, self.payload_size, access=mmap.ACCESS_WRITE)
        finally:
            os.close(fd)
        self.array = np.ndarray(self.shape, dtype=self.dtype, buffer=self.mapping)

    def write(self, tensor: np.ndarray) -> None:
        if tensor.shape != (1, *self.shape) or tensor.dtype != self.dtype:
            raise ValueError(
                f"unexpected scene tensor {tensor.shape}/{tensor.dtype}; "
                f"expected {(1, *self.shape)}/float32"
            )
        np.copyto(self.array, tensor[0])

    def close(self) -> None:
        self.array = None
        self.mapping.close()


class SceneInferenceTransport:
    """Synchronous camera-side facade over the fleet scene executor."""

    def __init__(self, channel: SceneInferenceChannel, *, timeout_seconds: float = 30.0):
        self.channel = channel
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.slot = SceneInputSlot(channel.slot_path, create=False)
        self.client_id = uuid.uuid4().hex
        self.sequence = 0
        self.last_queue_wait_seconds = 0.0
        self.last_device_inference_seconds = 0.0

    def _discard_stale_request(self) -> None:
        try:
            self.channel.request_queue.get_nowait()
        except queue.Empty:
            return

    def infer(self, tensor: np.ndarray) -> np.ndarray:
        self.sequence += 1
        job_id = f"{self.client_id}:{self.sequence}"
        submitted = time.monotonic()
        with self.channel.slot_lock:
            self.slot.write(tensor)
        job = {
            "job_id": job_id,
            "camera_id": self.channel.camera_id,
            "submitted_monotonic": submitted,
        }
        try:
            self.channel.request_queue.put(job, timeout=1.0)
        except queue.Full:
            self._discard_stale_request()
            self.channel.request_queue.put(job, timeout=1.0)

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"shared scene inference timed out for {self.channel.camera_id}"
                )
            try:
                response = self.channel.response_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError(
                    f"shared scene inference timed out for {self.channel.camera_id}"
                ) from exc
            if response.get("job_id") != job_id:
                continue
            self.last_queue_wait_seconds = float(
                response.get("queue_wait_seconds", 0.0)
            )
            self.last_device_inference_seconds = float(
                response.get("device_inference_seconds", 0.0)
            )
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            return np.asarray(response["output"], dtype=np.float32)

    def close(self) -> None:
        self.slot.close()


class SharedSceneExtractorClient:
    """Drop-in replacement for ``OpenVINOSceneExtractor`` in camera workers."""

    model_id = SCENE_MODEL_ID
    model_version = SCENE_MODEL_VERSION
    dimension = SCENE_DIMENSIONS

    def __init__(self, channel: SceneInferenceChannel, execution_devices: str):
        self.transport = SceneInferenceTransport(channel)
        self.execution_devices = str(execution_devices)

    def warmup(self) -> None:
        # The fleet parent warms the one shared compiled model before workers
        # start. A camera-local warmup would create an artificial scene event.
        return None

    def embed(self, image: np.ndarray) -> np.ndarray:
        tensor = preprocess_scene_image(image)
        return normalize_scene_embedding(self.transport.infer(tensor))

    @property
    def scheduler_queue_wait_seconds(self) -> float:
        return self.transport.last_queue_wait_seconds

    @property
    def scheduler_device_inference_seconds(self) -> float:
        return self.transport.last_device_inference_seconds

    def close(self) -> None:
        self.transport.close()


class SharedSceneInferenceScheduler:
    """Fair fleet scheduler over one compiled SigLIP2 image encoder."""

    def __init__(
        self,
        model_path: str,
        channels: dict[str, SceneInferenceChannel],
        *,
        device: str = "GPU",
        requests: int = 2,
    ):
        import openvino as ov
        from pipeline.scene_embeddings import OpenVINOSceneExtractor

        if not channels:
            raise ValueError("shared scene scheduler requires at least one camera")
        self.channels = channels
        self.camera_ids = list(channels)
        self.requests = max(1, min(4, int(requests)))
        self._slots = {
            camera_id: SceneInputSlot(channel.slot_path, create=False)
            for camera_id, channel in channels.items()
        }
        self._extractor = OpenVINOSceneExtractor(model_path, device=device)
        self._extractor.warmup()
        try:
            devices = self._extractor.compiled.get_property("EXECUTION_DEVICES")
            self.execution_devices = ",".join(str(item) for item in devices)
        except Exception:  # noqa: BLE001 - older OpenVINO property surface
            self.execution_devices = str(device)
        self._queue = ov.AsyncInferQueue(self._extractor.compiled, self.requests)
        self._queue.set_callback(self._complete)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._cursor = 0
        self.completed = 0
        self.rejected = 0

    def start(self) -> "SharedSceneInferenceScheduler":
        self._thread = threading.Thread(
            target=self._run, name="shared-scene-inference", daemon=True
        )
        self._thread.start()
        return self

    @property
    def alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    def _put_response(self, camera_id: str, response: dict[str, Any]) -> None:
        response_queue = self.channels[camera_id].response_queue
        try:
            response_queue.put_nowait(response)
            return
        except queue.Full:
            pass
        try:
            response_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            response_queue.put_nowait(response)
        except queue.Full:
            self.rejected += 1

    def _next_job(self):
        count = len(self.camera_ids)
        for offset in range(count):
            index = (self._cursor + offset) % count
            camera_id = self.camera_ids[index]
            try:
                job = self.channels[camera_id].request_queue.get_nowait()
            except queue.Empty:
                continue
            self._cursor = (index + 1) % count
            return camera_id, job
        return None

    def _complete(self, request, userdata) -> None:
        camera_id, job, started, input_tensor, slot_lock = userdata
        try:
            output = np.asarray(request.get_output_tensor().data).copy()
            if output.size != SCENE_DIMENSIONS:
                raise RuntimeError(
                    f"scene output has {output.size} values; expected {SCENE_DIMENSIONS}"
                )
            self._put_response(camera_id, {
                "job_id": job["job_id"],
                "output": output,
                "error": None,
                "queue_wait_seconds": max(
                    0.0, started - float(job["submitted_monotonic"])
                ),
                "device_inference_seconds": max(0.0, time.monotonic() - started),
            })
            self.completed += 1
        except BaseException as exc:  # noqa: BLE001 - OpenVINO callback boundary
            self._put_response(camera_id, {
                "job_id": job["job_id"],
                "output": None,
                "error": str(exc),
                "queue_wait_seconds": max(
                    0.0, started - float(job["submitted_monotonic"])
                ),
                "device_inference_seconds": max(0.0, time.monotonic() - started),
            })
            self.rejected += 1
        finally:
            del input_tensor
            slot_lock.release()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self._queue.is_ready():
                    self._stop.wait(0.0005)
                    continue
                selected = self._next_job()
                if selected is None:
                    self._stop.wait(0.001)
                    continue
                camera_id, job = selected
                slot_lock = self.channels[camera_id].slot_lock
                slot_lock.acquire()
                input_tensor = self._slots[camera_id].array[np.newaxis, ...]
                started = time.monotonic()
                try:
                    self._queue.start_async(
                        {0: input_tensor},
                        userdata=(camera_id, job, started, input_tensor, slot_lock),
                    )
                except BaseException as exc:  # noqa: BLE001
                    slot_lock.release()
                    self.rejected += 1
                    self._put_response(camera_id, {
                        "job_id": job["job_id"],
                        "output": None,
                        "error": str(exc),
                        "queue_wait_seconds": max(
                            0.0, started - float(job["submitted_monotonic"])
                        ),
                        "device_inference_seconds": 0.0,
                    })
        except BaseException as exc:  # noqa: BLE001 - scheduler supervision boundary
            self._failure = exc
            log.exception("shared scene inference scheduler stopped unexpectedly")
        finally:
            try:
                self._queue.wait_all()
            except Exception:  # noqa: BLE001
                log.exception("failed while draining shared scene inference")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=35.0)
        for slot in self._slots.values():
            slot.close()


def create_scene_channels(ctx, camera_ids: list[str], *, owner_pid: int | None = None):
    """Create one bounded tensor slot and queue pair per scene-enabled camera."""
    owner_pid = int(owner_pid or os.getpid())
    root = Path(os.getenv("SENTINEL_SHARED_MEMORY_DIR") or "/dev/shm")
    if not root.is_dir():
        root = Path("/tmp/sentinel/shared_scene")
    required = len(camera_ids) * int(np.prod(SceneInputSlot.shape)) * 4
    if root == Path("/dev/shm"):
        available = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
        if required + 4 * 1024 * 1024 > available:
            root = Path("/tmp/sentinel/shared_scene")
            log.warning(
                "/dev/shm cannot safely hold %d scene slots (%d bytes); using %s",
                len(camera_ids), required, root,
            )
    root.mkdir(parents=True, exist_ok=True)
    channels: dict[str, SceneInferenceChannel] = {}
    owners: list[SceneInputSlot] = []
    for camera_id in camera_ids:
        path = root / f"sentinel-v8-{owner_pid}-scene-{_safe_name(camera_id)}.f32"
        owner = SceneInputSlot(str(path), create=True)
        owners.append(owner)
        channels[camera_id] = SceneInferenceChannel(
            camera_id=camera_id,
            slot_path=str(path),
            request_queue=ctx.Queue(maxsize=1),
            response_queue=ctx.Queue(maxsize=2),
            slot_lock=ctx.Lock(),
        )
    return channels, owners


def destroy_scene_channels(
    channels: dict[str, SceneInferenceChannel], owners: list[SceneInputSlot]
) -> None:
    for owner in owners:
        owner.close()
    for channel in channels.values():
        try:
            Path(channel.slot_path).unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove shared scene slot %s", channel.slot_path)
        for transport_queue in (channel.request_queue, channel.response_queue):
            transport_queue.close()
            transport_queue.join_thread()
