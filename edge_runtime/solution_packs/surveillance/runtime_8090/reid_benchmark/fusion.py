"""Fusion galleries for multi-modal Re-ID.

Three fusion strategies:
  - ScoreFusionGallery: equal-weight average of N model distances (existing)
  - GEFFGallery: quality-gated appearance + face (GEFF paper style)
  - QualityFusionGallery: per-detection quality-weighted distances (FarSight / IDSelect style)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


def l2_normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(1.0 - np.dot(a, b))


# ---------------------------------------------------------------------------
# Score-fusion gallery
# ---------------------------------------------------------------------------

@dataclass
class ScoreFusionEntry:
    global_id: int
    embeddings: dict[str, np.ndarray]  # sub_key -> embedding
    last_seen_time: float
    seen_count: int = 1


class ScoreFusionGallery:
    """Fuses cosine distances from N sub-model galleries at match time."""

    def __init__(
        self,
        sub_keys: tuple[str, ...],
        sub_thresholds: dict[str, float],
        max_age_seconds: float,
        alpha: float = 0.85,
    ) -> None:
        self.sub_keys = sub_keys
        # Average the per-model calibrated thresholds for the fused threshold
        self.threshold = sum(sub_thresholds.get(k, 0.35) for k in sub_keys) / len(sub_keys)
        self.max_age_seconds = max_age_seconds
        self.alpha = alpha
        self.next_global_id = 1
        self.gallery: dict[int, ScoreFusionEntry] = {}

    def _expire(self, timestamp: float) -> None:
        expired = [
            gid
            for gid, e in self.gallery.items()
            if timestamp - e.last_seen_time > self.max_age_seconds
        ]
        for gid in expired:
            del self.gallery[gid]

    def match(
        self, embeddings: dict[str, np.ndarray], timestamp: float
    ) -> tuple[int, float]:
        """Match a dict of {sub_key: embedding} against all gallery entries.

        Returns (global_id, avg_distance).
        """
        self._expire(timestamp)

        best_gid: int | None = None
        best_dist = 999.0

        for gid, entry in self.gallery.items():
            dists: list[float] = []
            for k in self.sub_keys:
                q = embeddings.get(k)
                g = entry.embeddings.get(k)
                if q is not None and g is not None:
                    dists.append(cosine_distance(q, g))
            if not dists:
                continue
            avg = sum(dists) / len(dists)
            if avg < best_dist:
                best_dist = avg
                best_gid = gid

        if best_gid is None or best_dist > self.threshold:
            gid = self.next_global_id
            self.next_global_id += 1
            self.gallery[gid] = ScoreFusionEntry(
                global_id=gid,
                embeddings={k: v.copy() for k, v in embeddings.items()},
                last_seen_time=timestamp,
            )
            return gid, best_dist

        # Update gallery embeddings with EMA
        entry = self.gallery[best_gid]
        for k, emb in embeddings.items():
            if k in entry.embeddings:
                entry.embeddings[k] = l2_normalize(
                    self.alpha * entry.embeddings[k] + (1 - self.alpha) * emb
                )
            else:
                entry.embeddings[k] = emb.copy()
        entry.last_seen_time = timestamp
        entry.seen_count += 1
        return best_gid, best_dist


# ---------------------------------------------------------------------------
# Score-fusion Re-ID model wrapper
# ---------------------------------------------------------------------------

class ScoreFusionReIDModel:
    """Wraps N base Re-ID models; returns per-model embeddings as a dict."""

    def __init__(self, key: str, sub_keys: tuple[str, ...]) -> None:
        self.key = key
        self.sub_keys = sub_keys
        self._sub_models: dict[str, object] = {}
        self.backend = "unloaded"
        self.device = "cpu"

    def load(self) -> tuple[bool, str]:
        from .runner import ReIDModel  # type: ignore[attr-defined]

        for k in self.sub_keys:
            sub = ReIDModel(k)
            ok, info = sub.load()
            if not ok:
                return False, f"Score-fusion sub-model '{k}' failed: {info}"
            self._sub_models[k] = sub

        devices = {m.device for m in self._sub_models.values()}
        self.device = "cuda" if "cuda" in devices else "cpu"
        self.backend = f"score_fusion:{'_+_'.join(self.sub_keys)}:{self.device}"
        return True, self.backend

    def embed_multi(self, crops: list[np.ndarray]) -> dict[str, np.ndarray]:
        return {k: m.embed(crops) for k, m in self._sub_models.items()}


# ---------------------------------------------------------------------------
# GEFF: Gallery-Enriched appearance + face fusion
# ---------------------------------------------------------------------------

@dataclass
class GEFFEntry:
    global_id: int
    appear_emb: np.ndarray
    face_emb: Optional[np.ndarray]  # None until a face is first seen
    face_count: int                 # frames where face was detected
    last_seen_time: float
    seen_count: int = 1


class GEFFGallery:
    """Quality-gated appearance + face gallery (inspired by the GEFF paper).

    Appearance embedding is always used.  Face embedding enriches the match
    when both query and gallery entry have a valid face detection.  The face
    contribution scales with how consistently the gallery entry shows a face
    (face_count / seen_count), so noisy detections contribute less.
    """

    def __init__(
        self,
        appear_threshold: float,
        max_age_seconds: float,
        alpha: float = 0.85,
        face_weight: float = 0.45,
    ) -> None:
        self.threshold = appear_threshold
        self.max_age_seconds = max_age_seconds
        self.alpha = alpha
        self.face_weight = face_weight  # max face contribution when face is reliable
        self.next_global_id = 1
        self.gallery: dict[int, GEFFEntry] = {}

    def _expire(self, timestamp: float) -> None:
        expired = [
            gid for gid, e in self.gallery.items()
            if timestamp - e.last_seen_time > self.max_age_seconds
        ]
        for gid in expired:
            del self.gallery[gid]

    def match(
        self,
        appear_emb: np.ndarray,
        face_emb: Optional[np.ndarray],
        timestamp: float,
    ) -> tuple[int, float]:
        """Match query; face_emb is None when no face was detected in crop."""
        self._expire(timestamp)

        best_gid: int | None = None
        best_dist = 999.0

        for gid, entry in self.gallery.items():
            d_appear = cosine_distance(appear_emb, entry.appear_emb)
            if face_emb is not None and entry.face_emb is not None:
                d_face = cosine_distance(face_emb, entry.face_emb)
                # Weight face by how reliably this entry shows a face
                face_reliability = min(1.0, 2.0 * entry.face_count / max(1, entry.seen_count))
                w = self.face_weight * face_reliability
                d = (1.0 - w) * d_appear + w * d_face
            else:
                d = d_appear
            if d < best_dist:
                best_dist, best_gid = d, gid

        if best_gid is None or best_dist > self.threshold:
            gid = self.next_global_id
            self.next_global_id += 1
            self.gallery[gid] = GEFFEntry(
                global_id=gid,
                appear_emb=appear_emb.copy(),
                face_emb=face_emb.copy() if face_emb is not None else None,
                face_count=1 if face_emb is not None else 0,
                last_seen_time=timestamp,
            )
            return gid, best_dist

        entry = self.gallery[best_gid]
        entry.appear_emb = l2_normalize(
            self.alpha * entry.appear_emb + (1.0 - self.alpha) * appear_emb
        )
        if face_emb is not None:
            if entry.face_emb is not None:
                entry.face_emb = l2_normalize(
                    self.alpha * entry.face_emb + (1.0 - self.alpha) * face_emb
                )
            else:
                entry.face_emb = face_emb.copy()
            entry.face_count += 1
        entry.last_seen_time = timestamp
        entry.seen_count += 1
        return best_gid, best_dist


# ---------------------------------------------------------------------------
# Quality-weighted fusion gallery (FarSight-style & IDSelect-style)
# ---------------------------------------------------------------------------

@dataclass
class QualityFusionEntry:
    global_id: int
    embeddings: dict[str, np.ndarray]
    last_seen_time: float
    seen_count: int = 1


class QualityFusionGallery:
    """Multi-modal gallery with per-detection quality-weighted distance fusion.

    Unlike ScoreFusionGallery (equal weights), each modality's contribution
    is scaled by a quality weight supplied at match time.  A weight of 0
    silently skips that modality for this detection.

    Used by:
      - farsight_style (osnet_ain + face_reid + color_hist_reid; face weight
        drops to 0 when no face detected, crop size scales appearance weight)
      - idselect_style (osnet_ain + strongsort_reid + openvino_reid_retail;
        crop-area-adaptive weights approximate IDSelect's model selection)
    """

    def __init__(
        self,
        sub_keys: tuple[str, ...],
        base_threshold: float,
        max_age_seconds: float,
        alpha: float = 0.85,
    ) -> None:
        self.sub_keys = sub_keys
        self.threshold = base_threshold
        self.max_age_seconds = max_age_seconds
        self.alpha = alpha
        self.next_global_id = 1
        self.gallery: dict[int, QualityFusionEntry] = {}

    def _expire(self, timestamp: float) -> None:
        expired = [
            gid for gid, e in self.gallery.items()
            if timestamp - e.last_seen_time > self.max_age_seconds
        ]
        for gid in expired:
            del self.gallery[gid]

    def match(
        self,
        embeddings: dict[str, np.ndarray],
        quality_weights: dict[str, float],
        timestamp: float,
    ) -> tuple[int, float]:
        """Match with quality-weighted distances.  Weights need not sum to 1."""
        self._expire(timestamp)

        best_gid: int | None = None
        best_dist = 999.0

        for gid, entry in self.gallery.items():
            d_sum = 0.0
            w_sum = 0.0
            for k in self.sub_keys:
                w = quality_weights.get(k, 0.0)
                if w <= 0:
                    continue
                q_emb = embeddings.get(k)
                g_emb = entry.embeddings.get(k)
                if q_emb is None or g_emb is None:
                    continue
                d_sum += w * cosine_distance(q_emb, g_emb)
                w_sum += w
            if w_sum <= 0:
                continue
            d = d_sum / w_sum
            if d < best_dist:
                best_dist, best_gid = d, gid

        if best_gid is None or best_dist > self.threshold:
            gid = self.next_global_id
            self.next_global_id += 1
            self.gallery[gid] = QualityFusionEntry(
                global_id=gid,
                embeddings={k: v.copy() for k, v in embeddings.items()
                            if quality_weights.get(k, 0.0) > 0},
                last_seen_time=timestamp,
            )
            return gid, best_dist

        entry = self.gallery[best_gid]
        for k, emb in embeddings.items():
            if quality_weights.get(k, 0.0) > 0:
                if k in entry.embeddings:
                    entry.embeddings[k] = l2_normalize(
                        self.alpha * entry.embeddings[k] + (1.0 - self.alpha) * emb
                    )
                else:
                    entry.embeddings[k] = emb.copy()
        entry.last_seen_time = timestamp
        entry.seen_count += 1
        return best_gid, best_dist
