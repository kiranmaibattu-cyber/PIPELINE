"""Gallery adapters for ReIDPlugin.

`FakeGallery` -- a tiny cosine-NN gallery used to test the plugin's identity LOGIC
(sticky-id, same-frame guard, cross-cam match) without the heavy real gallery.

`MMGalleryAdapter` -- wraps the real MTMC MultiModalGallery (fusion/veto/topology/
colour-gate) behind the same protocol; mirrors the proven match/reinforce the live
two-tier `RealReID` uses. Instantiated for live wiring (Phase B step 2).
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def _cos_dist(a, b) -> float:
    a = np.asarray(a, np.float32); b = np.asarray(b, np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 999.0
    return 1.0 - float(np.dot(a, b) / (na * nb))


class FakeGallery:
    """Cosine nearest-neighbour over appearance only. Mints on no match. Test double."""

    def __init__(self, thr: float = 0.3):
        self.thr = float(thr)
        self._embs: dict[int, list] = {}     # gid -> [app_emb]
        self._next = 1

    def match(self, app, face, camera, t, gait, exclude_gids=None):
        ex = exclude_gids or set()
        best_gid, best_d = None, 999.0
        for gid, embs in self._embs.items():
            if gid in ex:
                continue
            d = min(_cos_dist(app, e) for e in embs)
            if d < best_d:
                best_gid, best_d = gid, d
        if best_gid is not None and best_d <= self.thr:
            return best_gid, best_d
        gid = self._next
        self._next += 1
        self._embs[gid] = [np.asarray(app, np.float32)]
        return gid, 0.0

    def reinforce(self, gid, app, face, camera, t, gait):
        self._embs.setdefault(int(gid), []).append(np.asarray(app, np.float32))


class MMGalleryAdapter:
    """Adapts the real gallery to the ReIDPlugin protocol.

    Wraps MTMC.streamapp_2tier `RealReID` -- the live gallery SERVICE (owns the
    MultiModalGallery, the thread lock, the calibrated thresholds, and the correct
    match/reinforce internals). Its match/reinforce already have exactly this
    protocol's signatures, so the adapter is a thin pass-through and the fusion/veto/
    topology/colour-gate logic stays untouched in MTMC.
    """

    def __init__(self, real_reid):
        self.rr = real_reid

    @classmethod
    def from_config(cls):
        """Build the real gallery from MTMC configs (same construction the live
        two-tier app uses). Lazy import so the substrate has no MTMC dependency."""
        from MTMC.streamapp_2tier import RealReID
        return cls(RealReID())

    def match(self, app, face, camera, t, gait, exclude_gids=None):
        return self.rr.match(app, face, camera, t, gait, exclude_gids=exclude_gids)

    def reinforce(self, gid, app, face, camera, t, gait):
        self.rr.reinforce(gid, app, face, camera, t, gait)
