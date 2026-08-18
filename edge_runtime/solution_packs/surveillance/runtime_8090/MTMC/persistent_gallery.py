"""Persistent gid rejoin store for the live edge pipeline.

The live gallery (fusion_gallery_app) is in-memory only: max_age purges any
identity that goes unseen, and a restart wipes everything, so a person who
leaves and returns is minted a BRAND-NEW global id. This store fixes that
without touching the matching/fusion logic.

It keeps, on disk, the SAME representation the pipeline matches on -- up to
`cap_per_gid` exemplars per modality (appearance + face + gait, mirroring the
gallery's k=10). Two hooks, both driven from reid_service:

  observe(gid, face, app, gait, ...) -- accumulate a person's exemplars as they
                                        are tracked; survives max_age + restart.
  rejoin(face, app, gait)            -- called ONLY the first time a fresh gid
                                        appears. If it matches a retired id,
                                        return that old gid so the service remaps
                                        the fresh id back to it (like repair).

Rejoin decision, in order:
  1. FACE confident -> rejoin. Face is the one modality stable across a long
     absence, so it is the strongest evidence and needs no corroboration.
  2. No/weak face -> rejoin only if BODY and GAIT *both* point at the same
     retired id, each under its calibrated threshold. Requiring agreement is
     stricter than live matching (which can link on body alone) and is what
     lets no-face back-view people rejoin without re-introducing the
     clothing-only false merges.

Thresholds are the pipeline's own calibrated values, passed in by the service.
Matching is unaffected: rejoin fires strictly on ids the gallery just minted.
Disabled unless a store path is given (and faiss present).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

MODS = ("face", "app", "gait")


def _l2(m: np.ndarray) -> np.ndarray:
    m = np.ascontiguousarray(m.astype(np.float32))
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.clip(n, 1e-9, None)


def _valid(v) -> bool:
    return v is not None and float(np.linalg.norm(np.asarray(v, np.float32))) > 1e-6


class PersistentGallery:
    def __init__(self, path: str,
                 face_thr: float = 0.45,
                 app_thr: float = 0.0,
                 gait_thr: float = 0.0,
                 cap_per_gid: int = 10,
                 max_gids: int = 20000,
                 max_age_s: float = 86400.0,
                 observe_every_s: float = 2.0):
        self.path = Path(path)
        # per-modality cosine-distance ceilings. 0 => that modality does not drive
        # rejoin (still stored). Face alone can rejoin; app/gait must agree.
        self.thr = {"face": float(face_thr), "app": float(app_thr), "gait": float(gait_thr)}
        self.cap = int(cap_per_gid)
        self.max_gids = int(max_gids)
        self.max_age_s = float(max_age_s)
        self.observe_every_s = float(observe_every_s)

        # parallel raw stores (persisted); faiss indices are derived
        self.vecs: dict[str, list] = {m: [] for m in MODS}   # each entry (dim,)
        self.ids: dict[str, list] = {m: [] for m in MODS}
        self.per_gid: dict[str, dict] = {m: {} for m in MODS}
        self.dim: dict[str, int] = {m: 0 for m in MODS}
        self.index: dict[str, object] = {m: None for m in MODS}
        self.last_seen: dict[int, float] = {}
        self.next_gid = 1

        self._faiss = None
        self._last_obs: dict[int, float] = {}
        self.rejoins = 0
        self.rejoins_by_mod = {"face": 0, "app+gait": 0}
        self.evicted = 0
        self.enabled = False

        try:
            import faiss
            self._faiss = faiss
            self.enabled = True
        except Exception as e:
            print(f"[rejoin] disabled (no faiss): {e}", flush=True)
            return
        self.load()

    # ------------------------------------------------------------------ index
    def _rebuild(self):
        if not self.enabled:
            return
        for m in MODS:
            self.index[m] = None
            if self.vecs[m]:
                mat = _l2(np.vstack(self.vecs[m]))
                self.dim[m] = mat.shape[1]
                idx = self._faiss.IndexIDMap2(self._faiss.IndexFlatIP(self.dim[m]))
                idx.add_with_ids(mat, np.asarray(self.ids[m], dtype=np.int64))
                self.index[m] = idx

    # ------------------------------------------------------------------- load
    def load(self):
        f = self.path / "store.npz"
        meta = self.path / "meta.json"
        if not f.exists():
            self._rebuild()
            return
        try:
            z = np.load(f, allow_pickle=False)
            for m in MODS:
                v, i = z.get(f"{m}_vecs"), z.get(f"{m}_ids")
                if v is not None and v.size:
                    self.vecs[m] = [v[k] for k in range(v.shape[0])]
                    self.ids[m] = i.astype(int).tolist()
                    for g in self.ids[m]:
                        self.per_gid[m][g] = self.per_gid[m].get(g, 0) + 1
            if meta.exists():
                mm = json.loads(meta.read_text(encoding="utf-8"))
                self.next_gid = int(mm.get("next_gid", 1))
                self.last_seen = {int(k): float(v) for k, v in mm.get("last_seen", {}).items()}
            now = time.time()
            allg = self._all_gids()
            for g in allg:
                self.last_seen.setdefault(g, now)
            self.next_gid = max([self.next_gid] + [g + 1 for g in allg]) if allg else self.next_gid
            print(f"[rejoin] loaded {len(allg)} identities ("
                  + ", ".join(f"{len(self.ids[m])} {m}" for m in MODS)
                  + f") next_gid={self.next_gid} from {self.path}", flush=True)
        except Exception as e:
            print(f"[rejoin] load failed, starting empty: {e}", flush=True)
            self.vecs = {m: [] for m in MODS}
            self.ids = {m: [] for m in MODS}
        self._rebuild()

    def _all_gids(self) -> set:
        s = set()
        for m in MODS:
            s |= set(self.ids[m])
        return s

    # ------------------------------------------------------------------ query
    def _search(self, m: str, q) -> tuple[int, float]:
        idx = self.index[m]
        if idx is None or idx.ntotal == 0:
            return -1, 999.0
        q = _l2(np.asarray(q, np.float32).reshape(1, -1))
        if q.shape[1] != self.dim[m]:
            return -1, 999.0
        sims, ids = idx.search(q, min(20, idx.ntotal))
        best: dict[int, float] = {}
        for s, rid in zip(sims[0], ids[0]):
            if rid < 0:
                continue
            d = 1.0 - float(s)
            g = int(rid)
            if g not in best or d < best[g]:
                best[g] = d
        if not best:
            return -1, 999.0
        g, d = min(best.items(), key=lambda kv: kv[1])
        return g, d

    def rejoin(self, face=None, app=None, gait=None) -> tuple[int | None, float, str]:
        """(old_gid, dist, modality) if a fresh id is a returning person, else
        (None, ...). Face alone suffices; otherwise body AND gait must agree."""
        if not self.enabled:
            return None, 999.0, ""
        # 1) face is decisive on its own
        if self.thr["face"] > 0 and _valid(face):
            g, d = self._search("face", face)
            if g >= 0 and d <= self.thr["face"]:
                self.rejoins += 1
                self.rejoins_by_mod["face"] += 1
                return g, d, "face"
        # 2) body AND gait must point at the SAME retired id, each under threshold
        if self.thr["app"] > 0 and self.thr["gait"] > 0 and _valid(app) and _valid(gait):
            ga, da = self._search("app", app)
            gg, dg = self._search("gait", gait)
            if ga >= 0 and ga == gg and da <= self.thr["app"] and dg <= self.thr["gait"]:
                self.rejoins += 1
                self.rejoins_by_mod["app+gait"] += 1
                return ga, max(da, dg), "app+gait"
        return None, 999.0, ""

    # ---------------------------------------------------------------- observe
    def _add(self, m: str, gid: int, v):
        if not _valid(v) or self.per_gid[m].get(gid, 0) >= self.cap:
            return
        v = np.asarray(v, np.float32).reshape(-1)
        self.vecs[m].append(v)
        self.ids[m].append(int(gid))
        self.per_gid[m][gid] = self.per_gid[m].get(gid, 0) + 1
        if self.index[m] is None:
            self.dim[m] = v.shape[0]
            self.index[m] = self._faiss.IndexIDMap2(self._faiss.IndexFlatIP(self.dim[m]))
        if v.shape[0] == self.dim[m]:
            self.index[m].add_with_ids(_l2(v.reshape(1, -1)), np.asarray([gid], dtype=np.int64))

    def observe(self, gid: int, face=None, app=None, gait=None, t=None):
        """Accumulate exemplars for a live gid across all modalities.

        Face and gait are SPARSE (cadence + motion gate), so grab them whenever
        present. Appearance is DENSE (every frame) and near-identical frame to
        frame, so it is throttled per gid for exemplar diversity. All modalities
        are capped per gid."""
        if not self.enabled:
            return
        now = time.time()
        self.last_seen[gid] = now   # WALL clock: retention is a real-time question
        self._add("face", gid, face)
        self._add("gait", gid, gait)
        if now - self._last_obs.get(gid, 0.0) >= self.observe_every_s:
            self._last_obs[gid] = now
            self._add("app", gid, app)

    # ------------------------------------------------------------------ prune
    def _filter_to(self, keep: set):
        for m in MODS:
            v, i = [], []
            for vec, g in zip(self.vecs[m], self.ids[m]):
                if g in keep:
                    v.append(vec); i.append(g)
            self.vecs[m], self.ids[m] = v, i
            self.per_gid[m] = {}
            for g in i:
                self.per_gid[m][g] = self.per_gid[m].get(g, 0) + 1
        self.last_seen = {g: ls for g, ls in self.last_seen.items() if g in keep}
        self._last_obs = {g: v for g, v in self._last_obs.items() if g in keep}
        self._rebuild()

    def prune(self) -> int:
        if not self.enabled:
            return 0
        allg = self._all_gids()
        if not allg:
            return 0
        now = time.time()
        keep = {g for g in allg
                if self.max_age_s <= 0 or (now - self.last_seen.get(g, now)) <= self.max_age_s}
        if len(keep) > self.max_gids:
            keep = set(sorted(keep, key=lambda g: self.last_seen.get(g, 0.0),
                              reverse=True)[:self.max_gids])
        evicted = len(allg) - len(keep)
        if evicted:
            self._filter_to(keep)
            self.evicted += evicted
        return evicted

    # ------------------------------------------------------------------- save
    def save(self, next_gid_hint: int = 0):
        if not self.enabled:
            return
        self.prune()
        self.path.mkdir(parents=True, exist_ok=True)
        self.next_gid = max(self.next_gid, int(next_gid_hint))
        payload = {}
        for m in MODS:
            d = self.dim[m] or 512
            payload[f"{m}_vecs"] = (np.vstack(self.vecs[m]).astype(np.float32)
                                    if self.vecs[m] else np.zeros((0, d), np.float32))
            payload[f"{m}_ids"] = np.asarray(self.ids[m], np.int64)
        tmp = self.path / "store.npz.tmp"
        with open(tmp, "wb") as fh:   # a str path would make np.savez append ".npz"
            np.savez(fh, **payload)
        os.replace(tmp, self.path / "store.npz")
        meta = {"next_gid": int(self.next_gid), "version": 2,
                "n_ids": len(self._all_gids()),
                "last_seen": {str(k): round(v, 2) for k, v in self.last_seen.items()}}
        (self.path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def stats(self) -> dict:
        return {"store_ids": len(self._all_gids()),
                "store_exemplars": {m: len(self.ids[m]) for m in MODS},
                "rejoins": self.rejoins,
                "rejoins_by_mod": dict(self.rejoins_by_mod),
                "store_evicted": self.evicted,
                "store_max_age_h": round(self.max_age_s / 3600.0, 1) if self.max_age_s else 0,
                "next_gid": self.next_gid}
