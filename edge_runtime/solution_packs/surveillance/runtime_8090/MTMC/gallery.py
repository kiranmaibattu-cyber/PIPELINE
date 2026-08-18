"""Multi-embedding gallery with pluggable retention policies and telemetry.

Answers the production gallery questions empirically:
  - embeddings per ID:   policy = ema (1) | ring (K, FIFO) | quality_topk (K, best kept)
  - matching over K:     min | mean | top3 (mean of 3 smallest distances)
  - retention:           max_age_seconds (0/None = unlimited)
  - growth control:      max_entries cap with lru | quality eviction
Every mutation is recorded to a telemetry list for offline aggregation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


def l2_normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(1.0 - np.dot(a, b))


@dataclass
class GalleryEntry:
    global_id: int
    embeddings: list          # list[np.ndarray], newest last
    qualities: list           # parallel list[float] (crop quality score)
    last_seen_time: float
    created_time: float
    seen_count: int = 1
    camera_set: set = field(default_factory=set)
    camera_last_seen: dict = field(default_factory=dict)  # camera -> last timestamp

    def matrix(self) -> np.ndarray:
        return np.stack(self.embeddings)


class MultiEmbeddingGallery:
    """Gallery storing 1..K embeddings per global ID.

    policy:  "ema"          — single embedding, exponential moving average (legacy behavior)
             "ring"         — K most recent embeddings (FIFO)
             "quality_topk" — K highest-quality embeddings ever seen
    match:   "min" | "mean" | "top3" distance across an entry's embeddings
    """

    def __init__(
        self,
        threshold: float,
        max_age_seconds: float = 180.0,
        policy: str = "ema",
        k: int = 5,
        match: str = "min",
        max_entries: int = 0,
        eviction: str = "lru",
        ema_alpha: float = 0.85,
        topology: bool = False,
        topo_min_transition_s: float = 5.0,
        topo_max_transition_s: float = 180.0,
        overlapping_cameras: bool = False,
        learned_transitions: dict | None = None,
    ) -> None:
        self.threshold = threshold
        self.max_age_seconds = max_age_seconds
        self.policy = policy
        self.k = max(1, k)
        self.match = match
        self.max_entries = max_entries
        self.eviction = eviction
        self.ema_alpha = ema_alpha
        # topology / transition-time gate (for non-overlapping camera networks):
        # a cross-camera match is only allowed if the time since the identity was
        # last seen in ANOTHER camera is a physically plausible travel time. For
        # overlapping cameras set overlapping_cameras=True (min transition = 0).
        self.topology = topology
        self.topo_min = 0.0 if overlapping_cameras else topo_min_transition_s
        self.topo_max = topo_max_transition_s
        # per-pair learned windows, keyed by frozenset({camA, camB}) -> (min, max);
        # falls back to the global default for pairs with no learned window.
        self.learned_windows: dict = {}
        if learned_transitions:
            for pair_key, info in learned_transitions.items():
                if not info.get("learned"):
                    continue
                cams = pair_key.split("|") if "|" in pair_key else pair_key.split("_")
                if len(cams) == 2:
                    self.learned_windows[frozenset(cams)] = (
                        float(info["min_s"]), float(info["max_s"]))
        self.next_global_id = 1
        self.gallery: dict[int, GalleryEntry] = {}
        self.telemetry: list[dict] = []
        self.topo_rejections = 0

    # ------------------------------------------------------------------ utils

    def _entry_distance(self, embedding: np.ndarray, entry: GalleryEntry) -> float:
        dists = 1.0 - entry.matrix() @ embedding
        if self.match == "min" or len(dists) == 1:
            return float(dists.min())
        if self.match == "mean":
            return float(dists.mean())
        if self.match == "top3":
            return float(np.sort(dists)[: min(3, len(dists))].mean())
        return float(dists.min())

    def _topology_ok(self, entry: GalleryEntry, camera: str, timestamp: float) -> bool:
        """True if matching this entry from `camera` at `timestamp` is a
        physically plausible move given where the identity has been.

        Rule: if the entry was most recently seen in a DIFFERENT camera, the gap
        since then must lie in [topo_min, topo_max] — you need time to walk
        between non-overlapping cameras, and can't be in two of them at once.
        Same-camera continuity is always allowed.
        """
        if not self.topology or not camera or not entry.camera_last_seen:
            return True
        # most recent activity in a camera other than the query camera
        other = [(t, c) for c, t in entry.camera_last_seen.items() if c != camera]
        if not other:
            return True  # identity has only been in this camera
        last_other_t, last_other_cam = max(other)
        last_same_t = entry.camera_last_seen.get(camera, -1e18)
        if last_same_t >= last_other_t:
            return True  # most recent evidence is in this same camera → continuity
        gap = timestamp - last_other_t
        # per-pair learned window if available, else global default
        lo, hi = self.learned_windows.get(
            frozenset((last_other_cam, camera)), (self.topo_min, self.topo_max))
        return lo <= gap <= hi

    def _expire(self, timestamp: float) -> int:
        if not self.max_age_seconds:
            return 0
        expired = [
            gid for gid, e in self.gallery.items()
            if timestamp - e.last_seen_time > self.max_age_seconds
        ]
        for gid in expired:
            del self.gallery[gid]
        return len(expired)

    def _evict_if_full(self) -> int:
        if not self.max_entries or len(self.gallery) < self.max_entries:
            return 0
        n_evict = len(self.gallery) - self.max_entries + 1
        if self.eviction == "quality":
            ranked = sorted(
                self.gallery.values(),
                key=lambda e: (max(e.qualities) if e.qualities else 0.0, e.seen_count),
            )
        else:  # lru
            ranked = sorted(self.gallery.values(), key=lambda e: e.last_seen_time)
        for entry in ranked[:n_evict]:
            del self.gallery[entry.global_id]
        return n_evict

    def _update_entry(self, entry: GalleryEntry, emb: np.ndarray, quality: float) -> None:
        if self.policy == "ema":
            merged = self.ema_alpha * entry.embeddings[0] + (1 - self.ema_alpha) * emb
            entry.embeddings[0] = l2_normalize(merged)
            entry.qualities[0] = max(entry.qualities[0], quality)
            return
        if self.policy == "ring":
            entry.embeddings.append(emb.copy())
            entry.qualities.append(quality)
            if len(entry.embeddings) > self.k:
                entry.embeddings.pop(0)
                entry.qualities.pop(0)
            return
        if self.policy == "quality_topk":
            if len(entry.embeddings) < self.k:
                entry.embeddings.append(emb.copy())
                entry.qualities.append(quality)
            else:
                worst = int(np.argmin(entry.qualities))
                if quality > entry.qualities[worst]:
                    entry.embeddings[worst] = emb.copy()
                    entry.qualities[worst] = quality
            return
        raise ValueError(f"Unknown gallery policy: {self.policy}")

    # ------------------------------------------------------------------ api

    def match_embedding(
        self,
        embedding: np.ndarray,
        timestamp: float,
        quality: float = 1.0,
        camera: str = "",
    ) -> tuple[int, float, bool]:
        """Match one embedding. Returns (global_id, best_distance, is_new)."""
        n_expired = self._expire(timestamp)

        best_gid: Optional[int] = None
        best_dist = 999.0
        n_topo_blocked = 0
        for gid, entry in self.gallery.items():
            if not self._topology_ok(entry, camera, timestamp):
                n_topo_blocked += 1
                continue
            d = self._entry_distance(embedding, entry)
            if d < best_dist:
                best_dist = d
                best_gid = gid
        self.topo_rejections += n_topo_blocked

        is_new = best_gid is None or best_dist > self.threshold
        if is_new:
            n_evicted = self._evict_if_full()
            gid = self.next_global_id
            self.next_global_id += 1
            self.gallery[gid] = GalleryEntry(
                global_id=gid,
                embeddings=[embedding.copy()],
                qualities=[quality],
                last_seen_time=timestamp,
                created_time=timestamp,
                camera_set={camera} if camera else set(),
                camera_last_seen={camera: timestamp} if camera else {},
            )
        else:
            n_evicted = 0
            gid = best_gid  # type: ignore[assignment]
            entry = self.gallery[gid]
            self._update_entry(entry, embedding, quality)
            entry.last_seen_time = timestamp
            entry.seen_count += 1
            if camera:
                entry.camera_set.add(camera)
                entry.camera_last_seen[camera] = timestamp

        self.telemetry.append({
            "t": round(timestamp, 2),
            "gallery_size": len(self.gallery),
            "embeddings_total": sum(len(e.embeddings) for e in self.gallery.values()),
            "matched_gid": gid,
            "distance": round(best_dist, 4),
            "new_id": int(is_new),
            "expired": n_expired,
            "evicted": n_evicted,
        })
        return gid, best_dist, is_new

    def force_assign(self, gid: int, embedding: np.ndarray, timestamp: float,
                     quality: float = 1.0, camera: str = "") -> None:
        """Externally-confirmed identity (e.g. BEV geometric match) — enrich entry."""
        entry = self.gallery.get(gid)
        if entry is None:
            self.gallery[gid] = GalleryEntry(
                global_id=gid,
                embeddings=[embedding.copy()],
                qualities=[quality],
                last_seen_time=timestamp,
                created_time=timestamp,
                camera_set={camera} if camera else set(),
            )
            self.next_global_id = max(self.next_global_id, gid + 1)
            return
        self._update_entry(entry, embedding, quality)
        entry.last_seen_time = timestamp
        entry.seen_count += 1
        if camera:
            entry.camera_set.add(camera)

    def stats(self) -> dict:
        sizes = [len(e.embeddings) for e in self.gallery.values()]
        return {
            "policy": self.policy,
            "k": self.k,
            "match": self.match,
            "entries": len(self.gallery),
            "total_ids_created": self.next_global_id - 1,
            "embeddings_stored": int(sum(sizes)),
            "avg_embeddings_per_id": round(float(np.mean(sizes)), 2) if sizes else 0.0,
            "cross_camera_ids": sum(1 for e in self.gallery.values() if len(e.camera_set) > 1),
            "topology_enabled": self.topology,
            "topology_rejections": self.topo_rejections,
        }


def crop_quality(crop: np.ndarray) -> float:
    """Cheap quality score in [0,1]: area (resolution) x sharpness proxy."""
    import cv2
    if crop is None or crop.size == 0:
        return 0.0
    h, w = crop.shape[:2]
    area_score = min(1.0, (h * w) / 30000.0)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
    sharp_score = min(1.0, sharp / 500.0)
    return 0.6 * area_score + 0.4 * sharp_score
