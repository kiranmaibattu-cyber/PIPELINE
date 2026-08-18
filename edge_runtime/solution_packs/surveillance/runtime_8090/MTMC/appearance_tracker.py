"""Appearance-aware IoU tracker — reduces crowd / walk-in-front ID swaps.

Plain IoU tracking swaps identities when two people's boxes overlap (a crowd, or
one passing in front of another) because it associates by box overlap alone. This
tracker keeps IoU as the SPATIAL GATE (a track may only match a nearby detection)
but lets APPEARANCE decide among the candidates, via Hungarian assignment on
embedding distance. A high-overlap match whose appearance strongly disagrees
(occluder in front of the tracked person) is rejected, so the tracked person
coasts instead of being stolen.

Drop-in for reid_benchmark.IoUTracker but update() also takes per-detection
embeddings: update(detections, embeddings, frame_idx) -> list[Track].
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reid_benchmark.runner import Track, xyxy_iou  # reuse the same Track dataclass

try:
    from scipy.optimize import linear_sum_assignment
    _HAVE_SCIPY = True
except Exception:  # noqa: BLE001
    _HAVE_SCIPY = False


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


class AppearanceIoUTracker:
    def __init__(self, max_age_frames: int = 40, iou_threshold: float = 0.2,
                 app_gate: float = 0.55, ema: float = 0.9) -> None:
        self.max_age_frames = max_age_frames
        self.iou_threshold = iou_threshold   # spatial gate
        self.app_gate = app_gate             # reject a match if app-dist exceeds this
        self.ema = ema
        self.next_id = 1
        self.tracks: dict[int, Track] = {}

    def update(self, detections: list[np.ndarray], embeddings, frame_idx: int) -> list[Track]:
        tracks = list(self.tracks.values())
        n_t, n_d = len(tracks), len(detections)
        has_emb = embeddings is not None and getattr(embeddings, "shape", (0, 0))[0] == n_d \
            and (n_d == 0 or embeddings.shape[1] > 1)

        matched_t: set[int] = set()
        matched_d: set[int] = set()

        if n_t and n_d:
            BIG = 10.0
            cost = np.full((n_t, n_d), BIG, dtype=np.float32)
            for i, tr in enumerate(tracks):
                for j, det in enumerate(detections):
                    iou = xyxy_iou(tr.bbox, det)
                    if iou < self.iou_threshold:
                        continue  # spatial gate: not a candidate
                    if has_emb and tr.embedding is not None:
                        app = float(1.0 - np.dot(_l2(tr.embedding), _l2(embeddings[j])))
                    else:
                        app = 1.0 - iou  # fall back to IoU if no embedding
                    cost[i, j] = app
            # Hungarian on appearance cost among spatially-valid pairs
            if _HAVE_SCIPY:
                rows, cols = linear_sum_assignment(cost)
                pairs = [(r, c) for r, c in zip(rows, cols) if cost[r, c] < BIG]
            else:
                pairs = []
                used_c = set()
                for r in np.argsort(cost.min(axis=1)):
                    c = int(np.argmin(cost[r]))
                    if cost[r, c] < BIG and c not in used_c:
                        pairs.append((r, c)); used_c.add(c)
            for r, c in pairs:
                # reject appearance-mismatched matches (occluder in front)
                if has_emb and tracks[r].embedding is not None and cost[r, c] > self.app_gate:
                    continue
                tr = tracks[r]
                tr.bbox = detections[c]
                tr.last_seen_frame = frame_idx
                tr.hits += 1
                if has_emb:
                    e = _l2(embeddings[c])
                    tr.embedding = _l2(self.ema * tr.embedding + (1 - self.ema) * e) \
                        if tr.embedding is not None else e
                    tr.cur_embedding = embeddings[c]   # raw current-frame emb for the gallery
                matched_t.add(r); matched_d.add(c)

        # unmatched detections -> new tracks
        for j in range(n_d):
            if j in matched_d:
                continue
            tr = Track(self.next_id, detections[j], frame_idx)
            if has_emb:
                tr.embedding = _l2(embeddings[j])
                tr.cur_embedding = embeddings[j]
            self.tracks[self.next_id] = tr
            self.next_id += 1

        # expire stale tracks
        for lid in [l for l, tr in self.tracks.items()
                    if frame_idx - tr.last_seen_frame > self.max_age_frames]:
            del self.tracks[lid]

        return [tr for tr in self.tracks.values() if tr.last_seen_frame == frame_idx]
