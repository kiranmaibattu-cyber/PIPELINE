"""Thin adapters over reid_benchmark — originals stay unmodified.

  - MultiClassDetector : YOLO detector accepting a set of COCO class ids
  - TTAEmbedder        : wraps any ReIDModel; horizontal-flip test-time averaging
  - load_embedder      : registry-aware loader (existing keys via reid_benchmark,
                         new tournament keys via MTMC loaders)
  - make_tracker       : IoUTracker (native) or boxmot trackers (soft dependency)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

# Make the repo root importable regardless of CWD
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reid_benchmark.runner import (  # noqa: E402
    Detector,
    IoUTracker,
    ReIDModel,
    Track,
    crop_boxes,
    draw_tracks,
    hstack_resize,
    l2_normalize,
    CALIBRATED_THRESHOLDS,
)

__all__ = [
    "MultiClassDetector", "TTAEmbedder", "load_embedder", "make_tracker",
    "Track", "IoUTracker", "crop_boxes", "draw_tracks", "hstack_resize",
    "l2_normalize", "CALIBRATED_THRESHOLDS",
]


class MultiClassDetector:
    """Detector wrapper that keeps boxes for a SET of class ids.

    reid_benchmark.Detector filters to one person_class_id; vehicles need
    {2,3,5,7}. We reuse its YOLO model/device setup and re-filter here.
    """

    def __init__(self, model_name: str, conf: float, iou: float, class_ids: set[int]) -> None:
        self._inner = Detector(model_name, conf, iou, person_class_id=-1)
        self.class_ids = set(class_ids)
        self.conf = conf
        self.iou = iou
        self.last_confidences: list[float] = []

    def detect(self, frame: np.ndarray) -> list[np.ndarray]:
        self.last_confidences = []
        results = self._inner.model.predict(
            frame, conf=self.conf, iou=self.iou, verbose=False, device=self._inner.device
        )
        boxes: list[np.ndarray] = []
        if not results:
            return boxes
        res = results[0]
        if res.boxes is None:
            return boxes
        for box in res.boxes:
            cls_id = int(box.cls.item()) if box.cls is not None else -1
            if cls_id in self.class_ids:
                boxes.append(box.xyxy[0].cpu().numpy())
                self.last_confidences.append(float(box.conf.item()) if box.conf is not None else 1.0)
        return boxes


class TTAEmbedder:
    """Wraps a loaded ReIDModel; embed() averages original + horizontally
    flipped crops (single extra batch, applied once at the entry point —
    works for every backend without touching the ~12 _embed_* methods)."""

    def __init__(self, reid: ReIDModel, flip: bool = True) -> None:
        self.reid = reid
        self.flip = flip
        self.key = reid.key
        self.backend = reid.backend

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return self.reid.embed(crops)
        base = self.reid.embed(crops)
        if not self.flip or base.size == 0 or base.shape[1] <= 1:
            return base
        flipped = self.reid.embed([cv2.flip(c, 1) for c in crops])
        if flipped.shape != base.shape:
            return base
        avg = base + flipped
        return np.vstack([l2_normalize(avg[i]) for i in range(avg.shape[0])])


def load_embedder(key: str, tta_flip: bool = True) -> tuple[Any, str]:
    """Load an appearance embedder by key.

    Existing reid_benchmark keys go through ReIDModel. New tournament keys
    (osnet_ibn, agw, mgn, solider, transreid_ssl, lightmbn) are handled by
    MTMC.new_models. Returns (embedder_or_None, detail).

    NOTE: kpr/bpbreid require albumentations==1.3.1 (their vendored
    torchreid fork's masks_transforms target that API); nothing else in this
    project needs a newer albumentations, so the environment is pinned to it.
    """
    from reid_benchmark.registry import MODEL_REGISTRY

    if key in MODEL_REGISTRY:
        reid = ReIDModel(key)
        ok, detail = reid.load()
        if not ok:
            return None, detail
        return TTAEmbedder(reid, flip=tta_flip), reid.backend

    try:
        from MTMC.new_models import load_new_embedder
    except ImportError as exc:
        return None, f"MTMC.new_models unavailable: {exc}"
    return load_new_embedder(key, tta_flip=tta_flip)


class _BoxmotTrackerShim:
    """Adapts a boxmot tracker to the IoUTracker.update(detections, frame_idx)
    -> list[Track] interface used by the pipeline runner."""

    def __init__(self, name: str) -> None:
        import torch
        try:
            from boxmot import create_tracker  # type: ignore  # boxmot <= 13
        except ImportError:
            from boxmot.trackers.tracker_zoo import create_tracker  # type: ignore  # boxmot >= 19

        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        # Motion-only trackers run appearance-free (our pipeline supplies
        # embeddings separately); appearance-based ones get a lightweight
        # auto-downloaded OSNet so their internal re-association works.
        needs_reid = name in ("botsort", "strongsort", "deepocsort", "boosttrack")
        reid_weights = Path("models") / "boxmot_reid" / "osnet_x0_25_msmt17.pt" if needs_reid else None
        if reid_weights is not None:
            reid_weights.parent.mkdir(parents=True, exist_ok=True)
        self._tracker = create_tracker(
            tracker_type=name, tracker_config=None, reid_weights=reid_weights,
            device=device, half=False, per_class=False,
        )
        self._tracks: dict[int, Track] = {}

    def update(self, detections: list[np.ndarray], frame_idx: int, frame: np.ndarray | None = None) -> list[Track]:
        if detections:
            dets = np.array([[*d, 0.9, 0] for d in detections], dtype=np.float32)
        else:
            dets = np.empty((0, 6), dtype=np.float32)
        img = frame if frame is not None else np.zeros((1440, 2560, 3), dtype=np.uint8)
        out = self._tracker.update(dets, img)  # M x (x1,y1,x2,y2,id,conf,cls,ind)
        alive: list[Track] = []
        for row in out:
            tid = int(row[4])
            bbox = row[:4].astype(np.float32)
            tr = self._tracks.get(tid)
            if tr is None:
                tr = Track(tid, bbox, frame_idx)
                self._tracks[tid] = tr
            else:
                tr.bbox = bbox
                tr.last_seen_frame = frame_idx
                tr.hits += 1
            alive.append(tr)
        return alive


def make_tracker(name: str, max_age_frames: int = 40, iou_threshold: float = 0.25) -> Any:
    """name: iou | sort | ocsort | bytetrack | botsort | strongsort | deepocsort.
    `iou` and `sort` are native (no deps beyond numpy/scipy); `sort` adds Kalman motion
    prediction + Hungarian association so crossings/occlusions keep their id. The remaining
    boxmot trackers are a soft dependency — raises RuntimeError with a clear reason if boxmot
    is missing so callers can skip and record it."""
    if name == "iou":
        return IoUTracker(max_age_frames=max_age_frames, iou_threshold=iou_threshold)
    if name in ("sort", "ocsort"):
        from MTMC.sort_tracker import SortTracker            # native, no torch/boxmot
        return SortTracker(max_age_frames=max_age_frames, iou_threshold=iou_threshold)
    if name in ("bytetrack", "byte"):
        import os
        from MTMC.bytetrack_tracker import ByteTrackTracker
        return ByteTrackTracker(
            max_age_frames=max_age_frames,
            iou_threshold=iou_threshold,
            high_thresh=float(os.environ.get("BYTE_HIGH_THRESH", "0.45")),
            low_thresh=float(os.environ.get("BYTE_LOW_THRESH", "0.10")),
            new_thresh=float(os.environ.get("BYTE_NEW_THRESH", "0.55")),
        )
    try:
        return _BoxmotTrackerShim(name)
    except ImportError as exc:
        raise RuntimeError(f"tracker '{name}' needs boxmot: {exc}") from exc
