"""Multi-modal Re-ID feature extractors.

Provides three feature types beyond pure appearance embeddings:
  - Face Re-ID via InsightFace ArcFace (512-D)
  - HSV colour-histogram soft-biometrics (72-D)
  - Feature-level fusion (concatenate N sub-model embeddings)

Score-level fusion lives in reid_benchmark.fusion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 1e-12 else vector


# ---------------------------------------------------------------------------
# Colour-histogram soft-biometric model
# ---------------------------------------------------------------------------

class ColorHistModel:
    """HSV colour histogram over three horizontal body bands.

    Bands: head (top 1/3), torso (middle 1/3), legs (bottom 1/3).
    Per band: 16-bin Hue + 8-bin Saturation histograms (V ignored for
    illumination robustness).  Output: 72-D L2-normalised vector.
    """

    def __init__(self) -> None:
        self.backend = "color_hist:hsv_3band_72d:none"
        self.device = "cpu"

    def load(self) -> tuple[bool, str]:
        return True, self.backend

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 72), dtype=np.float32)
        results = []
        for crop in crops:
            results.append(self._compute(crop))
        return np.vstack(results)

    def _compute(self, crop: np.ndarray) -> np.ndarray:
        h, w = crop.shape[:2]
        if h < 3:
            return np.zeros(72, dtype=np.float32)
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        thirds = [
            hsv[: h // 3, :],
            hsv[h // 3 : 2 * h // 3, :],
            hsv[2 * h // 3 :, :],
        ]
        parts: list[np.ndarray] = []
        for band in thirds:
            if band.size == 0:
                parts.append(np.zeros(24, dtype=np.float32))
                continue
            h_hist = cv2.calcHist([band], [0], None, [16], [0, 180]).flatten()
            s_hist = cv2.calcHist([band], [1], None, [8], [0, 256]).flatten()
            parts.append(np.concatenate([h_hist, s_hist]))
        feat = np.concatenate(parts).astype(np.float32)
        return l2_normalize(feat)


# ---------------------------------------------------------------------------
# Face Re-ID model (InsightFace ArcFace)
# ---------------------------------------------------------------------------

class FaceReIDModel:
    """Detect faces within person crops and extract ArcFace embeddings.

    Uses buffalo_s (small, fast) by default.  Auto-downloads weights to
    models/face_reid/.  Returns a 512-D ArcFace vector per crop; returns a
    zero vector if no face is detected (resulting in cosine distance = 1.0
    against all gallery entries, so face-less crops will spawn new identities
    unless the gallery also has face-less entries).
    """

    DIM = 512

    def __init__(self) -> None:
        self._app: Any = None
        self.backend = "unloaded"
        self.device = "cpu"

    def load(self, model_name: str = "buffalo_s") -> tuple[bool, str]:
        try:
            from insightface.app import FaceAnalysis
        except ImportError:
            return False, "insightface not installed; run: pip install insightface"

        try:
            import torch
            use_cuda = torch.cuda.is_available()
        except ImportError:
            use_cuda = False

        model_root = Path("models") / "face_reid"
        model_root.mkdir(parents=True, exist_ok=True)
        ctx_id = 0 if use_cuda else -1
        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if use_cuda
            else ["CPUExecutionProvider"]
        )
        try:
            app = FaceAnalysis(
                name=model_name,
                root=str(model_root),
                providers=providers,
            )
            app.prepare(ctx_id=ctx_id, det_size=(320, 320))
        except Exception as exc:  # noqa: BLE001
            return False, f"InsightFace load failed: {exc}"

        self._app = app
        self.device = "cuda" if use_cuda else "cpu"
        self.backend = f"insightface:{model_name}:arcface:{self.device}"
        return True, self.backend

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, self.DIM), dtype=np.float32)
        results = []
        for crop in crops:
            results.append(self._embed_one(crop))
        return np.vstack(results)

    def _embed_one(self, crop: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        try:
            faces = self._app.get(rgb)
        except Exception:  # noqa: BLE001
            faces = []
        if not faces:
            return np.zeros(self.DIM, dtype=np.float32)
        # Pick largest face by bounding-box area
        best = max(
            faces,
            key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
        )
        return l2_normalize(np.asarray(best.embedding, dtype=np.float32))


# ---------------------------------------------------------------------------
# Feature-level fusion model
# ---------------------------------------------------------------------------

class FeatureFusionModel:
    """Concatenates embeddings from N sub-models into a single descriptor.

    Each sub-model is loaded independently.  Their outputs are concatenated
    along the feature axis and then L2-normalised.  Compatible with the
    existing GlobalMatcher (which just needs any fixed-size embedding vector).
    """

    def __init__(self, key: str, sub_keys: tuple[str, ...]) -> None:
        self.key = key
        self.sub_keys = sub_keys
        self._sub_models: list[Any] = []
        self.backend = "unloaded"
        self.device = "cpu"

    def load(self) -> tuple[bool, str]:
        # Import here to avoid circular dependency
        from .runner import ReIDModel  # type: ignore[attr-defined]

        self._sub_models = []
        for k in self.sub_keys:
            sub = ReIDModel(k)
            ok, info = sub.load()
            if not ok:
                return False, f"Feature-fusion sub-model '{k}' failed: {info}"
            self._sub_models.append(sub)

        devices = {m.device for m in self._sub_models}
        self.device = "cuda" if "cuda" in devices else "cpu"
        self.backend = f"feature_fusion:{'_+_'.join(self.sub_keys)}:{self.device}"
        return True, self.backend

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1), dtype=np.float32)
        parts = [m.embed(crops) for m in self._sub_models]
        combined = np.concatenate(parts, axis=1)
        return np.vstack([l2_normalize(row) for row in combined])
