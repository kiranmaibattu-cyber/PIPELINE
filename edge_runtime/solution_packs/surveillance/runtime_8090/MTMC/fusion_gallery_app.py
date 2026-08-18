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
import time
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
    app_qualities: list = field(default_factory=list)
    face_qualities: list = field(default_factory=list)
    gait_qualities: list = field(default_factory=list)
    app_cameras: list = field(default_factory=list)  # camera per app exemplar (view_diverse)
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
        wall_clock_aging: bool = True,
        k: int = 5,
        policy: str = "ring",
        face_threshold: float = 0.8045,
        gait_threshold: float = 0.3496,
        learned_weights_path: Path | None = None,
        topology: bool = False,
        topo_min_transition_s: float = 5.0,
        topo_max_transition_s: float = 180.0,
        topo_enforce_min: bool = True,
        learned_transitions: dict | None = None,
        same_camera_match_threshold: float = 1.0,
        same_camera_match_thresholds: dict | None = None,
        same_camera_long_gap_s: float = 0.0,
        same_camera_long_gap_threshold: float | None = None,
        cross_camera_match_threshold: float = 1.0,
        cross_camera_no_modal_threshold: float | None = None,
        cross_camera_min_single_camera_seen: int = 1,
        cross_camera_min_single_camera_seen_by_camera: dict | None = None,
        min_store_quality: float = 0.0,
        cross_camera_exemplar_min_quality: float = 0.0,
        cross_camera_query_min_quality: float = 0.0,
        cross_camera_face_veto: bool = False,
        cross_camera_face_veto_threshold: float = 1.0,
        cross_camera_require_face: bool = False,
        off_chain_cameras: tuple = (),
        link_mode: str = "min",
        link_topk: int = 2,
        use_faiss: bool = True,
        faiss_min_gallery: int = 512,
        faiss_topk: int = 256,
        faiss_rebuild_every: int = 200,
        faiss_hot: int = 64,
    ) -> None:
        # FAISS candidate search. Scoring/gating below is UNCHANGED: the index only
        # decides WHICH gids the scoring loop visits. Below faiss_min_gallery the
        # linear scan is cheaper than maintaining the index, so it stays exact.
        self.link_mode = str(link_mode)
        self.link_topk = max(1, int(link_topk))
        self.use_faiss = bool(use_faiss)
        self.faiss_min_gallery = int(faiss_min_gallery)
        self.faiss_topk = int(faiss_topk)
        self.faiss_rebuild_every = max(1, int(faiss_rebuild_every))
        self.faiss_hot = max(0, int(faiss_hot))
        self._faiss_index = None
        self._faiss_row_gid = None
        self._faiss_since = 0        # matches since the last rebuild
        self._hot: list[int] = []    # recently touched gids: always scored, so an
        #                              identity created since the last rebuild is
        #                              never invisible to the next query
        self.faiss_searches = 0
        self.faiss_rebuilds = 0
        self.cross_camera_face_veto = bool(cross_camera_face_veto)
        self.cross_camera_face_veto_threshold = float(cross_camera_face_veto_threshold)
        # require a face-confirmed frame before allowing a CROSS-camera merge onto
        # an identity that already has faces (on frontal cameras). Without this the
        # merge forms on a faceless frame and the face veto never gets to run.
        self.cross_camera_require_face = bool(cross_camera_require_face)
        self.threshold = app_threshold
        self.face_threshold = face_threshold
        self.gait_threshold = gait_threshold
        self.strategy = strategy
        self.frontal_cameras = set(frontal_cameras)
        self.max_age_seconds = max_age_seconds
        self.wall_clock_aging = bool(wall_clock_aging)
        self.k = max(1, k)
        self.policy = policy
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
        # when False, drop the per-pair MINIMUM travel time (keeps concurrent-
        # exclusion for distant pairs + the max window); recovers fast-walker
        # cross-camera matches that a too-high learned minimum was rejecting.
        self.topo_enforce_min = bool(topo_enforce_min)
        self.same_camera_match_threshold = float(same_camera_match_threshold)
        self.same_camera_match_thresholds = {
            str(k): float(v) for k, v in (same_camera_match_thresholds or {}).items()
        }
        self.same_camera_long_gap_s = float(same_camera_long_gap_s)
        self.same_camera_long_gap_threshold = (
            float(same_camera_long_gap_threshold)
            if same_camera_long_gap_threshold is not None
            else float(same_camera_match_threshold)
        )
        self.cross_camera_match_threshold = float(cross_camera_match_threshold)
        self.cross_camera_no_modal_threshold = (
            float(cross_camera_no_modal_threshold)
            if cross_camera_no_modal_threshold is not None
            else float(cross_camera_match_threshold)
        )
        self.cross_camera_min_single_camera_seen = max(1, int(cross_camera_min_single_camera_seen))
        self.cross_camera_min_single_camera_seen_by_camera = {
            str(k): max(1, int(v))
            for k, v in (cross_camera_min_single_camera_seen_by_camera or {}).items()
        }
        self.min_store_quality = max(0.0, float(min_store_quality))
        self.cross_camera_exemplar_min_quality = max(
            self.min_store_quality,
            float(cross_camera_exemplar_min_quality),
        )
        self.cross_camera_query_min_quality = max(0.0, float(cross_camera_query_min_quality))
        self.embedding_store_rejections = 0
        self.cross_camera_quality_rejections = 0
        self.topo_rejections = 0
        self.learned_windows: dict = {}
        # Off-chain cameras: cameras NOT in the physical camera chain. They must never
        # cross-link (a person in an off-chain camera and any other camera cannot be the
        # same identity across cameras), regardless of any spurious learned transition
        # window. Missing physical logic that let ch16<->ch9 merge.
        self.off_chain_cams: set = set(off_chain_cameras or ())
        self.overlapping_pairs: set = set()
        # Adjacent (but non-overlapping) pairs, e.g. lift ch2 next to waiting-area
        # ch9/ch10: a person can be seen in both near-simultaneously, so they are
        # exempt from concurrent-exclusion like overlapping pairs. DISTANT pairs
        # keep concurrent-exclusion (nobody is in ch1 and ch16 at the same instant).
        self.adjacent_pairs: set = set()
        if learned_transitions:
            for pair_key, info in learned_transitions.items():
                if not info.get("learned"):
                    continue
                cams = pair_key.split("|") if "|" in pair_key else pair_key.split("_")
                if len(cams) == 2:
                    pair = frozenset(cams)
                    self.learned_windows[pair] = (float(info["min_s"]), float(info["max_s"]))
                    if info.get("overlapping") or info.get("adjacency") == "overlapping":
                        self.overlapping_pairs.add(pair)
                    elif info.get("adjacency") == "adjacent" or float(info["min_s"]) <= 1e-6:
                        self.adjacent_pairs.add(pair)

    def _touch_hot(self, gid: int) -> None:
        if self.faiss_hot <= 0:
            return
        self._hot.append(int(gid))
        if len(self._hot) > self.faiss_hot:
            del self._hot[0]

    def _rebuild_faiss(self) -> None:
        """Flat inner-product index over every app exemplar; row -> gid map."""
        try:
            import faiss
        except Exception:
            self.use_faiss = False
            self._faiss_index = None
            return
        rows, rid = [], []
        for gid, e in self.gallery.items():
            for v in e.app_embs:
                rows.append(v)
                rid.append(gid)
        if not rows:
            self._faiss_index, self._faiss_row_gid = None, None
            return
        M = np.stack(rows).astype(np.float32)     # already L2-normalized -> IP == cosine
        index = faiss.IndexFlatIP(M.shape[1])
        index.add(M)
        self._faiss_index = index
        self._faiss_row_gid = np.asarray(rid, dtype=np.int64)
        self._faiss_since = 0
        self.faiss_rebuilds += 1

    def _candidate_gids(self, app_emb: np.ndarray) -> set[int] | None:
        """gids worth scoring for this query, or None meaning 'score everything'."""
        if not self.use_faiss or app_emb is None:
            return None
        if len(self.gallery) < self.faiss_min_gallery:
            return None
        self._faiss_since += 1
        if self._faiss_index is None or self._faiss_since >= self.faiss_rebuild_every:
            self._rebuild_faiss()
        if self._faiss_index is None or self._faiss_row_gid is None:
            return None
        k = int(min(self.faiss_topk, self._faiss_index.ntotal))
        if k <= 0:
            return None
        q = np.ascontiguousarray(app_emb[None].astype(np.float32))
        _, idx = self._faiss_index.search(q, k)
        self.faiss_searches += 1
        cand = {int(self._faiss_row_gid[i]) for i in idx[0] if i >= 0}
        cand.update(self._hot)                    # cover ids added since the rebuild
        cand.intersection_update(self.gallery)    # drop expired ids
        return cand or None

    def _age_stamp(self, t: float) -> float:
        """Recency stamp for expiry.

        WALL clock, not video time. Expiry asks "is this identity stale?",
        which is a question about now. camera_last_seen keeps VIDEO time
        because topology asks "how long between these two cameras?", which
        is a question about the recording.

        Mixing them is what made overload destructive: under load the
        streams drift apart on the video clock (measured: 212s at 15
        streams, 728s at 25, 1152s at 30), so a stream running ahead
        called _expire with a large t and deleted every identity held by
        the streams running behind -- while those people were still on
        screen. They came back as new gids.
        """
        return time.time() if self.wall_clock_aging else t

    def _expire(self, t: float) -> None:
        if not self.max_age_seconds:
            return
        now = self._age_stamp(t)
        for gid in [g for g, e in self.gallery.items() if now - e.last_seen_time > self.max_age_seconds]:
            del self.gallery[gid]

    def _store_embedding(
        self,
        embs: list,
        qualities: list,
        emb: np.ndarray,
        quality: float,
        policy: str | None = None,
        cameras: list | None = None,
        camera: str | None = None,
    ) -> None:
        """Store one modality vector using the configured bounded policy."""
        if quality < self.min_store_quality:
            self.embedding_store_rejections += 1
            return
        policy = policy or self.policy
        if policy == "camera_balanced_quality" and cameras is not None and camera is not None:
            q = float(quality)
            if len(cameras) < len(embs):
                cameras.extend([None] * (len(embs) - len(cameras)))
            if len(embs) < self.k:
                embs.append(emb.copy())
                qualities.append(q)
                cameras.append(camera)
                return

            if camera not in cameras:
                counts = {c: cameras.count(c) for c in set(cameras)}
                evictable = [i for i, cam in enumerate(cameras) if counts.get(cam, 0) > 1]
                pool = evictable or list(range(len(embs)))
                worst = min(pool, key=lambda i: qualities[i])
                embs[worst] = emb.copy()
                qualities[worst] = q
                cameras[worst] = camera
                return

            same_camera = [i for i, cam in enumerate(cameras) if cam == camera]
            worst = min(same_camera, key=lambda i: qualities[i])
            if q > qualities[worst]:
                embs[worst] = emb.copy()
                qualities[worst] = q
                cameras[worst] = camera
            return

        if policy in ("quality_topk", "camera_balanced_quality"):
            if len(embs) < self.k:
                embs.append(emb.copy())
                qualities.append(float(quality))
                return
            if not qualities:
                qualities.extend([0.0] * len(embs))
            worst = int(np.argmin(qualities))
            if quality > qualities[worst]:
                embs[worst] = emb.copy()
                qualities[worst] = float(quality)
            return

        if policy == "view_diverse":
            # Keep K VIEW-DIVERSE exemplars (front/rear/side/seated/...) instead
            # of K near-duplicate best-quality crops. Matching is nearest-exemplar
            # (_min_dist), so spreading the anchors out lets a cross-camera query
            # match the nearest VIEW rather than an average. When per-exemplar
            # cameras are tracked, a NEW camera's view is always admitted and a
            # camera's SOLE exemplar is never evicted -> cross-camera views are
            # protected (the fix for fragile cross-camera links).
            q = float(quality)
            has_cams = cameras is not None            # keep list aligned with embs
            protect = has_cams and camera is not None  # camera-aware eviction active
            if len(embs) < self.k:
                embs.append(emb.copy())
                qualities.append(q)
                if has_cams:
                    cameras.append(camera)
                return
            E = np.stack(embs)                      # (k, d), L2-normalized
            new_min = float((1.0 - E @ emb).min())  # newcomer novelty vs the set
            D = 1.0 - E @ E.T                        # pairwise cosine distance
            np.fill_diagonal(D, np.inf)
            nn = D.min(axis=1)                       # each anchor's redundancy
            order = list(np.argsort(nn))             # most-redundant first
            if protect:
                counts = {c: cameras.count(c) for c in set(cameras)}
                # evictable = not the sole representative of its camera
                over = [int(i) for i in order if counts[cameras[i]] > 1]
                if camera not in cameras:
                    # newcomer adds a NEW camera view -> always admit it, evicting
                    # a redundant over-represented member (protect sole reps)
                    r = over[0] if over else int(order[0])
                    embs[r] = emb.copy(); qualities[r] = q; cameras[r] = camera
                    return
                r = over[0] if over else int(order[0])
            else:
                r = int(np.argmin(nn))
            # admit only if the newcomer is MORE spread-out than the chosen anchor
            if new_min > nn[r] or (new_min >= nn[r] - 1e-6 and q > qualities[r]):
                embs[r] = emb.copy()
                qualities[r] = q
                if has_cams:
                    cameras[r] = camera
            return

        embs.append(emb.copy())
        qualities.append(float(quality))
        if len(embs) > self.k:
            embs.pop(0)
            qualities.pop(0)

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
        # off-chain cameras never cross-link (physically impossible chain hop) --
        # independent of any learned window (which may be spurious for ch16).
        if self.off_chain_cams and (camera in self.off_chain_cams
                                    or last_other_cam in self.off_chain_cams):
            return False
        gap = t - last_other_t
        pair = frozenset((last_other_cam, camera))
        # Camera clips may start at different wall-clock times while being read
        # in frame-index order. Evidence from a future camera timestamp can
        # never be a physically valid transition, including adjacent pairs.
        if gap < -1e-6:
            return False
        # concurrent-exclusion only for DISTANT pairs (not overlapping/adjacent,
        # which can genuinely see the same person at the same instant)
        if gap <= 1e-6 and pair not in self.overlapping_pairs and pair not in self.adjacent_pairs:
            return False
        lo, hi = self.learned_windows.get(pair, (self.topo_min, self.topo_max))
        if not self.topo_enforce_min:
            lo = 0.0
        return lo <= gap <= hi

    def _is_cross_camera_candidate(self, entry: "FusionEntry", camera: str) -> bool:
        """True when accepting this candidate would attach current evidence to
        an identity whose freshest support is from another camera."""
        if not camera or not entry.camera_last_seen:
            return False
        other = [(tt, c) for c, tt in entry.camera_last_seen.items() if c != camera]
        if not other:
            return False
        last_other_t, _ = max(other)
        return entry.camera_last_seen.get(camera, -1e18) < last_other_t

    def _same_camera_gap(self, entry: "FusionEntry", camera: str, t: float) -> float:
        if not camera or not entry.camera_last_seen or camera not in entry.camera_last_seen:
            return 0.0
        return max(0.0, t - float(entry.camera_last_seen[camera]))

    def _cross_candidate_mature(self, entry: "FusionEntry", camera: str) -> bool:
        if not self._is_cross_camera_candidate(entry, camera):
            return True
        if len(entry.camera_set) > 1:
            return True
        source_camera = next(iter(entry.camera_set), "")
        required = self.cross_camera_min_single_camera_seen_by_camera.get(
            source_camera,
            self.cross_camera_min_single_camera_seen,
        )
        return int(entry.seen_count) >= required

    def _decision_threshold(
        self,
        is_cross_camera: bool,
        has_modal_pair: bool = False,
        same_camera_gap: float = 0.0,
        camera: str = "",
    ) -> float:
        if self.strategy == "learned":
            base = 0.5
            return min(base, self.cross_camera_match_threshold) if is_cross_camera else base
        if is_cross_camera:
            return self.cross_camera_match_threshold if has_modal_pair else self.cross_camera_no_modal_threshold
        if self.same_camera_long_gap_s and same_camera_gap >= self.same_camera_long_gap_s:
            return self.same_camera_long_gap_threshold
        if camera and camera in self.same_camera_match_thresholds:
            return self.same_camera_match_thresholds[camera]
        return self.same_camera_match_threshold

    @staticmethod
    def _min_dist(
        query: np.ndarray,
        embs: list,
        qualities: list | None = None,
        min_quality: float = 0.0,
    ) -> float | None:
        if not embs:
            return None
        selected = embs
        if min_quality > 0.0 and qualities is not None and len(qualities) == len(embs):
            selected = [emb for emb, quality in zip(embs, qualities) if quality >= min_quality]
        if not selected:
            return None
        return float((1.0 - np.stack(selected) @ query).min())

    def _link_dist(
        self,
        query: np.ndarray,
        embs: list,
        qualities: list | None = None,
        min_quality: float = 0.0,
    ) -> float | None:
        """Distance from a query to an identity's exemplar SET.

        `min` (the original rule) accepts on the single closest exemplar, so one
        lucky or corrupted anchor decides the match and nothing ever revisits it.
        `avg` is robust to that but punishes view-diverse identities: a back-view
        exemplar legitimately sits far from a front-view query, which is the normal
        case for ch9/ch10 facing each other across the hall. `topk` averages the k
        closest -- robust to one outlier without demanding every view agree.
        """
        if not embs:
            return None
        selected = embs
        if min_quality > 0.0 and qualities is not None and len(qualities) == len(embs):
            selected = [e for e, q in zip(embs, qualities) if q >= min_quality]
        if not selected:
            return None
        d = 1.0 - np.stack(selected) @ query
        if self.link_mode == "min" or len(d) == 1:
            return float(d.min())
        if self.link_mode == "avg":
            return float(d.mean())
        k = max(1, min(int(self.link_topk), len(d)))
        return float(np.partition(d, k - 1)[:k].mean())

    def pair_distance(self, gid_a: int, gid_b: int) -> float | None:
        """Symmetric appearance distance between two IDENTITIES under the same link
        rule. The repair pass uses this to decide whether two ids are one person."""
        a, b = self.gallery.get(int(gid_a)), self.gallery.get(int(gid_b))
        if a is None or b is None or not a.app_embs or not b.app_embs:
            return None
        D = 1.0 - np.stack(a.app_embs) @ np.stack(b.app_embs).T
        if self.link_mode == "min":
            return float(D.min())
        if self.link_mode == "avg":
            return float(D.mean())
        k = max(1, min(int(self.link_topk), D.size))
        return float(np.partition(D.reshape(-1), k - 1)[:k].mean())

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

    def _cross_query_quality_ok(self, entry: FusionEntry, camera: str, quality: float) -> tuple[bool, bool]:
        """Gate weak BODY queries only for cross-camera matching.

        Low-quality crops are still useful for same-camera continuity, so this
        deliberately does not reject same-camera candidates or storage. It only
        stops a weak/tiny/partial crop from creating a cross-camera identity link.
        """
        is_cross = self._is_cross_camera_candidate(entry, camera)
        if is_cross and float(quality) < self.cross_camera_query_min_quality:
            self.cross_camera_quality_rejections += 1
            return False, is_cross
        return True, is_cross

    def _rank_match(
        self, app_emb: np.ndarray, face_emb: np.ndarray | None, has_face_q: bool,
        camera: str = "", t: float = 0.0, exclude_gids: set[int] | None = None,
        include_gids: set[int] | None = None,
        quality: float = 1.0,
    ) -> tuple[int | None, float, int]:
        candidates: list[tuple[int, float, float | None]] = []
        n_topo_blocked = 0
        exclude_gids = exclude_gids or set()
        include_gids = include_gids or set()
        for gid, entry in self.gallery.items():
            if gid in exclude_gids:
                continue
            if include_gids and gid not in include_gids:
                continue
            if not self._topology_ok(entry, camera, t):
                n_topo_blocked += 1
                continue
            if not self._cross_candidate_mature(entry, camera):
                continue
            quality_ok, is_cross_candidate = self._cross_query_quality_ok(entry, camera, quality)
            if not quality_ok:
                continue
            exemplar_floor = (
                self.cross_camera_exemplar_min_quality
                if is_cross_candidate
                else 0.0
            )
            d_app = self._min_dist(
                app_emb, entry.app_embs, entry.app_qualities, exemplar_floor,
            )
            if d_app is None:
                continue
            d_face = None
            if has_face_q and entry.face_embs:
                d_face = self._min_dist(face_emb, entry.face_embs)
            candidates.append((gid, d_app, d_face))
        if not candidates:
            return None, 999.0, n_topo_blocked

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
        return best_gid, d_app / self.threshold, n_topo_blocked

    def score(
        self,
        app_emb: np.ndarray,
        face_emb: np.ndarray | None,
        camera: str,
        t: float,
        gait_emb: np.ndarray | None = None,
        exclude_gids: set[int] | None = None,
        include_gids: set[int] | None = None,
        quality: float = 1.0,
        decision_threshold_override: float | None = None,
    ) -> tuple[int, float]:
        """Return the currently acceptable existing gid without mutating it.

        Locked tracklets use this to test challengers. Mutation happens only
        after hysteresis accepts the candidate, preventing rejected one-frame
        matches from polluting another identity's gallery entry.
        """
        self._expire(t)
        has_face_q = face_emb is not None and float(np.linalg.norm(face_emb)) > 1e-6
        has_gait_q = gait_emb is not None and float(np.linalg.norm(gait_emb)) > 1e-6

        best_gid, best_dist = None, 999.0
        best_face_gid, best_face_dist = None, 999.0
        best_modal_pair = False

        n_topo_blocked = 0
        exclude_gids = exclude_gids or set()
        include_gids = include_gids or set()
        if self.strategy == "rank":
            best_gid, best_dist, n_topo_blocked = self._rank_match(
                app_emb, face_emb, has_face_q, camera, t, exclude_gids, include_gids, quality
            )
        else:
            for gid, entry in self.gallery.items():
                if gid in exclude_gids:
                    continue
                if include_gids and gid not in include_gids:
                    continue
                if not self._topology_ok(entry, camera, t):
                    n_topo_blocked += 1
                    continue
                if not self._cross_candidate_mature(entry, camera):
                    continue
                quality_ok, is_cross_candidate = self._cross_query_quality_ok(entry, camera, quality)
                if not quality_ok:
                    continue
                exemplar_floor = (
                    self.cross_camera_exemplar_min_quality
                    if is_cross_candidate
                    else 0.0
                )
                d_app = self._min_dist(
                    app_emb, entry.app_embs, entry.app_qualities, exemplar_floor,
                )
                if d_app is None:
                    continue
                d_app_n = d_app / self.threshold
                # cross-camera merges must be face-CONFIRMED on frontal cameras:
                # otherwise the merge forms on a faceless frame and the face veto
                # below never runs (this is why body+colour alone merges two
                # different white-coat doctors).
                if (self.cross_camera_require_face
                        and camera in self.frontal_cameras
                        and entry.face_embs
                        and not has_face_q
                        and is_cross_candidate):
                    continue
                w_face = self._face_weight(camera, has_face_q, bool(entry.face_embs))
                weights = [1.0 - w_face]
                dists = [d_app_n]
                has_face_pair = False
                has_gait_pair = False
                if w_face > 0:
                    d_face_n = self._min_dist(face_emb, entry.face_embs) / self.face_threshold
                    # cross-camera FACE VETO: two different people in the same
                    # uniform (e.g. two white-coat doctors) are identical by body
                    # + colour. If both sides have a face and the faces clearly
                    # disagree, it is a different person -> reject the merge.
                    if (self.cross_camera_face_veto
                            and d_face_n > self.cross_camera_face_veto_threshold
                            and is_cross_candidate):
                        continue
                    weights.append(w_face)
                    dists.append(d_face_n)
                    has_face_pair = True
                if self.strategy == "camera_aware_gait" and has_gait_q and entry.gait_embs:
                    has_gait_pair = True
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
                    best_modal_pair = has_face_pair or has_gait_pair
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
            best_modal_pair = True

        self.topo_rejections += n_topo_blocked
        is_cross = best_gid is not None and self._is_cross_camera_candidate(self.gallery[best_gid], camera)
        same_gap = self._same_camera_gap(self.gallery[best_gid], camera, t) if best_gid is not None else 0.0
        decision_threshold = (
            float(decision_threshold_override)
            if decision_threshold_override is not None
            else self._decision_threshold(is_cross, best_modal_pair, same_gap, camera)
        )
        if best_gid is None or best_dist > decision_threshold:
            return 0, best_dist
        return best_gid, best_dist

    def match(
        self,
        app_emb: np.ndarray,
        face_emb: np.ndarray | None,
        camera: str,
        t: float,
        gait_emb: np.ndarray | None = None,
        quality: float = 1.0,
        face_quality: float = 1.0,
        gait_quality: float = 1.0,
        exclude_gids: set[int] | None = None,
    ) -> tuple[int, float]:
        self._expire(t)
        has_face_q = face_emb is not None and float(np.linalg.norm(face_emb)) > 1e-6
        has_gait_q = gait_emb is not None and float(np.linalg.norm(gait_emb)) > 1e-6

        best_gid, best_dist = None, 999.0
        best_app_n = best_face_n = best_gait_n = None
        best_face_gid, best_face_dist = None, 999.0
        best_modal_pair = False

        n_topo_blocked = 0
        exclude_gids = exclude_gids or set()
        if self.strategy == "rank":
            best_gid, best_dist, n_topo_blocked = self._rank_match(
                app_emb, face_emb, has_face_q, camera, t, exclude_gids, quality=quality
            )
        else:
            # FAISS narrows WHICH gids are scored; every gate/weight below is
            # untouched. cand is None -> exact linear scan (small gallery).
            cand = self._candidate_gids(app_emb)
            items = (self.gallery.items() if cand is None
                     else [(g, self.gallery[g]) for g in cand])
            for gid, entry in items:
                if gid in exclude_gids:
                    continue
                if not self._topology_ok(entry, camera, t):
                    n_topo_blocked += 1
                    continue
                if not self._cross_candidate_mature(entry, camera):
                    continue
                quality_ok, is_cross_candidate = self._cross_query_quality_ok(entry, camera, quality)
                if not quality_ok:
                    continue
                exemplar_floor = (
                    self.cross_camera_exemplar_min_quality
                    if is_cross_candidate
                    else 0.0
                )
                d_app = self._link_dist(
                    app_emb, entry.app_embs, entry.app_qualities, exemplar_floor,
                )
                if d_app is None:
                    continue
                d_app_n = d_app / self.threshold
                # cross-camera merges must be face-CONFIRMED on frontal cameras:
                # otherwise the merge forms on a faceless frame and the face veto
                # below never runs (this is why body+colour alone merges two
                # different white-coat doctors).
                if (self.cross_camera_require_face
                        and camera in self.frontal_cameras
                        and entry.face_embs
                        and not has_face_q
                        and is_cross_candidate):
                    continue
                w_face = self._face_weight(camera, has_face_q, bool(entry.face_embs))
                weights = [1.0 - w_face]
                dists = [d_app_n]
                has_face_pair = False
                has_gait_pair = False
                if w_face > 0:
                    d_face_n = self._min_dist(face_emb, entry.face_embs) / self.face_threshold
                    # cross-camera FACE VETO: two different people in the same
                    # uniform (e.g. two white-coat doctors) are identical by body
                    # + colour. If both sides have a face and the faces clearly
                    # disagree, it is a different person -> reject the merge.
                    if (self.cross_camera_face_veto
                            and d_face_n > self.cross_camera_face_veto_threshold
                            and is_cross_candidate):
                        continue
                    weights.append(w_face)
                    dists.append(d_face_n)
                    has_face_pair = True
                if self.strategy == "camera_aware_gait" and has_gait_q and entry.gait_embs:
                    has_gait_pair = True
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
                    best_modal_pair = has_face_pair or has_gait_pair
                    best_app_n = d_app_n
                    best_face_n = d_face_n if has_face_pair else None
                    best_gait_n = d_gait_n if has_gait_pair else None
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
            best_modal_pair = True

        self.topo_rejections += n_topo_blocked
        is_cross = best_gid is not None and self._is_cross_camera_candidate(self.gallery[best_gid], camera)
        same_gap = self._same_camera_gap(self.gallery[best_gid], camera, t) if best_gid is not None else 0.0
        decision_threshold = self._decision_threshold(is_cross, best_modal_pair, same_gap, camera)
        is_new = best_gid is None or best_dist > decision_threshold
        face_q = float(face_quality)
        gait_q = float(gait_quality)
        if is_new:
            gid = self.next_global_id
            self.next_global_id += 1
            self.gallery[gid] = FusionEntry(
                global_id=gid,
                app_embs=[app_emb.copy()],
                app_cameras=[camera],
                face_embs=[face_emb.copy()] if has_face_q else [],
                gait_embs=[gait_emb.copy()] if has_gait_q else [],
                app_qualities=[float(quality)],
                face_qualities=[face_q] if has_face_q else [],
                gait_qualities=[gait_q] if has_gait_q else [],
                last_seen_time=self._age_stamp(t),
                camera_set={camera},
                created_time=t,
                camera_last_seen={camera: t},
            )
            self._touch_hot(gid)
        else:
            gid = best_gid
            self._touch_hot(gid)
            e = self.gallery[gid]
            self._store_embedding(e.app_embs, e.app_qualities, app_emb, quality,
                                  cameras=e.app_cameras, camera=camera)
            if has_face_q:
                self._store_embedding(e.face_embs, e.face_qualities, face_emb, face_q,
                                      policy="quality_topk")
            if has_gait_q:
                self._store_embedding(e.gait_embs, e.gait_qualities, gait_emb, gait_q,
                                      policy="ring")
            e.last_seen_time = self._age_stamp(t)
            e.seen_count += 1
            e.camera_set.add(camera)
            e.camera_last_seen[camera] = t

        self.telemetry.append(
            {
                "t": round(t, 2),
                "camera": camera,
                "gallery_size": len(self.gallery),
                "matched_gid": gid,
                "distance": round(best_dist, 4),
                "new_id": int(is_new),
                "face_used": int(has_face_q),
                "gait_used": int(has_gait_q),
                "topo_blocked": n_topo_blocked,
                "cross_camera_candidate": int(is_cross),
                "modal_pair_used": int(best_modal_pair),
                "same_camera_gap": round(same_gap, 2),
                "decision_threshold": round(decision_threshold, 4),
                'best_gid': best_gid,
                'best_cameras': (sorted(self.gallery[best_gid].camera_last_seen) if best_gid is not None else []),
                'query_camera': camera,
                'd_app_n': (round(best_app_n, 4) if best_app_n is not None else None),
                'd_face_n': (round(best_face_n, 4) if best_face_n is not None else None),
                'd_gait_n': (round(best_gait_n, 4) if best_gait_n is not None else None),
                'reason': ('matched' if not is_new else ('below_threshold' if best_gid is not None else 'no_surviving_candidate')),
            }
        )
        import os as _os, json as _json
        _tp = _os.environ.get('REID_TRACE_LOG')
        if _tp and (is_cross or (is_new and best_gid is not None)):
            try:
                with open(_tp, 'a') as _tf:
                    _tf.write(_json.dumps(self.telemetry[-1]) + '\n')
            except Exception:
                pass
        return gid, best_dist

    def mint_local(self, camera: str, t: float) -> int:
        """Create an identity carrying NO appearance evidence.

        For a track whose crop is good enough to BE someone continuously, but not good
        enough to be evidence of WHO. The entry is deliberately born empty, which makes
        it inert in the matcher: `_link_dist` returns None for an empty exemplar set and
        the match loop skips it, so this id can never be matched into, can never form a
        cross-camera link, and can never pull another person in. It only exists so the
        track has a stable id to display and to accumulate history under.

        It is promoted the normal way: when a seed-grade crop finally arrives, the usual
        `reinforce` populates it and it becomes an ordinary identity, with no special
        case anywhere. That is the whole point -- the person keeps one id from the moment
        they appear instead of being minted a fresh one each time they walk close enough.
        """
        gid = self.next_global_id
        self.next_global_id += 1
        self.gallery[gid] = FusionEntry(
            global_id=gid,
            app_embs=[], app_cameras=[],
            face_embs=[], gait_embs=[],
            app_qualities=[], face_qualities=[], gait_qualities=[],
            last_seen_time=self._age_stamp(t),
            camera_set={camera},
            created_time=t,
            camera_last_seen={camera: t},
        )
        self._touch_hot(gid)
        return gid

    def is_local(self, gid: int) -> bool:
        """True while an identity still carries no appearance evidence, i.e. it was
        minted by `mint_local` and has not yet been promoted by a seed-grade crop."""
        e = self.gallery.get(int(gid))
        return e is not None and not e.app_embs

    def touch(self, gid: int, camera: str, t: float) -> bool:
        """Keep an identity alive WITHOUT storing embeddings.

        Tracklet hysteresis needs to hold a gid steady across frames where the
        matcher disagrees. Doing that with force_assign() writes whoever is in the
        box right now into the held identity, so an unstable id corrupts the very
        exemplars used to match it next frame. This refreshes recency only.
        """
        e = self.gallery.get(gid)
        if e is None:
            return False
        e.last_seen_time = self._age_stamp(t)
        e.camera_set.add(camera)
        e.camera_last_seen[camera] = t
        self._touch_hot(gid)
        return True

    def force_assign(
        self,
        gid: int,
        app_emb: np.ndarray,
        face_emb: np.ndarray | None,
        camera: str,
        t: float,
        gait_emb: np.ndarray | None = None,
        quality: float = 1.0,
        face_quality: float = 1.0,
        gait_quality: float = 1.0,
    ) -> None:
        e = self.gallery.get(gid)
        self._touch_hot(gid)
        has_face = face_emb is not None and float(np.linalg.norm(face_emb)) > 1e-6
        has_gait = gait_emb is not None and float(np.linalg.norm(gait_emb)) > 1e-6
        face_q = float(face_quality)
        gait_q = float(gait_quality)
        if e is None:
            self.gallery[gid] = FusionEntry(
                global_id=gid,
                app_embs=[app_emb.copy()],
                app_cameras=[camera],
                face_embs=[face_emb.copy()] if has_face else [],
                gait_embs=[gait_emb.copy()] if has_gait else [],
                app_qualities=[float(quality)],
                face_qualities=[face_q] if has_face else [],
                gait_qualities=[gait_q] if has_gait else [],
                last_seen_time=self._age_stamp(t),
                camera_set={camera},
                created_time=t,
                camera_last_seen={camera: t},
            )
            self.next_global_id = max(self.next_global_id, gid + 1)
            return
        self._store_embedding(e.app_embs, e.app_qualities, app_emb, quality,
                              cameras=e.app_cameras, camera=camera)
        if has_face:
            self._store_embedding(e.face_embs, e.face_qualities, face_emb, face_q,
                                  policy="quality_topk")
        if has_gait:
            self._store_embedding(e.gait_embs, e.gait_qualities, gait_emb, gait_q,
                                  policy="ring")
        e.last_seen_time = self._age_stamp(t)
        e.seen_count += 1
        e.camera_set.add(camera)
        e.camera_last_seen[camera] = t

    def merge_gid(self, source_gid: int, target_gid: int) -> bool:
        """Merge a short-lived provisional identity into an established one."""
        source_gid = int(source_gid)
        target_gid = int(target_gid)
        if source_gid == target_gid:
            return True
        source = self.gallery.get(source_gid)
        target = self.gallery.get(target_gid)
        if source is None or target is None:
            return False

        src_cams = source.app_cameras if len(source.app_cameras) == len(source.app_embs) else [None] * len(source.app_embs)
        for emb, quality, cam in zip(source.app_embs, source.app_qualities, src_cams):
            self._store_embedding(target.app_embs, target.app_qualities, emb, quality,
                                  cameras=target.app_cameras, camera=cam)
        for emb, quality in zip(source.face_embs, source.face_qualities):
            self._store_embedding(target.face_embs, target.face_qualities, emb, quality)
        for emb, quality in zip(source.gait_embs, source.gait_qualities):
            self._store_embedding(
                target.gait_embs,
                target.gait_qualities,
                emb,
                quality,
                policy="ring",
            )

        target.seen_count += source.seen_count
        target.last_seen_time = max(target.last_seen_time, source.last_seen_time)
        if source.created_time:
            target.created_time = min(target.created_time, source.created_time)
        target.camera_set.update(source.camera_set)
        for camera, seen_at in source.camera_last_seen.items():
            target.camera_last_seen[camera] = max(
                target.camera_last_seen.get(camera, float("-inf")),
                seen_at,
            )
        del self.gallery[source_gid]
        return True

    def stats(self) -> dict:
        return {
            "strategy": self.strategy,
            "policy": self.policy,
            "k": self.k,
            "entries": len(self.gallery),
            "total_ids_created": self.next_global_id - 1,
            "entries_with_face": sum(1 for e in self.gallery.values() if e.face_embs),
            "entries_with_gait": sum(1 for e in self.gallery.values() if e.gait_embs),
            "cross_camera_ids": sum(1 for e in self.gallery.values() if len(e.camera_set) > 1),
            "same_camera_match_threshold": self.same_camera_match_threshold,
            "same_camera_match_thresholds": self.same_camera_match_thresholds,
            "same_camera_long_gap_s": self.same_camera_long_gap_s,
            "same_camera_long_gap_threshold": self.same_camera_long_gap_threshold,
            "cross_camera_match_threshold": self.cross_camera_match_threshold,
            "cross_camera_no_modal_threshold": self.cross_camera_no_modal_threshold,
            "cross_camera_min_single_camera_seen": self.cross_camera_min_single_camera_seen,
            "cross_camera_min_single_camera_seen_by_camera": self.cross_camera_min_single_camera_seen_by_camera,
            "min_store_quality": self.min_store_quality,
            "cross_camera_exemplar_min_quality": self.cross_camera_exemplar_min_quality,
            "cross_camera_query_min_quality": self.cross_camera_query_min_quality,
            "embedding_store_rejections": self.embedding_store_rejections,
            "cross_camera_quality_rejections": self.cross_camera_quality_rejections,
            "topology_enabled": self.topology,
            "topology_rejections": self.topo_rejections,
        }
