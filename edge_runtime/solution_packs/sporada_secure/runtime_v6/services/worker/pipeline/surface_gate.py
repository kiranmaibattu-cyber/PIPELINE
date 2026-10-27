"""VA-surface motion gate service for the worker.

Wraps the tuned fixed-slots gate (mag0.2/cons0.42/red25+height, hybrid CPU
corner re-find via MOTION_GATE_REDETECT_CPU=1): votes on VA surface IDs with
~8 bytes crossing per frame. The CV runtime enables it internally; desired
state does not expose motion gating as a Management-controlled setting.
"""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_CANDIDATES = [
    Path(os.getenv("FINALCOMBO_DIR", "/opt/finalcombo")) / "libgpu_motion_gate_fixed_slots.so",
    Path("/home/admin1/Downloads/motion_gate_package/working_pipeline/libgpu_motion_gate_fixed_slots.so"),
]

_OCV_CANDIDATES = [
    Path(os.getenv("OCV_LIB", "/opt/finalcombo/ocvlib")),
    Path("/home/admin1/motion_gate_benchmark/opencv-local/root/usr/lib/x86_64-linux-gnu"),
]


def _gate_library() -> ctypes.CDLL:
    for extra in _OCV_CANDIDATES:
        if extra.is_dir():
            prev = os.environ.get("LD_LIBRARY_PATH", "")
            if str(extra) not in prev:
                os.environ["LD_LIBRARY_PATH"] = f"{extra}:{prev}"
    for path in _CANDIDATES:
        if path.exists():
            lib = ctypes.CDLL(str(path))
            lib.gpu_motion_create.argtypes = [ctypes.c_size_t]
            lib.gpu_motion_create.restype = ctypes.c_void_p
            lib.gpu_motion_update.argtypes = [
                ctypes.c_void_p, ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            lib.gpu_motion_update.restype = ctypes.c_int
            lib.gpu_motion_reset.argtypes = [ctypes.c_void_p]
            lib.gpu_motion_destroy.argtypes = [ctypes.c_void_p]
            lib.gpu_motion_last_error.restype = ctypes.c_char_p
            return lib
    raise RuntimeError("GPU motion gate library missing")


class SurfaceGate:
    """Per-stream gate: create from the VA/OpenCL bridge context."""

    def __init__(self, context: int) -> None:
        self._lib = _gate_library()
        handle = self._lib.gpu_motion_create(context)
        if not handle:
            err = self._lib.gpu_motion_last_error()
            raise RuntimeError(err.decode(errors="replace") if err else
                               "surface gate create failed")
        self._handle = handle

    def update(self, image: int) -> bool:
        cand = ctypes.c_int()
        pts = ctypes.c_int()
        if self._lib.gpu_motion_update(self._handle, image,
                                       ctypes.byref(cand),
                                       ctypes.byref(pts)) != 0:
            err = self._lib.gpu_motion_last_error()
            raise RuntimeError(err.decode(errors="replace") if err else
                               "surface gate update failed")
        return bool(cand.value)

    def reset(self) -> None:
        self._lib.gpu_motion_reset(self._handle)

    def close(self) -> None:
        if self._handle:
            self._lib.gpu_motion_destroy(self._handle)
            self._handle = None
