"""BEV (bird's-eye / ground-plane) layer -- the spatial answer to cross-camera MTMC.

Appearance + colour hit a hard ceiling on faceless/uniform footage (two people in white
can't be told apart). Position can: a person at map-(x,y) leaving one camera and appearing
at the same (x,y) entering an overlapping/adjacent camera is the same person, regardless of
clothing. This projects each camera's FOOT-POINTS onto a shared 2D site map via a per-camera
homography, then associates identities by world position + time.

Calibration lives in PLATF/config/calibration.yaml: per camera, 4+ image points (px, in the
engine's 640x360 space) and their corresponding shared-map points. It is ESTIMATED first
(rough) and refined against the live map view -- the map shows where it drops each person,
and the correspondences are edited until the dots land right. `cv2.findHomography` builds
the 3x3 image->map matrix; a foot-point maps by `perspectiveTransform`.

Pure-ish: numpy + cv2 only, so the projection/association are testable without the engine.
"""
from __future__ import annotations

import numpy as np

try:
    import cv2
except Exception:            # cv2 optional for import; needed for homography
    cv2 = None


class BEV:
    def __init__(self, calibration: dict):
        # calibration: {map:{w,h}, cameras:{cam:{image:[[x,y]*4], map:[[x,y]*4]}}}
        self.map_w = float((calibration.get("map") or {}).get("w", 1000))
        self.map_h = float((calibration.get("map") or {}).get("h", 600))
        # camera pairs that TRULY share floor (position-coincidence only applies here).
        # Adjacent/chain pairs (ch2<->ch9) touch at a border but a person is not in both at
        # once -> those are hand-offs (time), not overlaps, and must NOT position-link.
        self.overlaps = {frozenset(p) for p in (calibration.get("overlaps") or []) if len(p) == 2}
        self.H: dict = {}                       # camera -> 3x3 image->map homography
        for cam, c in (calibration.get("cameras") or {}).items():
            img = np.asarray(c.get("image", []), np.float32)
            mp = np.asarray(c.get("map", []), np.float32)
            if cv2 is not None and len(img) >= 4 and len(img) == len(mp):
                Hh, _ = cv2.findHomography(img, mp, 0)
                if Hh is not None:
                    self.H[cam] = Hh

    def set_camera(self, camera: str, image, mp) -> bool:
        """Rebuild ONE camera's image->map homography live (from click-to-calibrate)."""
        img = np.asarray(image, np.float32)
        m = np.asarray(mp, np.float32)
        if cv2 is None or len(img) < 4 or len(img) != len(m):
            return False
        Hh, _ = cv2.findHomography(img, m, 0)
        if Hh is None:
            return False
        self.H[camera] = Hh
        return True

    def calibrated(self, camera: str) -> bool:
        return camera in self.H

    def project(self, camera: str, foot_point):
        """Image foot-point (px, 640x360) -> shared-map (x,y), or None if uncalibrated."""
        H = self.H.get(camera)
        if H is None or foot_point is None or cv2 is None:
            return None
        p = np.asarray([[[float(foot_point[0]), float(foot_point[1])]]], np.float32)
        m = cv2.perspectiveTransform(p, H)[0][0]
        return [round(float(m[0]), 1), round(float(m[1]), 1)]

    def associate(self, points, radius: float = 40.0):
        """points: [{gid, camera, map:[x,y], t}] currently on the map. Link gids from
        DIFFERENT cameras whose map positions are within `radius` -> the same physical
        person (cross-camera link by geometry, no appearance). A cross-camera pair is only
        linked when the two cameras are declared OVERLAPPING (share floor); if no overlaps
        are declared, fall back to any different-camera pair. Returns
        [{map:[x,y], gids:[...], cameras:[...]}] clusters."""
        pts = [p for p in points if p.get("map")]
        used = [False] * len(pts)
        clusters = []
        for i, a in enumerate(pts):
            if used[i]:
                continue
            grp = [a]
            used[i] = True
            for j in range(i + 1, len(pts)):
                if used[j]:
                    continue
                b = pts[j]
                if b["camera"] == a["camera"]:
                    continue                    # same camera -> not a cross-cam link here
                if self.overlaps and frozenset((a["camera"], b["camera"])) not in self.overlaps:
                    continue                    # not a declared-overlapping pair -> no position link
                d = ((a["map"][0] - b["map"][0]) ** 2 + (a["map"][1] - b["map"][1]) ** 2) ** 0.5
                if d <= radius:
                    grp.append(b)
                    used[j] = True
            mx = round(sum(g["map"][0] for g in grp) / len(grp), 1)
            my = round(sum(g["map"][1] for g in grp) / len(grp), 1)
            clusters.append({"map": [mx, my],
                             "gids": sorted({g["gid"] for g in grp}),
                             "cameras": sorted({g["camera"] for g in grp})})
        return clusters
