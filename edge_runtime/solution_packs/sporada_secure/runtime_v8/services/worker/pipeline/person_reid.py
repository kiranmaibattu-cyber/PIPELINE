"""OpenVINO body and gait extractors used by the Sentinel V7 event pipeline."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time
from typing import Any
import uuid

import cv2
import numpy as np
from PIL import Image

BODY_MODEL_ID = "transreid_ssl_int8"
BODY_MODEL_VERSION = "v1"
BODY_DIMENSIONS = 384
BODY_EMBEDDING_SPACE = "transreid_ssl_int8@v1:l2:384"
FACE_MODEL_ID = "face-embedding-model-v1"
FACE_MODEL_VERSION = "adaface_ir101_int8-v1"
FACE_DIMENSIONS = 512
FACE_EMBEDDING_SPACE = "adaface-ir101-int8@v1:aligned112:l2:512"
GAIT_MODEL_ID = "gaitbase_int8"
GAIT_MODEL_VERSION = "v1"
GAIT_DIMENSIONS = 4096
GAIT_EMBEDDING_SPACE = "gaitbase_int8@v1:30x64x44:parts16x256:l2:4096"


def _l2(vector: np.ndarray, expected: int) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    if vector.size != expected or not np.isfinite(vector).all():
        raise ValueError(f"embedding must contain {expected} finite values")
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("embedding must not be zero")
    return vector / norm


class OpenVINOBodyExtractor:
    model_id = BODY_MODEL_ID
    model_version = BODY_MODEL_VERSION
    dimension = BODY_DIMENSIONS

    def __init__(self, model_path: str, device: str = "NPU") -> None:
        try:
            import openvino as ov
        except ImportError as exc:
            raise RuntimeError("OpenVINO is required for body Re-ID") from exc
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"body Re-ID model is missing: {path}")
        core = ov.Core()
        self.compiled = core.compile_model(core.read_model(str(path)), device)
        self.input = self.compiled.input(0)
        self.output = self.compiled.output(0)
        shape = list(self.input.shape)
        self.height, self.width = int(shape[2]), int(shape[3])

    def warmup(self) -> None:
        self.embed([np.zeros((self.height, self.width, 3), dtype=np.uint8)])

    def _prepare(self, crop: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        rgb = np.asarray(
            Image.fromarray(rgb).resize((self.width, self.height), Image.BILINEAR),
            dtype=np.float32,
        )
        tensor = ((rgb / 255.0) - 0.5) / 0.5
        return np.transpose(tensor, (2, 0, 1))[None]

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        vectors = []
        for crop in crops:
            result = np.asarray(
                self.compiled([self._prepare(crop)])[self.output], dtype=np.float32
            )
            vectors.append(_l2(result, self.dimension))
        return np.stack(vectors) if vectors else np.empty((0, self.dimension), np.float32)


def opengait_pretreat(mask: np.ndarray, target_h: int = 64, target_w: int = 44):
    """Normalize a binary person mask using the OpenGait silhouette convention."""
    if mask is None or mask.size == 0 or mask.max() == 0:
        return None
    ys = np.where(mask.any(axis=1))[0]
    if len(ys) < 10:
        return None
    mask = mask[ys[0]:ys[-1] + 1]
    height, width = mask.shape
    resized_width = max(1, int(width * target_h / height))
    mask = cv2.resize(mask, (resized_width, target_h), interpolation=cv2.INTER_NEAREST)
    weights = mask.sum(axis=0)
    if weights.sum() == 0:
        return None
    center = int(round((weights * np.arange(len(weights))).sum() / weights.sum()))
    half = target_w // 2
    padded = np.zeros((target_h, len(weights) + 2 * target_w), dtype=mask.dtype)
    padded[:, target_w:target_w + len(weights)] = mask
    result = padded[:, center + target_w - half:center + target_w + half]
    if result.shape != (target_h, target_w):
        return None
    return (result > 0).astype(np.uint8) * 255


class OpenVINOGaitExtractor:
    model_id = GAIT_MODEL_ID
    model_version = GAIT_MODEL_VERSION
    dimension = GAIT_DIMENSIONS

    def __init__(
        self,
        gait_model_path: str,
        gait_device: str = "NPU",
        min_sequence: int = 20,
        max_sequence: int = 60,
        minimum_motion_pixels: float = 2.0,
    ) -> None:
        try:
            import openvino as ov
        except ImportError as exc:
            raise RuntimeError("OpenVINO is required for gait Re-ID") from exc
        self.source = "background_subtraction"
        core = ov.Core()
        gait_path = Path(gait_model_path)
        if not gait_path.is_file():
            raise FileNotFoundError(f"GaitBase model is missing: {gait_path}")
        self.gait = core.compile_model(core.read_model(str(gait_path)), gait_device)
        self.gait_output = self.gait.output(0)
        self.sequence_length = int(self.gait.input(0).shape[1])
        self.min_sequence = int(min_sequence)
        self.max_sequence = int(max_sequence)
        self.buffers: dict[str, list[np.ndarray]] = defaultdict(list)
        self.buffer_times: dict[str, list[float]] = defaultdict(list)
        self.cache: dict[str, np.ndarray] = {}
        self.backgrounds: dict[str, Any] = {}
        self.minimum_motion_pixels = float(minimum_motion_pixels)
        self.centers: dict[tuple[str, int], np.ndarray] = {}

    def warmup(self) -> None:
        sequence = np.zeros((1, self.sequence_length, 64, 44), dtype=np.float32)
        self.gait([sequence])

    def collect(self, frame: np.ndarray, people: list[Any], camera_id: str,
                keys: dict[int, str], captured_at: float | None = None) -> dict[int, np.ndarray]:
        masks = self._silhouettes(frame, people, camera_id)
        output: dict[int, np.ndarray] = {}
        for person, mask in zip(people, masks):
            track_id = int(person.metadata["track_id"])
            x1, y1, x2, y2 = person.bbox
            center = np.asarray(((x1 + x2) / 2.0, (y1 + y2) / 2.0), np.float32)
            center_key = (camera_id, track_id)
            previous_center = self.centers.get(center_key)
            self.centers[center_key] = center
            if (previous_center is None or
                    float(np.linalg.norm(center - previous_center))
                    < self.minimum_motion_pixels):
                continue
            if mask is None:
                continue
            key = keys.get(track_id, f"track:{track_id}")
            buffer = self.buffers[key]
            buffer.append(mask)
            self.buffer_times[key].append(float(captured_at if captured_at is not None else time.time()))
            if len(buffer) > self.max_sequence:
                del buffer[:-self.max_sequence]
                del self.buffer_times[key][:-self.max_sequence]
            if len(buffer) < self.min_sequence:
                continue
            indices = np.linspace(0, len(buffer) - 1, self.sequence_length).round().astype(int)
            sequence = np.stack(buffer)[indices].astype(np.float32) / 255.0
            result = np.asarray(self.gait([sequence[None]])[self.gait_output])
            vector = _l2(result, self.dimension)
            self.cache[key] = vector
            output[track_id] = vector
        return output

    def cached(self, key: str) -> np.ndarray | None:
        value = self.cache.get(key)
        return value.copy() if value is not None else None

    def rekey(self, old_key: str, new_key: str) -> None:
        if old_key == new_key:
            return
        if old_key in self.buffers:
            merged = self.buffers[new_key] + self.buffers.pop(old_key)
            self.buffers[new_key] = merged[-self.max_sequence:]
            merged_times = self.buffer_times[new_key] + self.buffer_times.pop(old_key)
            self.buffer_times[new_key] = merged_times[-self.max_sequence:]
        if old_key in self.cache:
            self.cache[new_key] = self.cache.pop(old_key)

    def _silhouettes(self, frame: np.ndarray, people: list[Any], camera_id: str):
        background = self.backgrounds.get(camera_id)
        if background is None:
            background = cv2.createBackgroundSubtractorMOG2(
                history=300, varThreshold=25, detectShadows=False
            )
            self.backgrounds[camera_id] = background
        foreground = background.apply(frame)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, kernel)
        foreground = cv2.morphologyEx(
            foreground, cv2.MORPH_CLOSE, kernel, iterations=2
        )
        return [opengait_pretreat(self._crop(foreground, p.bbox)) for p in people]

    @staticmethod
    def _crop(image: np.ndarray, bbox):
        height, width = image.shape[:2]
        x1, y1, x2, y2 = [int(value) for value in bbox]
        return image[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
