"""Async OpenVINO OCR for the fleet pipeline (Intel Arc iGPU).

The synchronous OpenVINOOCR blocks the consume loop on every plate. This version
fires each plate crop as a non-blocking `start_async` into an AsyncInferQueue
compiled with the THROUGHPUT performance hint, so:

  - the consume loop never waits on OCR (decode cascade keeps running at full fps),
  - OpenVINO runs multiple inference requests/streams concurrently and implicitly
    auto-batches crops arriving from all cameras (the big iGPU-throughput win),
  - each result is tagged with (camera, track_id, frame_idx, conf) via userdata and
    delivered to `on_result`, which routes it into the track-keyed OcrStabilizer.

Matches OpenVINOOCR's I/O exactly: 128x64 RGB, NO /255 scaling, argmax decode over
the 0-9A-Z_ alphabet (strip the `_` pad).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
PAD = "_"


class AsyncOCR:
    def __init__(self, model_path: str, device: str = "MULTI:GPU,NPU",
                 on_result: Callable[[str, Tuple], None] = None,
                 cache_dir: str = "/tmp/ov_cache"):
        self.img_w, self.img_h = 128, 64
        self.on_result = on_result
        import openvino as ov

        core = ov.Core()
        try:
            os.makedirs(cache_dir, exist_ok=True)
            core.set_property({"CACHE_DIR": cache_dir})
        except Exception:  # noqa: BLE001
            pass
        device = self._resolve_device(core, device)
        # MULTI/AUTO spread requests across the listed devices (iGPU + idle NPU);
        # CUMULATIVE_THROUGHPUT keeps every device busy rather than picking one.
        cfg = {"PERFORMANCE_HINT": "CUMULATIVE_THROUGHPUT" if ":" in device
               else "THROUGHPUT"}
        model = core.read_model(model_path)
        try:
            self._compiled = core.compile_model(model, device, cfg)
        except Exception as exc:  # noqa: BLE001  (e.g. NPU can't compile the transformer)
            logger.warning("OCR compile on %s failed (%s); falling back to GPU", device, exc)
            self._compiled = core.compile_model(model, "GPU", {"PERFORMANCE_HINT": "THROUGHPUT"})
        self._out = self._compiled.outputs[0]
        try:
            self._n = self._compiled.get_property("OPTIMAL_NUMBER_OF_INFER_REQUESTS")
        except Exception:  # noqa: BLE001
            self._n = 0
        # The GPU runs each plate in ~1.5ms with large throughput headroom, so the
        # default "optimal" 4 requests is too shallow — a burst of plates in one frame
        # fills it and start_async blocks the consume loop. Deepen the queue so bursts
        # are absorbed inflight instead.
        self.depth = int(os.getenv("OCR_QUEUE_DEPTH", "16"))
        self._q = ov.AsyncInferQueue(self._compiled, self.depth)
        self._q.set_callback(self._on_done)
        self.submitted = self.dropped = 0
        self._lock = threading.Lock()  # the all-OpenVINO worker submits from N camera threads
        self.actual_device = ",".join(self._compiled.get_property("EXECUTION_DEVICES"))
        logger.info("Async OCR requested=%s exec=%s (depth=%d, optimal=%s)",
                    device, self.actual_device, self.depth, self._n or "auto")

    @staticmethod
    def _resolve_device(core, device: str) -> str:
        """Keep only devices that are actually present. MULTI:GPU,NPU on a box with
        no NPU collapses to GPU; an unknown plain device falls back to GPU/CPU."""
        avail = list(core.available_devices)  # e.g. ["CPU", "GPU", "NPU"] (or "GPU.0")
        present = lambda d: any(a == d or a.startswith(d + ".") for a in avail)
        if ":" in device:                      # MULTI:GPU,NPU / AUTO:GPU,NPU
            prefix, rest = device.split(":", 1)
            wanted = [d for d in rest.split(",") if present(d)]
            if not wanted:
                return "GPU" if present("GPU") else "CPU"
            return wanted[0] if len(wanted) == 1 else f"{prefix}:{','.join(wanted)}"
        if present(device) or device == "AUTO":
            return device
        return "GPU" if present("GPU") else "CPU"

    def submit(self, crop_bgr: np.ndarray, userdata: Tuple) -> None:
        if crop_bgr is None or crop_bgr.size == 0:
            return
        # Preprocess outside the lock (per-thread work). NCHW RGB, no /255.
        rgb = cv2.cvtColor(cv2.resize(crop_bgr, (self.img_w, self.img_h)), cv2.COLOR_BGR2RGB)
        arr = rgb.astype(np.float32).transpose(2, 0, 1)[None]
        # is_ready()+start_async() must be atomic: the all-OpenVINO worker calls submit
        # from many camera threads, and otherwise two threads grab the same idle request
        # ("Infer Request is busy"). All requests busy -> drop (the track is read on a
        # later frame); never block the caller.
        with self._lock:
            if not self._q.is_ready():
                self.dropped += 1
                return
            self.submitted += 1
            self._q.start_async({0: arr}, userdata)

    def _on_done(self, request, userdata: Tuple) -> None:
        try:
            logits = np.asarray(request.get_output_tensor(0).data)
            idx = np.argmax(logits, axis=-1)[0]
            text = "".join(ALPHABET[i] for i in idx if ALPHABET[i] != PAD)
            if self.on_result is not None:
                self.on_result(text, userdata)
        except Exception:  # noqa: BLE001
            logger.exception("async OCR callback failed")

    def wait(self) -> None:
        self._q.wait_all()
