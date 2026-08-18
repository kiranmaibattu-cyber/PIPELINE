"""Re-ID plugin -- the first and hardest plugin, the identity engine of the platform.

It runs FIRST in the host so it binds (camera, local_id) -> global Person before any
analytics plugin sees the observation. It reuses the proven matching engine (the
MultiModalGallery in MTMC/fusion_gallery_app.py) via a small gallery protocol, so the
identity LOGIC (sticky-id, same-frame guard, exemplar memory, identity events) lives
here on the platform while the heavy fusion/veto/topology stays in the gallery.

Identity is event-triggered: it only acts on observations that carry an appearance
embedding (a keyframe / matured tracklet) -- "track first, then analyze."

Gallery protocol (duck-typed, so it is testable without instantiating the real MMG):
    match(app, face, camera, t, gait, exclude_gids) -> (gid|None, dist)
    reinforce(gid, app, face, camera, t, gait) -> None
The gid space is owned by the gallery; PersonStore.get_or_create aligns Person ids to it.
"""
from __future__ import annotations

from typing import Optional

from PLATF.core import Event, Plugin


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1) + max(0.0, bx2 - bx1) * max(0.0, by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


class ReIDPlugin(Plugin):
    name = "reid"

    def __init__(self, gallery=None, sticky: bool = True,
                 reassoc: bool = False, reassoc_gap: int = 30, reassoc_iou: float = 0.4):
        # gallery is OPTIONAL. In the platform's single-engine model the backbone owns
        # identity and the observation CARRIES its authoritative gid, so this plugin just
        # INGESTS it -- no gallery, no second engine. The gallery is a legacy fallback:
        # only used to reconstruct a gid for observations that carry none (old dumps /
        # synthetic test streams). When gid is carried, the gallery is never touched.
        self.gallery = gallery
        self.sticky = bool(sticky)
        # WITHIN-CAMERA RE-ASSOCIATION (the live LOCAL_REASSOC, at observation cadence):
        # a new local_id whose bbox continues a just-departed track in the SAME camera
        # inherits that person instead of gallery-matching/minting.
        # DEFAULT OFF: measured on a real 4-cam run it barely moved the id count (53->52)
        # and RAISED the merge rate (5.7%->7.7%). Cause: the embedded-observation stream
        # is sparse (sync cadence, not every frame), so a re-acquired track's first
        # embedded obs lands many frames later at a shifted bbox -> IoU continuity is
        # unreliable. Reconciliation needs per-frame track continuity, which belongs in
        # the BACKBONE (stamp a break-stable local id on the TrackObservation), not here.
        # Kept as an opt-in for dense observation streams; validated by the unit test.
        self.reassoc = bool(reassoc)
        self.reassoc_gap = int(reassoc_gap)
        self.reassoc_iou = float(reassoc_iou)
        self._recent: dict[str, dict] = {}   # camera -> {local_id: (bbox, gid, frame)}
        # per-camera same-frame guard: gids already assigned in the current frame of a
        # camera are co-visible => different people => excluded from the next match.
        self._frame: dict[str, tuple] = {}   # camera -> (frame_idx, set(gids))

    # --- same-frame guard bookkeeping ------------------------------------------
    def _covis_gids(self, camera: str, frame_idx: int) -> set:
        fr, gids = self._frame.get(camera, (None, set()))
        if fr != frame_idx:
            gids = set()
            self._frame[camera] = (frame_idx, gids)
        return gids

    def _mark(self, camera: str, frame_idx: int, gid: int):
        self._covis_gids(camera, frame_idx).add(int(gid))

    # --- within-camera re-association bookkeeping ------------------------------
    def _remember(self, camera, local_id, bbox, gid, frame):
        self._recent.setdefault(camera, {})[int(local_id)] = (tuple(bbox), int(gid), int(frame))

    def _prune_recent(self, camera, frame):
        d = self._recent.get(camera)
        if not d:
            return
        for k in [k for k, v in d.items() if frame - v[2] > self.reassoc_gap]:
            del d[k]

    def _try_reassoc(self, obs, covis) -> Optional[int]:
        """gid of a just-departed same-camera track whose bbox this new track
        continues (best IoU over the gap), else None. Never re-uses a gid that is
        co-visible THIS frame (that person is on another active box)."""
        d = self._recent.get(obs.camera)
        if not d:
            return None
        best_iou, best_gid = self.reassoc_iou, None
        for llid, (lbox, lgid, lframe) in d.items():
            if llid == int(obs.local_id) or lgid in covis:
                continue
            if obs.frame_idx - lframe > self.reassoc_gap:
                continue
            v = _iou(obs.bbox, lbox)
            if v > best_iou:
                best_iou, best_gid = v, lgid
        return best_gid

    # --- exemplar memory --------------------------------------------------------
    @staticmethod
    def _store_all(ctx, person, obs):
        # the crop these pixels came from travels with the exemplar, so a stored
        # re-id memory can be looked at rather than only matched against
        crop = (obs.meta or {}).get("crop") or ""
        ctx.store.add_exemplar(person, "app", obs.app_emb, obs.quality, obs.camera,
                               obs.t, crop=crop)
        if obs.has_face():
            ctx.store.add_exemplar(person, "face", obs.face_emb, obs.quality, obs.camera,
                                   obs.t, crop=crop)
        if obs.has_gait():
            ctx.store.add_exemplar(person, "gait", obs.gait_emb, obs.quality, obs.camera,
                                   obs.t, crop=crop)
        if obs.color is not None:
            person.add_color(obs.camera, obs.color)

    # --- main -------------------------------------------------------------------
    def process(self, obs, person, ctx):
        have_gid = obs.gid is not None
        # act on an identity keyframe (appearance extracted) OR any obs the backbone
        # already resolved to a gid (so live boxes get an id even without a fresh emb).
        if not have_gid and not obs.has_app():
            return
        self._prune_recent(obs.camera, obs.frame_idx)

        bound = ctx.store.resolve_local(obs.camera, obs.local_id)
        if bound is not None:
            # The local track is sticky, but the backbone gid is still authoritative.
            # The engine can remap a live local track after a strong face/name anchor
            # (e.g. P127 -> P101).  If PLATF keeps the old binding forever, the UI may
            # show engine P101 while history/exemplars continue under the stale gid.
            if have_gid:
                incoming = int(obs.gid)
                p_auth = ctx.store.get_or_create(incoming, obs.t)
                rid = int(p_auth.global_id)
                if rid != int(bound):
                    covis = self._covis_gids(obs.camera, obs.frame_idx)
                    # Same-frame duplicate protection: if another box in this camera
                    # already claimed the incoming gid, do not fold two visible people.
                    if rid not in covis:
                        old = int(bound)
                        if ctx.store.get(old) is not None:
                            ctx.store.merge(rid, old)
                            p_auth = ctx.store.get(rid) or p_auth
                        ctx.store.bind_local(obs.camera, obs.local_id, rid)
                        self._store_all(ctx, p_auth, obs)
                        self._mark(obs.camera, obs.frame_idx, rid)
                        self._remember(obs.camera, obs.local_id, obs.bbox, rid, obs.frame_idx)
                        ctx.emit(Event("identity", obs.t, obs.camera, rid,
                                       payload={"authoritative_remap": True,
                                                "from_gid": old, "to_gid": rid,
                                                "raw_gid": incoming, "ingested": True}))
                        return
            # STICKY: a track keeps its id for life. Reinforce the gallery (if any) with
            # the fresh exemplar (improves OTHER tracks' matches) but never re-match.
            if self.sticky and self.gallery is not None and obs.has_app():
                self.gallery.reinforce(bound, obs.app_emb, obs.face_emb, obs.camera, obs.t, obs.gait_emb)
            p = person if person is not None else ctx.store.get_or_create(bound, obs.t)
            self._store_all(ctx, p, obs)
            self._mark(obs.camera, obs.frame_idx, bound)
            self._remember(obs.camera, obs.local_id, obs.bbox, bound, obs.frame_idx)
            return

        covis = self._covis_gids(obs.camera, obs.frame_idx)

        # WITHIN-CAMERA RE-ASSOCIATION (opt-in): a new local_id that spatially continues
        # a just-departed track inherits its person -- no match, no new id.
        if self.reassoc:
            rg = self._try_reassoc(obs, covis)
            if rg is not None:
                p = ctx.store.get_or_create(rg, obs.t)
                rid = p.global_id
                ctx.store.bind_local(obs.camera, obs.local_id, rid)
                if self.sticky and self.gallery is not None and obs.has_app():
                    self.gallery.reinforce(rid, obs.app_emb, obs.face_emb, obs.camera, obs.t, obs.gait_emb)
                self._store_all(ctx, p, obs)
                self._mark(obs.camera, obs.frame_idx, rid)
                self._remember(obs.camera, obs.local_id, obs.bbox, rid, obs.frame_idx)
                ctx.emit(Event("identity", obs.t, obs.camera, rid,
                               payload={"reassoc": True, "new": False, "cross_cam": False}))
                return

        # FIRST sight. INGEST the backbone gid when carried (single identity engine);
        # else fall back to a gallery match (legacy dumps / tests), excluding co-visible
        # gids (one person cannot be two boxes in one frame -> the same-frame guard).
        if have_gid:
            gid, dist, ingested = int(obs.gid), 0.0, True
        else:
            if self.gallery is None:
                return
            gid, dist = self.gallery.match(obs.app_emb, obs.face_emb, obs.camera, obs.t,
                                           obs.gait_emb, exclude_gids=set(covis) or None)
            if gid is None:
                return
            ingested = False

        p = ctx.store.get_or_create(gid, obs.t)
        rid = p.global_id                       # reported/metric id (survives merges)
        new = not p.local_ids
        # cross-cam link = this camera is new to a person already living elsewhere.
        # Computed BEFORE the host touches the person with this camera.
        cross_cam = bool(p.cameras_seen) and obs.camera not in p.cameras_seen
        ctx.store.bind_local(obs.camera, obs.local_id, rid)
        self._store_all(ctx, p, obs)
        self._mark(obs.camera, obs.frame_idx, rid)
        self._remember(obs.camera, obs.local_id, obs.bbox, rid, obs.frame_idx)
        ctx.emit(Event("identity", obs.t, obs.camera, rid,
                       payload={"dist": round(float(dist), 4), "new": bool(new),
                                "cross_cam": cross_cam, "ingested": ingested}))
