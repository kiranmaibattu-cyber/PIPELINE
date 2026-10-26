"""Built-in motion-gated inference controller.

The gate is an internal CV runtime optimization, not a Management contract
feature. The worker starts in ACTIVE mode, runs inference normally, and only
stands down after several fully evaluated no-detection frames. In idle MOTION
mode, cheap optical flow watches frames and a periodic sweep rechecks the
models.
"""
from __future__ import annotations

from collections import deque
from typing import Deque

import cv2
import numpy as np

GATE_WIDTH, GATE_HEIGHT = 320, 180


class MotionGateGray:
    """Lucas-Kanade motion vote on a downscaled BGR frame."""

    MAG_TH = 0.2
    MEANMAG_TH = 0.08
    MOVEPCT_TH = 2.0
    CONS_TH = 0.42

    def __init__(self, redet_every: int = 25) -> None:
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        self._lk = {
            "winSize": (15, 15),
            "maxLevel": 2,
            "criteria": (
                cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                10,
                0.03,
            ),
        }
        self._prev_gray = None
        self._prev_pts = None
        self._n = 0
        self._redet_every = int(redet_every)

    def reset(self) -> None:
        self._prev_gray = None
        self._prev_pts = None
        self._n = 0

    def update(self, bgr: np.ndarray) -> bool:
        small = cv2.resize(bgr, (GATE_WIDTH, GATE_HEIGHT),
                           interpolation=cv2.INTER_LINEAR)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = self._clahe.apply(gray)
        candidate = False
        if self._prev_gray is not None and self._prev_pts is not None:
            nxt, st, _ = cv2.calcOpticalFlowPyrLK(
                self._prev_gray, gray, self._prev_pts, None, **self._lk
            )
            if nxt is not None and st is not None:
                valid = st.reshape(-1) == 1
                good_old = self._prev_pts.reshape(-1, 2)[valid]
                good_new = nxt.reshape(-1, 2)[valid]
            else:
                good_old = np.empty((0, 2), np.float32)
                good_new = np.empty((0, 2), np.float32)
            if len(good_new):
                delta = good_new - good_old
                magnitude = np.sqrt((delta ** 2).sum(axis=1))
                moving = magnitude > self.MAG_TH
                mean_mag = float(magnitude.mean())
                move_pct = float(moving.sum() / len(good_new) * 100.0)
                if moving.sum():
                    angle = np.arctan2(delta[moving, 1], delta[moving, 0])
                    hist, _ = np.histogram(angle, bins=8, range=(-np.pi, np.pi))
                    consistency = float(hist.max() / moving.sum())
                else:
                    consistency = 0.0
                candidate = (
                    move_pct > self.MOVEPCT_TH
                    and mean_mag > self.MEANMAG_TH
                    and consistency > self.CONS_TH
                )
            self._prev_pts = (
                good_new.reshape(-1, 1, 2) if len(good_new) else None
            )
        self._n += 1
        if (
            self._prev_pts is None
            or len(self._prev_pts) == 0
            or self._n % self._redet_every == 1
        ):
            points = cv2.goodFeaturesToTrack(
                gray, maxCorners=300, qualityLevel=0.01, minDistance=7,
                blockSize=7,
            )
            if points is not None:
                self._prev_pts = points
        self._prev_gray = gray.copy()
        return candidate


class MotionGateController:
    """ACTIVE -> MOTION -> ACTIVE inference state machine."""

    def __init__(self, confirm_win: int = 14, confirm_need: int = 4,
                 release_misses: int = 6, sweep_seconds: float = 5.0,
                 redet_every: int = 25, start_active: bool = True) -> None:
        if confirm_need <= 0 or confirm_win <= 0 or confirm_need > confirm_win:
            raise ValueError("motion confirmation requires 0 < need <= window")
        self.gate = MotionGateGray(redet_every=redet_every)
        self.hist: Deque[bool] = deque(maxlen=confirm_win)
        self.confirm_need = int(confirm_need)
        self.release_misses = int(release_misses)
        self.sweep_seconds = float(sweep_seconds)
        self.start_active = bool(start_active)
        self.reset()

    def reset(self) -> None:
        self.gate.reset()
        self.hist.clear()
        self.tracking = self.start_active
        self.miss = 0
        self.quiet_frames = 0
        self.switches = 0
        self.sweeps = 0
        self.sweep_hits = 0
        self.motion_frames = 0
        self.skipped_frames = 0
        self.inferred_frames = 0

    def update(self, bgr: np.ndarray, timestamp_s: float,
               fps: float) -> tuple[str, bool, str]:
        """Return (state, run_inference, reason)."""
        if self.tracking:
            self.inferred_frames += 1
            return "TRACKING", True, "active"
        candidate = self.gate.update(bgr)
        if candidate:
            self.motion_frames += 1
        self.hist.append(bool(candidate))
        if sum(self.hist) >= self.confirm_need:
            self.tracking = True
            self.miss = 0
            self.quiet_frames = 0
            self.switches += 1
            self.inferred_frames += 1
            return "TRACKING", True, "motion"
        self.quiet_frames += 1
        sweep_every = max(1, int(round(self.sweep_seconds * fps)))
        if self.quiet_frames >= sweep_every:
            self.quiet_frames = 0
            self.sweeps += 1
            self.inferred_frames += 1
            return "MOTION", True, "sweep"
        self.skipped_frames += 1
        return "MOTION", False, "idle"

    def observe_models(self, vehicle_hit: bool, smoke_hit: bool,
                       fully_evaluated: bool = True) -> bool:
        """Feed model result back. Returns True when ACTIVE releases."""
        detected = bool(vehicle_hit or smoke_hit)
        if not self.tracking:
            if detected:
                self.sweep_hits += 1
                self.tracking = True
                self.miss = 0
                self.quiet_frames = 0
                self.switches += 1
            return False
        if detected:
            self.miss = 0
            return False
        if not fully_evaluated:
            # A skipped periodic model is not a positive observation and not a
            # confirmed negative. Preserve prior confirmed-negative progress.
            return False
        self.miss += 1
        if self.miss >= self.release_misses:
            self.tracking = False
            self.hist.clear()
            self.gate.reset()
            self.miss = 0
            self.quiet_frames = 0
            self.switches += 1
            return True
        return False
