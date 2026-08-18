"""Person object + PersonStore -- the platform's identity memory.

Today identity is scattered across maps in reid_service/worker (track_gid,
local2global, gcache, fcache, chists, stable_id) + the gallery's FusionEntry. The
platform unifies that into ONE persistent record every feature attaches to (re-id,
face, gait, zones, analytics, the 2D map). The detector/tracker is NOT the centre --
the Person is.

PersonStore owns the (camera, local_id) -> global_id binding (what the re-id plugin
writes) and the Person lifecycle (mint / touch / prune). Per-modality exemplars are
capped and quality-ranked so one bad crop never defines an identity.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .schemas import IdentityMapping

try:
    import numpy as np
except Exception:
    np = None

MODALITIES = ("app", "face", "gait")


@dataclass(eq=False)
class Exemplar:
    """eq=False is load-bearing, not a style choice.

    A generated __eq__ compares every field, including the numpy embedding, and
    `array == array` yields an array -- so `list.remove(e)` raises "truth value of
    an array is ambiguous" the moment it tests against a non-identical entry.
    Identity is also the semantics wanted here: two exemplars are the same entry
    only if they are the same object. (Same reason FACE gallery Vec sets eq=False.)
    """

    emb: object                      # np.ndarray
    quality: float
    camera: str
    t: float
    crop: str = ""                   # the crop these pixels came from, so a stored
    #                                  exemplar can be SEEN and audited, not just matched


@dataclass
class Person:
    # global_id is the INTEGER reported/metric id (== the first backbone gid bound here,
    # so non-merge output stays identical to the raw pipeline). person_uuid is the
    # PERSISTENT platform key that survives gid churn + merges -- what names/identity
    # links attach to. gids is the set of backbone gid HYPOTHESES folded into this person.
    global_id: int
    first_seen: float
    last_seen: float
    # Lifecycle expiry uses one process-local clock. Observation timestamps may mix
    # recorded-video epochs and live RTSP monotonic time and cannot be compared safely.
    last_active_mono: float = field(default_factory=time.monotonic)

    person_uuid: str = ""                                # persistent truth key
    gids: set = field(default_factory=set)               # backbone gid hypotheses bound here
    local_ids: set = field(default_factory=set)          # {(camera, local_id)}
    app_embs: list = field(default_factory=list)          # [Exemplar] (capped)
    face_embs: list = field(default_factory=list)
    gait_embs: list = field(default_factory=list)

    cameras_seen: set = field(default_factory=set)
    color_hists: dict = field(default_factory=dict)       # camera -> [torso colour hist] (capped)
    current_camera: str = ""
    current_zone: Optional[str] = None
    previous_cameras: list = field(default_factory=list)  # ordered camera history
    trajectory: list = field(default_factory=list)        # [(t, camera, foot_point)]

    speed: float = 0.0
    direction: Optional[float] = None
    height_est: float = 0.0

    rejected_exemplars: int = 0        # contradicted the memory -> refused
    behavior_state: str = "active"
    alert_history: list = field(default_factory=list)
    attributes: dict = field(default_factory=dict)        # name, employee?, etc.
    usecase_states: dict = field(default_factory=dict)     # per-plugin state (zone timers...)

    def _bucket(self, modality: str) -> list:
        return {"app": self.app_embs, "face": self.face_embs, "gait": self.gait_embs}[modality]

    @staticmethod
    def _coherent(bucket: list, emb, purity: float) -> bool:
        """Would this exemplar contradict the memory it is joining?

        A global id is not owned by a track -- a track re-queries the gallery every
        couple of seconds and can be reassigned -- so a SECOND person's observations
        can be handed the same gid and their crops land in the same memory. Measured
        on a real contaminated id: the two people's exemplars formed clean blocks,
        every within-person pair <= 0.13 and every across-person pair >= 0.23. This
        refuses an exemplar that is far from EVERY exemplar already stored, which is
        the shape contamination takes; an ordinary new view of the same person always
        resembles at least one existing view.

        Deliberately compares against the NEAREST existing exemplar, not a centroid:
        a memory that has already been polluted has a centroid between two people and
        would then accept anybody.
        """
        if np is None or purity <= 0 or len(bucket) < 3:
            return True                      # too little evidence to contradict
        v = np.asarray(emb, np.float32).reshape(-1)
        n = float(np.linalg.norm(v))
        if n < 1e-6:
            return True
        v = v / n
        best = 2.0
        for ex in bucket:
            u = np.asarray(ex.emb, np.float32).reshape(-1)
            un = float(np.linalg.norm(u))
            if un < 1e-6 or u.shape != v.shape:
                continue
            best = min(best, 1.0 - float(v @ (u / un)))
        return best <= purity

    def add_exemplar(self, modality: str, emb, quality: float, camera: str, t: float,
                     cap: int = 10, crop: str = "", purity: float = 0.0) -> bool:
        """Keep `cap` exemplars per modality, spread across the cameras that saw them.

        Eviction is per-CAMERA, not global-by-quality. Sorting the whole bucket by
        quality and truncating looks like "keep the best", but it means a person who
        walks into a second camera contributes nothing whenever that camera's crops
        score lower -- which is exactly the case that matters, because the reason to
        keep several exemplars is to recognise someone under a DIFFERENT view. Here a
        full bucket evicts the weakest exemplar of whichever camera currently holds
        the most, so every camera keeps a foothold and quality still decides within it.
        """
        if emb is None:
            return False
        if np is not None and isinstance(emb, np.ndarray) and float(np.linalg.norm(emb)) < 1e-6:
            return False
        b = self._bucket(modality)
        if not self._coherent(b, emb, purity):
            self.rejected_exemplars += 1
            return False
        b.append(Exemplar(emb, float(quality), camera, float(t), str(crop or "")))
        while len(b) > cap:
            counts: dict = {}
            for e in b:
                counts[e.camera] = counts.get(e.camera, 0) + 1
            fullest = max(counts.items(), key=lambda kv: kv[1])[0]
            worst = min((e for e in b if e.camera == fullest), key=lambda e: e.quality)
            b.remove(worst)
        return True

    def add_color(self, camera: str, hist, cap: int = 12) -> None:
        """Store a torso colour histogram per camera (capped) for the cross-cam colour gate."""
        if hist is None or not camera:
            return
        lst = self.color_hists.setdefault(camera, [])
        lst.append(hist)
        if len(lst) > cap:
            del lst[0]

    def touch(self, t: float, camera: str, foot_point=None):
        self.last_seen = float(t)
        self.last_active_mono = time.monotonic()
        if camera and camera != self.current_camera:
            if self.current_camera:
                self.previous_cameras.append(self.current_camera)
            self.current_camera = camera
        if camera:
            self.cameras_seen.add(camera)
        if foot_point is not None:
            self.trajectory.append((float(t), camera, tuple(foot_point)))
            if len(self.trajectory) > 512:
                del self.trajectory[0]

    @property
    def multi_camera(self) -> bool:
        return len(self.cameras_seen) > 1


class PersonStore:
    """Thread-safe registry of Persons + the (camera, local_id) -> gid binding."""

    def __init__(self, max_age_s: float = 86400.0, exemplar_cap: int = 10,
                 exemplar_purity: float | None = None):
        self._lock = threading.RLock()
        self._persons: dict[int, Person] = {}   # keyed by global_id (int, reported id)
        self._by_uuid: dict[str, Person] = {}   # persistent-key index
        self._gid_uuid: dict[int, str] = {}     # backbone gid HYPOTHESIS -> person_uuid
        self._bind: dict[tuple, int] = {}       # (camera, local_id) -> global_id
        self._next_gid = 1
        self._uuid_seq = 0
        self.identity = IdentityMapping()       # person_uuid -> enrolled IdentityLink
        self.max_age_s = float(max_age_s)
        self.exemplar_cap = int(exemplar_cap)
        # Reject an exemplar further than this from EVERY exemplar already held.
        # 0.20 sits in the measured gap between same-person (<=0.13) and
        # different-person (>=0.23) distances. 0 disables the gate.
        self.exemplar_purity = float(
            exemplar_purity if exemplar_purity is not None
            else os.environ.get("EXEMPLAR_PURITY", "0.20"))

    def _mint_uuid(self) -> str:
        self._uuid_seq += 1
        return f"p{self._uuid_seq:06d}"

    def _new_person(self, global_id: int, t: float) -> Person:
        p = Person(global_id=int(global_id), first_seen=float(t), last_seen=float(t),
                   person_uuid=self._mint_uuid())
        self._persons[int(global_id)] = p
        self._by_uuid[p.person_uuid] = p
        return p

    def mint(self, t: float) -> Person:
        """Mint a Person with a fresh auto global_id and no backbone gid bound (tests /
        pre-identity)."""
        with self._lock:
            gid = self._next_gid
            self._next_gid += 1
            return self._new_person(gid, t)

    def get(self, gid: int) -> Optional[Person]:
        with self._lock:
            return self._persons.get(int(gid))

    def canonical_gid(self, gid: int) -> int:
        """Map a raw backbone gid to the stable global_id of the Person that owns it."""
        with self._lock:
            g = int(gid)
            u = self._gid_uuid.get(g)
            if u is not None:
                p = self._by_uuid.get(u)
                if p is not None:
                    return int(p.global_id)
            return g

    def get_by_uuid(self, person_uuid: str) -> Optional[Person]:
        with self._lock:
            return self._by_uuid.get(str(person_uuid))

    def resolve_person(self, camera: str, local_id: int) -> Optional[Person]:
        """The bound Person for a camera-local track (None if unbound)."""
        with self._lock:
            gid = self._bind.get((camera, int(local_id)))
            return self._persons.get(gid) if gid is not None else None

    def get_or_create(self, gid: int, t: float) -> Person:
        """Resolve the Person that owns backbone gid `gid`, minting one if this gid is
        unseen. gid is a HYPOTHESIS, not the key: a fresh gid mints a Person whose
        reported global_id == gid (so non-merge output matches the raw pipeline exactly);
        a gid already folded into a Person (via merge / evidence) resolves to that
        Person. The persistent identity is Person.person_uuid, not the gid."""
        with self._lock:
            gid = int(gid)
            u = self._gid_uuid.get(gid)
            if u is not None:
                p = self._by_uuid.get(u)
                if p is not None:
                    return p
            p = self._persons.get(gid)
            if p is None:
                p = self._new_person(gid, t)
            p.gids.add(gid)
            self._gid_uuid[gid] = p.person_uuid
            self._next_gid = max(self._next_gid, gid + 1)
            return p

    def add_exemplar(self, person: Person, modality: str, emb, quality: float,
                     camera: str, t: float, crop: str = "") -> bool:
        """Store an exemplar using the store's configured cap -- the single place the
        per-modality memory-size policy lives, so plugins can't forget it."""
        return person.add_exemplar(modality, emb, quality, camera, t,
                                   cap=self.exemplar_cap, crop=crop,
                                   purity=self.exemplar_purity)

    def bind_local(self, camera: str, local_id: int, gid: int):
        """Re-id plugin binds a camera-local track to a global Person."""
        with self._lock:
            self._bind[(camera, int(local_id))] = int(gid)
            p = self._persons.get(int(gid))
            if p is not None:
                p.local_ids.add((camera, int(local_id)))

    def resolve_local(self, camera: str, local_id: int) -> Optional[int]:
        with self._lock:
            return self._bind.get((camera, int(local_id)))

    def merge(self, keep_gid: int, drop_gid: int):
        """Fold drop_gid's Person into keep_gid's (evidence-driven repair merges only).
        The dropped gid becomes another HYPOTHESIS of the surviving Person; every gid,
        binding, and identity link re-points to the survivor's uuid. Caller must supply
        evidence + never fuse two co-visible gids (same-frame guard)."""
        with self._lock:
            keep = self._persons.get(int(keep_gid))
            drop = self._persons.pop(int(drop_gid), None)
            if keep is None or drop is None or keep is drop:
                return
            keep.local_ids |= drop.local_ids
            keep.cameras_seen |= drop.cameras_seen
            keep.gids |= drop.gids
            keep.gids.add(int(drop_gid))
            for cam, hists in drop.color_hists.items():
                kl = keep.color_hists.setdefault(cam, [])
                kl.extend(hists)
                del kl[:-12]
            for m in MODALITIES:
                keep._bucket(m).extend(drop._bucket(m))
                keep._bucket(m).sort(key=lambda e: e.quality, reverse=True)
                del keep._bucket(m)[self.exemplar_cap:]
            # re-point every gid hypothesis + local binding from drop -> keep
            for g, uu in list(self._gid_uuid.items()):
                if uu == drop.person_uuid:
                    self._gid_uuid[g] = keep.person_uuid
            for k, v in list(self._bind.items()):
                if v == int(drop_gid):
                    self._bind[k] = int(keep_gid)
            self._by_uuid.pop(drop.person_uuid, None)
            self.identity.reassign(drop.person_uuid, keep.person_uuid)

    def prune(self, now: float) -> int:
        with self._lock:
            if self.max_age_s <= 0:
                return 0
            mono_now = time.monotonic()
            dead = [g for g, p in self._persons.items()
                    if mono_now - p.last_active_mono > self.max_age_s]
            dead_uuids = set()
            for g in dead:
                p = self._persons.pop(g, None)
                if p is not None:
                    dead_uuids.add(p.person_uuid)
                    self._by_uuid.pop(p.person_uuid, None)
            if dead:
                dset = set(dead)
                self._bind = {k: v for k, v in self._bind.items() if v not in dset}
                self._gid_uuid = {g: u for g, u in self._gid_uuid.items() if u not in dead_uuids}
            return len(dead)

    def all(self) -> list:
        with self._lock:
            return list(self._persons.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._persons)
