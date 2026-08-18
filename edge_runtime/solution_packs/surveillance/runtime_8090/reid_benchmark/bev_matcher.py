"""BEV (Bird's Eye View) cross-camera person matcher.

What this does
--------------
Given the homography H calibrated by calibrate_bev.py, this module matches
persons detected simultaneously in ch9 and ch10 by projecting their foot-points
(bottom-centre of bounding box) through H and computing euclidean distance in
ch10's pixel coordinate space.

Two detections are the SAME person if their projected foot-point distance is
below foot_threshold_px — regardless of how their appearance embeddings compare.

Why foot-points?
---------------
The homography H maps any point on the GROUND PLANE from ch9 to ch10.  A
person stands on the ground, so their foot (bottom of bbox) lies on that plane.
Projecting foot-points therefore gives us the true geometric position, bypassing
all view-angle appearance differences.

Usage
-----
    matcher = BEVMatcher.from_calibration("configs/bev_calibration.json")
    pairs = matcher.match(ch9_tracks, ch10_tracks)
    # pairs: list of (ch9_track_index, ch10_track_index)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def _project_points(H: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Project N x 2 points through 3x3 homography. Returns N x 2."""
    if len(points) == 0:
        return np.empty((0, 2), dtype=np.float32)
    pts_h = np.hstack([points, np.ones((len(points), 1), dtype=np.float64)])
    proj_h = (H @ pts_h.T).T          # N x 3
    proj = proj_h[:, :2] / proj_h[:, 2:3]
    return proj.astype(np.float32)


def _foot_points(tracks) -> np.ndarray:
    """Extract foot-point (bottom-centre of bbox) for each track. Returns N x 2."""
    pts = []
    for t in tracks:
        x1, y1, x2, y2 = t.bbox
        pts.append([(x1 + x2) / 2.0, float(y2)])
    return np.array(pts, dtype=np.float64) if pts else np.empty((0, 2), dtype=np.float64)


def _hungarian_match(cost: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    """Optimal assignment via scipy; falls back to greedy if scipy unavailable."""
    if cost.size == 0:
        return []
    try:
        from scipy.optimize import linear_sum_assignment
        row_ind, col_ind = linear_sum_assignment(cost)
        return [(int(r), int(c)) for r, c in zip(row_ind, col_ind) if cost[r, c] <= threshold]
    except ImportError:
        # Greedy fallback: take smallest distances first
        pairs = []
        used_r, used_c = set(), set()
        flat = sorted(np.ndindex(*cost.shape), key=lambda idx: cost[idx])
        for r, c in flat:
            if r not in used_r and c not in used_c and cost[r, c] <= threshold:
                pairs.append((r, c))
                used_r.add(r)
                used_c.add(c)
        return pairs


class BEVMatcher:
    def __init__(self, H: np.ndarray, threshold_px: float) -> None:
        self.H = H
        self.threshold_px = threshold_px

    @classmethod
    def from_calibration(cls, path: str | Path) -> "BEVMatcher":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        H = np.array(data["H_ch9_to_ch10"], dtype=np.float64)
        threshold = float(data.get("foot_threshold_px", 100.0))
        return cls(H, threshold)

    def match(self, ch9_tracks: list, ch10_tracks: list) -> list[tuple[int, int]]:
        """Return list of (ch9_index, ch10_index) pairs for same-person detections.

        Parameters
        ----------
        ch9_tracks : list of Track objects with .bbox (x1,y1,x2,y2)
        ch10_tracks : list of Track objects with .bbox (x1,y1,x2,y2)

        Returns
        -------
        List of index pairs — each pair means those two tracks show the same person.
        """
        if not ch9_tracks or not ch10_tracks:
            return []

        feet_a = _foot_points(ch9_tracks)        # N x 2 in ch9 pixel space
        feet_b = _foot_points(ch10_tracks)        # M x 2 in ch10 pixel space
        proj_a = _project_points(self.H, feet_a)  # N x 2 projected to ch10 space

        # Distance matrix: (N, M) euclidean distances in ch10 pixel space
        diff = proj_a[:, np.newaxis, :] - feet_b[np.newaxis, :, :]  # N x M x 2
        cost = np.sqrt((diff ** 2).sum(axis=2))                      # N x M

        return _hungarian_match(cost, self.threshold_px)

    def project_foot(self, x1: float, y1: float, x2: float, y2: float) -> tuple[float, float]:
        """Project a single ch9 bbox foot-point to ch10 coordinates."""
        foot = np.array([[(x1 + x2) / 2.0, y2]], dtype=np.float64)
        proj = _project_points(self.H, foot)
        return float(proj[0, 0]), float(proj[0, 1])
