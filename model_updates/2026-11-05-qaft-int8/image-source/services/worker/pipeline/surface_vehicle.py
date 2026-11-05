"""OpenVINO vehicle inference directly from a legacy VAAPI surface."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

import numpy as np

from detectors.backends.openvino_detector import yolo_detections


def _library_path() -> str:
    candidates = [
        Path(os.getenv("FINALCOMBO_DIR", "/opt/finalcombo"))
        / "libva_surface_vehicle.so",
    ]
    # Developer checkouts and the installed image have different directory
    # depths. Search ancestors instead of indexing a depth that may not exist.
    candidates.extend(
        parent / "native/lib/libva_surface_vehicle.so"
        for parent in Path(__file__).resolve().parents
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("VA-surface vehicle detector library is unavailable")


class SurfaceVehicleDetector:
    """Keep all frame pixels on the GPU and return ordinary Detection objects."""

    def __init__(
        self,
        decoder_pointer: int,
        model_path: str,
        *,
        confidence: float,
        class_ids: set[int],
        class_names: dict[int, str],
    ) -> None:
        self.lib = ctypes.CDLL(_library_path())
        self.lib.va_surface_vehicle_create.argtypes = [
            ctypes.c_size_t, ctypes.c_char_p,
        ]
        self.lib.va_surface_vehicle_create.restype = ctypes.c_void_p
        self.lib.va_surface_vehicle_infer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.lib.va_surface_vehicle_infer.restype = ctypes.c_int
        self.lib.va_surface_vehicle_last_error.restype = ctypes.c_char_p
        self.lib.va_surface_vehicle_destroy.argtypes = [ctypes.c_void_p]
        self.handle = self.lib.va_surface_vehicle_create(
            int(decoder_pointer), os.fsencode(model_path)
        )
        if not self.handle:
            raise RuntimeError(self.error())
        self.confidence = float(confidence)
        self.class_ids = set(class_ids)
        self.class_names = dict(class_names)
        self.exec_devices = "GPU/VA-RemoteTensor"

    def error(self) -> str:
        value = self.lib.va_surface_vehicle_last_error()
        return value.decode(errors="replace") if value else "VA surface inference error"

    def detect_surface(self, surface_id: int):
        rows = np.zeros((1, 300, 6), dtype=np.float32)
        kept = ctypes.c_int()
        status = self.lib.va_surface_vehicle_infer(
            self.handle,
            int(surface_id),
            rows.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            rows.shape[1],
            ctypes.byref(kept),
        )
        if status != 0:
            raise RuntimeError(self.error())
        # Native VPP scales 1920x1080 to 640x360 and PPP pads 140 pixels on
        # top/bottom. This is exactly the current Python letterbox transform.
        return yolo_detections(
            rows[:, :kept.value],
            image_shape=(1080, 1920),
            scale=1.0 / 3.0,
            pad_x=0,
            pad_y=140,
            confidence=self.confidence,
            class_ids=self.class_ids,
            class_names=self.class_names,
            model_name="vehicle",
        )

    def close(self) -> None:
        if self.handle:
            self.lib.va_surface_vehicle_destroy(self.handle)
            self.handle = None
