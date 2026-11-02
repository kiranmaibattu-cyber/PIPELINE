"""Shared-memory, fair asynchronous vehicle/person inference.

The camera processes retain ownership of decoding, tracking, presence state,
policy and evidence.  They place only the 640x640 detector canvas in a private
shared-memory slot and send a small job token to this scheduler.  One central
OpenVINO ``AsyncInferQueue`` then serves the camera slots in round-robin order.

There is at most one outstanding detector job per camera.  Consequently the
camera cannot overwrite a canvas while OpenVINO is still consuming it, and no
source/evidence frame ever leaves the camera process through this transport.
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


log = logging.getLogger(__name__)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))


@dataclass
class VehicleInferenceChannel:
    """Pickle-safe camera transport created by the fleet parent."""

    camera_id: str
    slot_path: str
    request_queue: Any
    response_queue: Any
    slot_lock: Any
    width: int = 640
    height: int = 640


class VehicleInputSlot:
    """Fixed-size mmap slot for one camera's uint8 NHWC detector canvas."""

    def __init__(self, path: str, width: int = 640, height: int = 640, *, create: bool):
        self.path = Path(path)
        self.width = int(width)
        self.height = int(height)
        self.shape = (self.height, self.width, 3)
        self.payload_size = int(np.prod(self.shape))
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if create:
                os.ftruncate(fd, self.payload_size)
            elif os.fstat(fd).st_size != self.payload_size:
                raise ValueError(
                    f"vehicle slot {self.path} has size {os.fstat(fd).st_size}; "
                    f"expected {self.payload_size}"
                )
            self.mapping = mmap.mmap(fd, self.payload_size, access=mmap.ACCESS_WRITE)
        finally:
            os.close(fd)
        self.array = np.ndarray(self.shape, dtype=np.uint8, buffer=self.mapping)

    def write(self, canvas: np.ndarray) -> None:
        if canvas.shape != self.shape or canvas.dtype != np.uint8:
            raise ValueError(
                f"unexpected vehicle canvas {canvas.shape}/{canvas.dtype}; "
                f"expected {self.shape}/uint8"
            )
        np.copyto(self.array, canvas)

    def close(self) -> None:
        self.array = None
        self.mapping.close()


class VehicleInferenceTransport:
    """Camera-side request/response adapter for one shared input slot."""

    def __init__(self, channel: VehicleInferenceChannel, *, timeout_seconds: float = 30.0):
        self.channel = channel
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.slot = VehicleInputSlot(
            channel.slot_path, channel.width, channel.height, create=False
        )
        self.client_id = uuid.uuid4().hex
        self.sequence = 0
        self.last_queue_wait_seconds = 0.0
        self.last_device_inference_seconds = 0.0

    def _discard_stale_request(self) -> None:
        try:
            self.channel.request_queue.get_nowait()
        except queue.Empty:
            return

    def infer(self, canvas: np.ndarray) -> dict[str, Any]:
        self.sequence += 1
        job_id = f"{self.client_id}:{self.sequence}"
        submitted = time.monotonic()
        with self.channel.slot_lock:
            self.slot.write(canvas)
        job = {
            "job_id": job_id,
            "camera_id": self.channel.camera_id,
            "submitted_monotonic": submitted,
        }
        try:
            self.channel.request_queue.put(job, timeout=1.0)
        except queue.Full:
            # A camera process may have died with one unconsumed token.  A new
            # stream session is allowed to replace that stale, unstarted work.
            self._discard_stale_request()
            self.channel.request_queue.put(job, timeout=1.0)

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"shared vehicle inference timed out for {self.channel.camera_id}"
                )
            try:
                response = self.channel.response_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError(
                    f"shared vehicle inference timed out for {self.channel.camera_id}"
                ) from exc
            if response.get("job_id") != job_id:
                # Stale completion from an earlier camera process/session.
                continue
            self.last_queue_wait_seconds = float(response.get("queue_wait_seconds", 0.0))
            self.last_device_inference_seconds = float(
                response.get("device_inference_seconds", 0.0)
            )
            return response

    def reset(self) -> None:
        """There is no camera-local InferRequest to reset in this architecture."""

    def close(self) -> None:
        self.slot.close()


class SharedVehicleInferenceScheduler:
    """Fleet-level fair scheduler backed by one OpenVINO AsyncInferQueue."""

    def __init__(
        self,
        model_xml: str,
        confidence: float,
        channels: dict[str, VehicleInferenceChannel],
        *,
        device: str,
        class_ids: set[int],
        class_names: dict[int, str],
        requests: int = 4,
    ):
        import openvino as ov
        from detectors.backends.openvino_detector import OpenVINOYOLODetector

        self.channels = channels
        self.camera_ids = list(channels)
        self.requests = max(1, int(requests))
        self._slots = {
            camera_id: VehicleInputSlot(
                channel.slot_path, channel.width, channel.height, create=False
            )
            for camera_id, channel in channels.items()
        }
        self._detector = OpenVINOYOLODetector(
            model_xml,
            "vehicle",
            device=device,
            imgsz=640,
            confidence=confidence,
            class_ids=class_ids,
            class_names=class_names,
            performance_hint="THROUGHPUT" if self.requests > 1 else "LATENCY",
        )
        self.execution_devices = self._detector.exec_devices
        self._queue = ov.AsyncInferQueue(self._detector._compiled, self.requests)
        self._queue.set_callback(self._complete)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._cursor = 0
        self.completed = 0
        self.rejected = 0

        # Warm the same compiled model before camera processes announce ready.
        request = self._detector._compiled.create_infer_request()
        dummy = np.zeros((640, 640, 3), np.uint8)
        for _ in range(min(2, self.requests)):
            self._detector.infer_letterboxed(dummy, request=request)

    def start(self) -> "SharedVehicleInferenceScheduler":
        self._thread = threading.Thread(
            target=self._run,
            name="shared-vehicle-inference",
            daemon=True,
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

    def _complete(self, request, userdata) -> None:
        camera_id, job, started, input_view = userdata
        try:
            output = np.asarray(request.get_output_tensor().data).copy()
            response = {
                "job_id": job["job_id"],
                "output": output,
                "error": None,
                "queue_wait_seconds": max(
                    0.0, started - float(job["submitted_monotonic"])
                ),
                "device_inference_seconds": max(0.0, time.monotonic() - started),
            }
            self.completed += 1
        except BaseException as exc:  # noqa: BLE001 - OpenVINO callback boundary
            response = {
                "job_id": job["job_id"],
                "output": None,
                "error": str(exc),
                "queue_wait_seconds": max(
                    0.0, started - float(job["submitted_monotonic"])
                ),
                "device_inference_seconds": max(0.0, time.monotonic() - started),
            }
            self.rejected += 1
        finally:
            # Keep the mmap-backed ndarray alive until OpenVINO has completed.
            del input_view
            self.channels[camera_id].slot_lock.release()
        self._put_response(camera_id, response)

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

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                # AsyncInferQueue.start_async() waits when every request is
                # occupied.  In this Python callback design that can delay the
                # callbacks which return results to camera processes.  Admit a
                # new camera only when OpenVINO reports an idle request, keeping
                # the scheduler loop and completion callbacks non-blocking.
                if not self._queue.is_ready():
                    self._stop.wait(0.0005)
                    continue
                selected = self._next_job()
                if selected is None:
                    self._stop.wait(0.001)
                    continue
                camera_id, job = selected
                channel = self.channels[camera_id]
                slot = self._slots[camera_id]
                # Hold the inter-process slot lock until the callback.  Normally
                # one outstanding request per camera already prevents overwrite;
                # the lock additionally protects an in-flight request when a
                # camera process crashes and its replacement starts immediately.
                channel.slot_lock.acquire()
                input_view = slot.array[np.newaxis, ...]
                started = time.monotonic()
                try:
                    self._queue.start_async(
                        {0: input_view},
                        userdata=(camera_id, job, started, input_view),
                    )
                except BaseException as exc:  # noqa: BLE001
                    channel.slot_lock.release()
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
            log.exception("shared vehicle inference scheduler stopped unexpectedly")
        finally:
            try:
                self._queue.wait_all()
            except Exception:  # noqa: BLE001
                log.exception("failed while draining shared vehicle inference")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=35.0)
        for slot in self._slots.values():
            slot.close()


def create_vehicle_channels(ctx, camera_ids: list[str], *, owner_pid: int | None = None):
    """Create one mmap slot and bounded request/response queues per camera."""
    owner_pid = int(owner_pid or os.getpid())
    configured_root = os.getenv("SENTINEL_SHARED_MEMORY_DIR")
    if configured_root:
        root = Path(configured_root)
    else:
        root = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path("/tmp")
        if root == Path("/dev/shm"):
            available = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
            required = len(camera_ids) * 640 * 640 * 3
            reserve = 8 * 1024 * 1024
            if required + reserve > available:
                root = Path("/tmp/sentinel/shared_vehicle")
                log.warning(
                    "/dev/shm cannot safely hold %d vehicle slots (%d bytes); "
                    "using %s",
                    len(camera_ids), required, root,
                )
    root.mkdir(parents=True, exist_ok=True)
    channels: dict[str, VehicleInferenceChannel] = {}
    owners: list[VehicleInputSlot] = []
    for camera_id in camera_ids:
        path = root / (
            f"sentinel-v7-{owner_pid}-vehicle-{_safe_name(camera_id)}.bgr"
        )
        owner = VehicleInputSlot(str(path), create=True)
        owners.append(owner)
        channels[camera_id] = VehicleInferenceChannel(
            camera_id=camera_id,
            slot_path=str(path),
            request_queue=ctx.Queue(maxsize=1),
            response_queue=ctx.Queue(maxsize=2),
            slot_lock=ctx.Lock(),
        )
    return channels, owners


def destroy_vehicle_channels(
    channels: dict[str, VehicleInferenceChannel], owners: list[VehicleInputSlot]
) -> None:
    for owner in owners:
        owner.close()
    for channel in channels.values():
        try:
            Path(channel.slot_path).unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove shared vehicle slot %s", channel.slot_path)
        for transport_queue in (channel.request_queue, channel.response_queue):
            transport_queue.close()
            transport_queue.join_thread()
