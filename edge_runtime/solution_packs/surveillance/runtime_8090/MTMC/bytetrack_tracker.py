"""Native ByteTrack-style tracker.

This keeps the dependency surface small while fixing the main weakness of plain
SORT in CCTV scenes: a person may briefly drop below the detector's high
confidence threshold during occlusion, sitting, or motion blur. ByteTrack uses
high-confidence detections to spawn tracks, then uses lower-confidence detections
only to keep existing tracks alive.

Interface is intentionally close to SortTracker:
    update(detections, frame_idx, scores=None) -> list[Track]

Only tracks matched to a detection this frame are returned; predicted-only tracks
are withheld so downstream Re-ID never embeds synthetic boxes.
"""
from __future__ import annotations

import numpy as np

try:
    from scipy.optimize import linear_sum_assignment
except Exception:  # pragma: no cover
    linear_sum_assignment = None

from reid_benchmark.runner import Track
from MTMC.sort_tracker import _KalmanBox, _xyxy_to_z, _z_to_xyxy, _iou


class ByteTrackTracker:
    def __init__(
        self,
        max_age_frames: int = 40,
        iou_threshold: float = 0.2,
        high_thresh: float = 0.45,
        low_thresh: float = 0.10,
        new_thresh: float = 0.55,
        min_hits: int = 1,
        coast_vel_decay: float = 0.5,
    ) -> None:
        self.max_age_frames = int(max_age_frames)
        self.iou_threshold = float(iou_threshold)
        self.high_thresh = float(high_thresh)
        self.low_thresh = float(low_thresh)
        self.new_thresh = float(new_thresh)
        self.min_hits = int(min_hits)
        self.coast_vel_decay = float(coast_vel_decay)
        self.next_id = 1
        self.tracks: dict[int, Track] = {}
        self._kf: dict[int, _KalmanBox] = {}
        self._last_obs: dict[int, np.ndarray] = {}

    def _assign(self, tids: list[int], dets: list[np.ndarray], pred: dict[int, np.ndarray]):
        matches: dict[int, int] = {}
        unmatched = set(range(len(dets)))
        if not tids or not dets:
            return matches, unmatched
        cost = np.ones((len(tids), len(dets)), np.float64)
        iou_m = np.zeros((len(tids), len(dets)), np.float64)
        for i, tid in enumerate(tids):
            for j, d in enumerate(dets):
                v = _iou(pred[tid], d)
                iou_m[i, j] = v
                cost[i, j] = 1.0 - v
        if linear_sum_assignment is not None:
            rows, cols = linear_sum_assignment(cost)
        else:
            rows, cols = [], []
            used = set()
            for i in range(len(tids)):
                j = int(np.argmax(iou_m[i]))
                if j not in used:
                    rows.append(i)
                    cols.append(j)
                    used.add(j)
        for r, c in zip(rows, cols):
            if iou_m[r, c] >= self.iou_threshold:
                matches[tids[r]] = c
                unmatched.discard(c)
        return matches, unmatched

    def _update_track(self, tid: int, det: np.ndarray, frame_idx: int) -> None:
        tr = self.tracks[tid]
        gap = frame_idx - tr.last_seen_frame
        if gap > 1 and tid in self._last_obs:
            prev = _xyxy_to_z(self._last_obs[tid])
            cur = _xyxy_to_z(det)
            self._kf[tid].x[4:7] = (cur[:3] - prev[:3]) / float(gap)
        self._kf[tid].update(_xyxy_to_z(det))
        tr.bbox = _z_to_xyxy(self._kf[tid].x[:4])
        tr.last_seen_frame = frame_idx
        tr.hits += 1
        self._last_obs[tid] = det.copy()

    def update(self, detections, frame_idx: int, scores=None):
        dets_all = [np.asarray(d, np.float32) for d in (detections or [])]
        if scores is None or len(scores) != len(dets_all):
            scores_all = [1.0 for _ in dets_all]
        else:
            scores_all = [float(s) for s in scores]

        high_idx = [i for i, s in enumerate(scores_all) if s >= self.high_thresh]
        low_idx = [i for i, s in enumerate(scores_all)
                   if self.low_thresh <= s < self.high_thresh]
        high = [dets_all[i] for i in high_idx]
        low = [dets_all[i] for i in low_idx]

        tids = list(self.tracks.keys())
        pred = {tid: _z_to_xyxy(self._kf[tid].predict()) for tid in tids}

        matched: set[int] = set()
        returned: set[int] = set()

        # Stage 1: normal high-confidence association.
        m_high, un_high = self._assign(tids, high, pred)
        for tid, j in m_high.items():
            self._update_track(tid, high[j], frame_idx)
            matched.add(tid)
            returned.add(tid)

        # Stage 2: unmatched tracks may be continued by low-confidence detections.
        remain_tids = [tid for tid in tids if tid not in matched]
        pred2 = {tid: _z_to_xyxy(self._kf[tid].x[:4]) for tid in remain_tids}
        m_low, _un_low = self._assign(remain_tids, low, pred2)
        for tid, j in m_low.items():
            self._update_track(tid, low[j], frame_idx)
            matched.add(tid)
            returned.add(tid)

        # Unmatched tracks coast but are not returned for crop embedding.
        for tid in tids:
            if tid not in matched and tid in self._kf:
                self._kf[tid].x[4:7] *= self.coast_vel_decay
                self.tracks[tid].bbox = _z_to_xyxy(self._kf[tid].x[:4])

        # Only high-confidence unmatched detections can start new tracks.
        for j in un_high:
            original_idx = high_idx[j]
            if scores_all[original_idx] < self.new_thresh:
                continue
            tid = self.next_id
            self.next_id += 1
            self._kf[tid] = _KalmanBox(_xyxy_to_z(high[j]))
            self.tracks[tid] = Track(tid, high[j].astype(np.float32), frame_idx)
            self._last_obs[tid] = high[j].copy()
            returned.add(tid)

        for tid in list(self.tracks):
            if frame_idx - self.tracks[tid].last_seen_frame > self.max_age_frames:
                del self.tracks[tid]
                self._kf.pop(tid, None)
                self._last_obs.pop(tid, None)

        return [t for t in self.tracks.values()
                if t.local_id in returned
                and t.last_seen_frame == frame_idx
                and t.hits >= self.min_hits]
