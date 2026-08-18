"""Native SORT / OC-SORT-style motion tracker -- the edge-clean upgrade from the greedy
IoUTracker, with NO torch/boxmot dependency (pure numpy + scipy, both already on the box).

Why: the engine's IoUTracker associates by greedy single-best IoU only -- no motion model --
so two people who cross or stand close SWAP ids, and a one-frame detection gap (a person
sitting / briefly occluded) drops the track and re-mints a new id. This tracker adds:

  * a constant-velocity KALMAN filter per track (state [cx,cy,s,r,vx,vy,vs], SORT's model)
    -> it PREDICTS where a track is on the next frame, so association survives a gap and a
    crossing is disambiguated by motion, not just overlap.
  * HUNGARIAN assignment (scipy.linear_sum_assignment) over predicted-vs-detection IoU
    instead of greedy nearest -- globally consistent, so two nearby tracks don't both grab
    the same detection.
  * a lost-track buffer (max_age) during which a coasting track keeps predicting; when the
    person reappears it RE-ASSOCIATES to the SAME local_id (id preserved across the gap).
  * OC-SORT's observation-centric re-update: on re-acquisition after a gap, re-seed the
    Kalman with a virtual trajectory between the last real observation and the new one, so
    the velocity doesn't diverge during the coast (reduces id switches after occlusion).

Interface matches IoUTracker exactly: update(detections, frame_idx) -> list[Track], where
`detections` is a list of xyxy boxes and only tracks MATCHED to a detection this frame are
returned (downstream re-id embeds those crops; coasting/predicted-only tracks are withheld).
"""
from __future__ import annotations

import numpy as np

try:
    from scipy.optimize import linear_sum_assignment
except Exception:                                   # pragma: no cover - scipy always on box
    linear_sum_assignment = None

from reid_benchmark.runner import Track


def _xyxy_to_z(b):
    """xyxy -> Kalman measurement [cx, cy, s(area), r(aspect w/h)]."""
    w = max(1e-3, float(b[2] - b[0])); h = max(1e-3, float(b[3] - b[1]))
    return np.array([b[0] + w / 2.0, b[1] + h / 2.0, w * h, w / h], np.float64)


def _z_to_xyxy(z):
    """[cx,cy,s,r] -> xyxy."""
    s = max(1e-6, float(z[2])); r = max(1e-6, float(z[3]))
    w = np.sqrt(s * r); h = s / max(1e-6, w)
    return np.array([z[0] - w / 2.0, z[1] - h / 2.0, z[0] + w / 2.0, z[1] + h / 2.0], np.float32)


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


class _KalmanBox:
    """Constant-velocity Kalman for [cx,cy,s,r] with velocities [vx,vy,vs] (SORT model)."""

    def __init__(self, z):
        self.x = np.zeros(7, np.float64)
        self.x[:4] = z
        # covariances mirror the canonical SORT setup (high uncertainty on velocities)
        self.P = np.eye(7) * 10.0
        self.P[4:, 4:] *= 1000.0
        self.F = np.eye(7)
        for i in range(3):
            self.F[i, i + 4] = 1.0
        self.H = np.zeros((4, 7)); self.H[:4, :4] = np.eye(4)
        self.Q = np.eye(7); self.Q[4:, 4:] *= 0.01; self.Q[2, 2] *= 0.01
        self.R = np.eye(4); self.R[2:, 2:] *= 10.0

    def predict(self):
        if self.x[2] + self.x[6] <= 0:      # keep area positive
            self.x[6] = 0.0
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q
        return self.x[:4].copy()

    def update(self, z):
        y = z - self.H @ self.x
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(7) - K @ self.H) @ self.P


class SortTracker:
    """SORT/OC-SORT-style motion tracker. Drop-in for IoUTracker."""

    def __init__(self, max_age_frames: int = 40, iou_threshold: float = 0.2,
                 min_hits: int = 1, coast_vel_decay: float = 0.5):
        self.max_age_frames = int(max_age_frames)
        self.iou_threshold = float(iou_threshold)
        self.min_hits = int(min_hits)
        # velocity decay applied each frame a track COASTS (no detection). A near-stationary
        # person often has a small jitter velocity; without decay the coasted prediction
        # drifts during an occlusion, the re-detection then misses the IoU gate, and a NEW
        # track+id is minted (= churn). Decaying velocity keeps the predicted box put.
        self.coast_vel_decay = float(coast_vel_decay)
        self.next_id = 1
        self.tracks: dict[int, Track] = {}
        self._kf: dict[int, _KalmanBox] = {}
        self._last_obs: dict[int, np.ndarray] = {}     # local_id -> last measured xyxy

    def update(self, detections, frame_idx: int):
        dets = [np.asarray(d, np.float32) for d in (detections or [])]
        ids = list(self.tracks.keys())

        # 1) predict every track's box for this frame
        pred = {}
        for tid in ids:
            pred[tid] = _z_to_xyxy(self._kf[tid].predict())

        # 2) Hungarian association on predicted-vs-detection IoU
        matches, un_det = {}, set(range(len(dets)))
        if ids and dets:
            cost = np.ones((len(ids), len(dets)), np.float64)
            iou_m = np.zeros((len(ids), len(dets)), np.float64)
            for i, tid in enumerate(ids):
                for j, d in enumerate(dets):
                    v = _iou(pred[tid], d)
                    iou_m[i, j] = v
                    cost[i, j] = 1.0 - v
            if linear_sum_assignment is not None:
                rows, cols = linear_sum_assignment(cost)
            else:                                       # greedy fallback if scipy missing
                rows, cols = [], []
                for i in range(len(ids)):
                    j = int(np.argmax(iou_m[i]))
                    rows.append(i); cols.append(j)
            for r, c in zip(rows, cols):
                if iou_m[r, c] >= self.iou_threshold:
                    matches[ids[r]] = c
                    un_det.discard(c)

        # 3) update matched tracks with their detection (OC-SORT re-seed after a coast)
        for tid, j in matches.items():
            d = dets[j]
            tr = self.tracks[tid]
            gap = frame_idx - tr.last_seen_frame
            if gap > 1 and tid in self._last_obs:
                # observation-centric: re-anchor velocity from last real obs -> new obs
                prev = _xyxy_to_z(self._last_obs[tid]); cur = _xyxy_to_z(d)
                self._kf[tid].x[4:7] = (cur[:3] - prev[:3]) / float(gap)
            self._kf[tid].update(_xyxy_to_z(d))
            tr.bbox = _z_to_xyxy(self._kf[tid].x[:4])
            tr.last_seen_frame = frame_idx
            tr.hits += 1
            self._last_obs[tid] = d.copy()

        # 3.5) coasting tracks (no detection this frame): decay velocity toward 0 so a
        # stationary person's predicted box stays put through an occlusion, so the
        # re-detection matches the SAME track instead of spawning a new id (churn fix).
        for tid in ids:
            if tid not in matches and tid in self._kf:
                self._kf[tid].x[4:7] *= self.coast_vel_decay
                self.tracks[tid].bbox = _z_to_xyxy(self._kf[tid].x[:4])

        # 4) spawn new tracks for unmatched detections
        for j in un_det:
            tid = self.next_id; self.next_id += 1
            self._kf[tid] = _KalmanBox(_xyxy_to_z(dets[j]))
            self.tracks[tid] = Track(tid, dets[j].astype(np.float32), frame_idx)
            self._last_obs[tid] = dets[j].copy()

        # 5) age out coasting tracks past the buffer
        for tid in list(self.tracks):
            if frame_idx - self.tracks[tid].last_seen_frame > self.max_age_frames:
                del self.tracks[tid]; self._kf.pop(tid, None); self._last_obs.pop(tid, None)

        # 6) return only tracks matched to a detection THIS frame + past the min_hits warmup
        return [t for t in self.tracks.values()
                if t.last_seen_frame == frame_idx and t.hits >= self.min_hits]
