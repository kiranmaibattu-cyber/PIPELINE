"""Multi-modal fusion gallery: appearance plus optional face/gait signals.

Strategies:
  camera_aware       face weight is high only on frontal cameras.
  quality            face weight is fixed when both sides have a face.
  geff               body match plus confident face override.
  learned            logistic-regression probability from app/face distances.
  rank               reciprocal-rank fusion over body and face rankings.
  camera_aware_gait  camera-aware face fusion plus optional gait distance.

Distances are normalized by their calibrated per-modality thresholds before
weighted fusion. Missing modalities are dropped and the remaining weights are
renormalized.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


@dataclass
class FusionEntry:
    global_id: int
    app_embs: list
    face_embs: list
    last_seen_time: float
    gait_embs: list = field(default_factory=list)
    seen_count: int = 1
    camera_set: set = field(default_factory=set)
    created_time: float = 0.0
    camera_last_seen: dict = field(default_factory=dict)  # camera -> last timestamp


class MultiModalGallery:
    FACE_WEIGHT_FRONTAL = 0.55
    FACE_WEIGHT_DEFAULT = 0.0
    FACE_WEIGHT_QUALITY = 0.45
    GAIT_WEIGHT = 0.20
    GEFF_MERGE_FRAC = 0.75
    RRF_K = 60

    def __init__(
        self,
        app_threshold: float,
        strategy: str = "camera_aware",
        frontal_cameras: tuple[str, ...] = ("ch10",),
        max_age_seconds: float = 180.0,
        k: int = 5,
        face_threshold: float = 0.8045,
        gait_threshold: float = 0.3496,
        learned_weights_path: Path | None = None,
        topology: bool = False,
        topo_min_transition_s: float = 5.0,
        topo_max_transition_s: float = 180.0,
        learned_transitions: dict | None = None,
    ) -> None:
        self.threshold = app_threshold
        self.face_threshold = face_threshold
        self.gait_threshold = gait_threshold
        self.strategy = strategy
        self.frontal_cameras = set(frontal_cameras)
        self.max_age_seconds = max_age_seconds
        self.k = k
        self.next_global_id = 1
        self.gallery: dict[int, FusionEntry] = {}
        self.telemetry: list[dict] = []
        self.learned: dict | None = None
        if self.strategy == "learned":
            path = learned_weights_path or Path(__file__).resolve().parent / "reports" / "learned_fusion_weights.json"
            if path.exists():
                self.learned = json.loads(path.read_text(encoding="utf-8"))
        # topology / transition-time gate (per-pair windows; frozenset key)
        self.topology = topology
        self.topo_min = topo_min_transition_s
        self.topo_max = topo_max_transition_s
        self.topo_rejections = 0
        self.learned_windows: dict = {}
        if learned_transitions:
            for pair_key, info in learned_transitions.items():
                if not info.get("learned"):
                    continue
                cams = pair_key.split("|") if "|" in pair_key else pair_key.split("_")
                if len(cams) == 2:
                    self.learned_windows[frozenset(cams)] = (float(info["min_s"]), float(info["max_s"]))

    def _expire(self, t: float) -> None:
        if not self.max_age_seconds:
            return
        for gid in [g for g, e in self.gallery.items() if t - e.last_seen_time > self.max_age_seconds]:
            del self.gallery[gid]

    def _topology_ok(self, entry: "FusionEntry", camera: str, t: float) -> bool:
        """Physically plausible move? Cross-camera match allowed only if the gap
        since the identity was last in ANOTHER camera is in the per-pair window;
        same-camera continuity always allowed."""
        if not self.topology or not camera or not entry.camera_last_seen:
            return True
        other = [(tt, c) for c, tt in entry.camera_last_seen.items() if c != camera]
        if not other:
            return True
        last_other_t, last_other_cam = max(other)
        if entry.camera_last_seen.get(camera, -1e18) >= last_other_t:
            return True  # most recent evidence is this same camera
        gap = t - last_other_t
        lo, hi = self.learned_windows.get(frozenset((last_other_cam, camera)),
                                          (self.topo_min, self.topo_max))
        return lo <= gap <= hi

    @staticmethod
    def _min_dist(query: np.ndarray, embs: list) -> float | None:
        if not embs:
            return None
        return float((1.0 - np.stack(embs) @ query).min())

    def _face_weight(self, camera: str, has_face_query: bool, has_face_entry: bool) -> float:
        if not (has_face_query and has_face_entry):
            return 0.0
        if self.strategy in ("camera_aware", "camera_aware_gait"):
            return self.FACE_WEIGHT_FRONTAL if camera in self.frontal_cameras else self.FACE_WEIGHT_DEFAULT
        if self.strategy == "quality":
            return self.FACE_WEIGHT_QUALITY
        return 0.0

    def _learned_distance(self, d_app_n: float, d_face_n: float, has_face: bool) -> float:
        if not self.learned:
            return d_app_n
        coef = np.array(self.learned["coef"], dtype=np.float32)
        x = np.array([d_app_n, d_face_n if has_face else 0.0, float(has_face)], dtype=np.float32)
        z = float(self.learned["intercept"] + coef @ x)
        prob_same = 1.0 / (1.0 + np.exp(-z))
        return 1.0 - prob_same

    def _rank_match(
        self, app_emb: np.ndarray, face_emb: np.ndarray | None, has_face_q: bool,
        camera: str = "", t: float = 0.0
    ) -> tuple[int | None, float]:
        candidates: list[tuple[int, float, float | None]] = []
        for gid, entry in self.gallery.items():
            if not self._topology_ok(entry, camera, t):
                continue
            d_app = self._min_dist(app_emb, entry.app_embs)
            if d_app is None:
                continue
            d_face = None
            if has_face_q and entry.face_embs:
                d_face = self._min_dist(face_emb, entry.face_embs)
            candidates.append((gid, d_app, d_face))
        if not candidates:
            return None, 999.0

        body_rank = {gid: r for r, (gid, _, _) in enumerate(sorted(candidates, key=lambda x: x[1]), start=1)}
        face_rank = {
            gid: r
            for r, (gid, _, _) in enumerate(
                sorted((c for c in candidates if c[2] is not None), key=lambda x: x[2]), start=1
            )
        }
        best_gid, best_score = None, -1.0
        for gid, _, _ in candidates:
            score = 1.0 / (self.RRF_K + body_rank[gid])
            if gid in face_rank:
                score += 1.0 / (self.RRF_K + face_rank[gid])
            if score > best_score:
                best_gid, best_score = gid, score
        # RRF chooses the candidate ordering. New-vs-match still uses calibrated
        # body distance, so a noisy face rank cannot force a merge by itself.
        d_app = next(d for gid, d, _ in candidates if gid == best_gid)
        return best_gid, d_app / self.threshold

    def match(
        self,
        app_emb: np.ndarray,
        face_emb: np.ndarray | None,
        camera: str,
        t: float,
        gait_emb: np.ndarray | None = None,
    ) -> tuple[int, float]:
        self._expire(t)
        has_face_q = face_emb is not None and float(np.linalg.norm(face_emb)) > 1e-6
        has_gait_q = gait_emb is not None and float(np.linalg.norm(gait_emb)) > 1e-6

        best_gid, best_dist = None, 999.0
        best_face_gid, best_face_dist = None, 999.0

        n_topo_blocked = 0
        if self.strategy == "rank":
            best_gid, best_dist = self._rank_match(app_emb, face_emb, has_face_q, camera, t)
        else:
            for gid, entry in self.gallery.items():
                if not self._topology_ok(entry, camera, t):
                    n_topo_blocked += 1
                    continue
                d_app = self._min_dist(app_emb, entry.app_embs)
                if d_app is None:
                    continue
                d_app_n = d_app / self.threshold
                w_face = self._face_weight(camera, has_face_q, bool(entry.face_embs))
                weights = [1.0 - w_face]
                dists = [d_app_n]
                has_face_pair = False
                if w_face > 0:
                    d_face_n = self._min_dist(face_emb, entry.face_embs) / self.face_threshold
                    weights.append(w_face)
                    dists.append(d_face_n)
                    has_face_pair = True
                if self.strategy == "camera_aware_gait" and has_gait_q and entry.gait_embs:
                    d_gait_n = self._min_dist(gait_emb, entry.gait_embs) / self.gait_threshold
                    weights[0] = max(0.0, weights[0] - self.GAIT_WEIGHT)
                    weights.append(self.GAIT_WEIGHT)
                    dists.append(d_gait_n)

                if self.strategy == "learned":
                    dist = self._learned_distance(d_app_n, dists[1] if has_face_pair else 0.0, has_face_pair)
                else:
                    total = sum(weights)
                    dist = sum(w * d for w, d in zip(weights, dists)) / total if total else d_app_n
                if dist < best_dist:
                    best_dist, best_gid = dist, gid
                if self.strategy == "geff" and has_face_q and entry.face_embs:
                    d_face = self._min_dist(face_emb, entry.face_embs)
                    if d_face < best_face_dist:
                        best_face_dist, best_face_gid = d_face, gid

        if (
            self.strategy == "geff"
            and best_face_gid is not None
            and best_face_dist <= self.GEFF_MERGE_FRAC * self.face_threshold
        ):
            best_gid, best_dist = best_face_gid, best_face_dist / self.face_threshold

        self.topo_rejections += n_topo_blocked
        decision_threshold = 0.5 if self.strategy == "learned" else 1.0
        is_new = best_gid is None or best_dist > decision_threshold
        if is_new:
            gid = self.next_global_id
            self.next_global_id += 1
            self.gallery[gid] = FusionEntry(
                global_id=gid,
                app_embs=[app_emb.copy()],
                face_embs=[face_emb.copy()] if has_face_q else [],
                gait_embs=[gait_emb.copy()] if has_gait_q else [],
                last_seen_time=t,
                camera_set={camera},
                created_time=t,
                camera_last_seen={camera: t},
            )
        else:
            gid = best_gid
            e = self.gallery[gid]
            e.app_embs.append(app_emb.copy())
            if len(e.app_embs) > self.k:
                e.app_embs.pop(0)
            if has_face_q:
                e.face_embs.append(face_emb.copy())
                if len(e.face_embs) > self.k:
                    e.face_embs.pop(0)
            if has_gait_q:
                e.gait_embs.append(gait_emb.copy())
                if len(e.gait_embs) > self.k:
                    e.gait_embs.pop(0)
            e.last_seen_time = t
            e.seen_count += 1
            e.camera_set.add(camera)
            e.camera_last_seen[camera] = t

        self.telemetry.append(
            {
                "t": round(t, 2),
                "gallery_size": len(self.gallery),
                "matched_gid": gid,
                "distance": round(best_dist, 4),
                "new_id": int(is_new),
                "face_used": int(has_face_q),
                "gait_used": int(has_gait_q),
                "topo_blocked": n_topo_blocked,
            }
        )
        return gid, best_dist

    def force_assign(
        self,
        gid: int,
        app_emb: np.ndarray,
        face_emb: np.ndarray | None,
        camera: str,
        t: float,
        gait_emb: np.ndarray | None = None,
    ) -> None:
        e = self.gallery.get(gid)
        has_face = face_emb is not None and float(np.linalg.norm(face_emb)) > 1e-6
        has_gait = gait_emb is not None and float(np.linalg.norm(gait_emb)) > 1e-6
        if e is None:
            self.gallery[gid] = FusionEntry(
                global_id=gid,
                app_embs=[app_emb.copy()],
                face_embs=[face_emb.copy()] if has_face else [],
                gait_embs=[gait_emb.copy()] if has_gait else [],
                last_seen_time=t,
                camera_set={camera},
            )
            self.next_global_id = max(self.next_global_id, gid + 1)
            return
        e.app_embs.append(app_emb.copy())
        if len(e.app_embs) > self.k:
            e.app_embs.pop(0)
        if has_face:
            e.face_embs.append(face_emb.copy())
            if len(e.face_embs) > self.k:
                e.face_embs.pop(0)
        if has_gait:
            e.gait_embs.append(gait_emb.copy())
            if len(e.gait_embs) > self.k:
                e.gait_embs.pop(0)
        e.last_seen_time = t
        e.seen_count += 1
        e.camera_set.add(camera)

    def stats(self) -> dict:
        return {
            "strategy": self.strategy,
            "entries": len(self.gallery),
            "total_ids_created": self.next_global_id - 1,
            "entries_with_face": sum(1 for e in self.gallery.values() if e.face_embs),
            "entries_with_gait": sum(1 for e in self.gallery.values() if e.gait_embs),
            "cross_camera_ids": sum(1 for e in self.gallery.values() if len(e.camera_set) > 1),
            "topology_enabled": self.topology,
            "topology_rejections": self.topo_rejections,
        }
