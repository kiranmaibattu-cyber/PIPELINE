"""Semantic search over recorded history: find a person, then their path.

Two ways in, one way out:

  a NAME        "kiran"                  -> matched against enrolled faces and the
                                            names already attached to sightings
  a DESCRIPTION "man in a dark shirt"    -> IRRA (CLIP fine-tuned on CUHK-PEDES for
                                            pedestrian text->image retrieval) scored
                                            against each sighting's stored crop

Both return the same shape: PEOPLE, each with the sightings that matched, the path
those sightings trace across cameras, and which camera they are on right now if they
are still live. Sightings are the unit because they are what actually happened; the
person is an assembly of them, and the assembly can be wrong.

Indexing runs on a background thread against the history's own crops, and every
vector it computes is persisted (history `vec` table, kind='irra'). Restarting does
not re-encode work already done, and the index can be deleted and rebuilt from the
stored crops at any time -- the crops are the truth, the vectors are derived.

Honest limits, worth repeating to anyone reading results:
  * A description ranks CROPS. On ceiling-mounted cameras, clothing colour and
    coarse build carry; fine detail does not.
  * A person may exist as several ids (cross-camera appearance linking is off on
    this deployment), so one physical person can appear as more than one result.
    That is why every result carries its evidence.
"""
from __future__ import annotations

import os
import threading
import time

import numpy as np


class SemanticSearch:
    def __init__(self, history, face_gallery=None, live_lookup=None,
                 batch: int | None = None):
        self.history = history
        self.face_gallery = face_gallery
        # canonical_gid -> camera it is on right now (None if gone). Injected so this
        # module never reaches into the live store itself.
        self.live_lookup = live_lookup
        self.searcher = None
        self.status = "idle"
        self.batch = int(batch if batch is not None
                         else os.environ.get("SEARCH_INDEX_BATCH", "8"))
        self._thread = None
        self._lock = threading.RLock()
        self.indexed = 0
        self.last_query = None

    # -- model + indexing -------------------------------------------------

    def _load(self) -> bool:
        if self.searcher is not None:
            return True
        try:
            self.status = "loading IRRA (1.3 GB)…"
            from MTMC.text_search.models import IRRASearcher
            s = IRRASearcher()
            try:
                s.model.float()      # no CUDA on this box; fp32 on CPU
            except Exception:
                pass
            self.searcher = s
            self.status = "ready"
            return True
        except Exception as exc:
            self.status = f"load failed: {type(exc).__name__}: {exc}"[:160]
            return False

    def start(self) -> None:
        """Begin (or resume) background indexing. Safe to call repeatedly."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._index_loop, daemon=True)
            self._thread.start()

    def _index_loop(self, idle_s: float = 5.0) -> None:
        if not self._load():
            return
        import cv2
        while True:
            todo = []
            try:
                todo = self.history.vectors("irra", missing_only_kind="irra",
                                            limit=self.batch)
            except Exception:
                pass
            if not todo:
                time.sleep(idle_s)
                continue
            for row in todo:
                path = self.history.root / row["crop"]
                img = cv2.imread(str(path))
                if img is None:
                    # crop gone (pruned): store a zero vector so this sighting is not
                    # retried forever, and so it can never win a search.
                    self.history.put_vector(row["id"], "irra", np.zeros(512, np.float32))
                    continue
                try:
                    vec = self.searcher.encode_images([img])[0]
                    self.history.put_vector(row["id"], "irra", vec)
                    self.indexed += 1
                except Exception:
                    time.sleep(1.0)

    # -- search -----------------------------------------------------------

    def _names_matching(self, query: str) -> list:
        """Enrolled names the query could be referring to."""
        q = query.strip().lower()
        if not q:
            return []
        people = []
        try:
            if self.face_gallery is not None:
                people = list(self.face_gallery.gallery.people())
        except Exception:
            people = []
        try:                                    # names already on stored sightings
            rows = self.history.db.execute(
                "SELECT DISTINCT name FROM sighting WHERE name IS NOT NULL").fetchall()
            people += [r["name"] for r in rows]
        except Exception:
            pass
        seen, out = set(), []
        for p in people:
            if p and p.lower() not in seen and (q in p.lower() or p.lower() in q):
                seen.add(p.lower())
                out.append(p)
        return out

    def search(self, query: str, top_k: int = 8, since: float | None = None,
               camera: str | None = None) -> dict:
        query = str(query or "").strip()
        if not query:
            return {"query": "", "mode": None, "people": [],
                    "status": "type a name or describe someone"}

        names = self._names_matching(query)
        if names:
            return self._by_name(query, names, since=since, camera=camera,
                                 top_k=top_k)
        return self._by_description(query, top_k=top_k, since=since, camera=camera)

    def _by_name(self, query, names, since=None, camera=None, top_k=8) -> dict:
        rows = []
        for name in names:
            rows += self.history.sightings(name=name, since=since, camera=camera,
                                           limit=400)
        return {"query": query, "mode": "name", "matched_names": names,
                "status": "ok" if rows else "that name has not been seen yet",
                "people": self._group(rows, top_k=top_k)}

    def _by_description(self, query, top_k=8, since=None, camera=None) -> dict:
        self.start()
        if self.searcher is None:
            return {"query": query, "mode": "description", "people": [],
                    "status": self.status}
        self.last_query = query
        try:
            q = self.searcher.encode_text(query)
        except Exception as exc:
            return {"query": query, "mode": "description", "people": [],
                    "status": f"query failed: {type(exc).__name__}: {exc}"[:160]}
        vecs = self.history.vectors("irra", limit=20000)
        if not vecs:
            return {"query": query, "mode": "description", "people": [],
                    "status": f"nothing indexed yet ({self.status})"}
        ids = [v["sighting_id"] for v in vecs]
        mat = np.stack([v["vec"] for v in vecs])
        sims = mat @ np.asarray(q, np.float32).reshape(-1)
        order = np.argsort(-sims)
        # Pull enough sightings to fill top_k PEOPLE, since one person can own many.
        want, rows = top_k * 12, []
        by_id = {}
        for i in order[:want]:
            by_id[int(ids[i])] = float(sims[i])
        if by_id:
            marks = ",".join("?" * len(by_id))
            with self.history._lock:
                found = self.history.db.execute(
                    f"SELECT * FROM sighting WHERE id IN ({marks})",
                    list(by_id.keys())).fetchall()
            for r in found:
                d = dict(r)
                if since is not None and d["wall_start"] < since:
                    continue
                if camera and d["camera"] != camera:
                    continue
                d["score"] = round(by_id.get(d["id"], 0.0), 4)
                rows.append(d)
        return {"query": query, "mode": "description",
                "status": "ok" if rows else "no match in the indexed history",
                "indexed": len(vecs), "people": self._group(rows, top_k=top_k)}

    def _app_vector(self, person: dict):
        """This person's best stored body embedding, for matching against live tracks.
        Prefers the highest-quality sighting, which is the one most likely to embed
        cleanly."""
        best = max(person["sightings"], key=lambda s: s.get("quality") or 0.0,
                   default=None)
        for sid in ([best["id"]] if best else []) + [s["id"] for s in person["path"]]:
            try:
                with self.history._lock:
                    row = self.history.db.execute(
                        "SELECT dim, data FROM vec WHERE sighting_id=? AND kind='app'",
                        (int(sid),)).fetchone()
            except Exception:
                return None
            if row:
                return np.frombuffer(row["data"], np.float32, count=int(row["dim"]))
        return None

    def _best_matched_crop(self, rows: list) -> dict | None:
        """Pick a display crop from the sightings that actually matched a query."""
        best, best_s = None, -1.0
        for r in rows:
            if r.get("score") is None or not r.get("crop"):
                continue
            s = self.history.display_score(r.get("crop_h"), r.get("crop_w"),
                                           r.get("quality"))
            # Keep text relevance primary, then use display quality as tie-breaker.
            rank = (float(r.get("score") or 0.0), s)
            if best is None or rank > best_s:
                best, best_s = r, rank
        if best is None:
            return None
        return {"sighting": best["id"], "crop": best["crop"], "camera": best["camera"],
                "wall_start": best["wall_start"], "score": best_s[1],
                "matched": True, "match_score": round(float(best.get("score") or 0.0), 4),
                "wh": [best.get("crop_w"), best.get("crop_h")]}

    def _group(self, rows: list, top_k: int = 8) -> list:
        """Sightings -> people. One entry per canonical id, carrying its evidence."""
        people: dict = {}
        for r in rows:
            g = int(r["canonical_gid"])
            p = people.setdefault(g, {"person": g, "score": 0.0, "names": set(),
                                      "cameras": [], "sightings": [],
                                      "_matched_rows": [],
                                      "first_seen": r["wall_start"],
                                      "last_seen": r["wall_start"]})
            p["score"] = max(p["score"], float(r.get("score") or 0.0))
            if r.get("name"):
                p["names"].add(r["name"])
            p["first_seen"] = min(p["first_seen"], r["wall_start"])
            p["last_seen"] = max(p["last_seen"], r["wall_start"])
            p["sightings"].append({
                "id": r["id"], "camera": r["camera"], "t_start": r["t_start"],
                "t_end": r["t_end"], "wall_start": r["wall_start"],
                "n_obs": r["n_obs"], "quality": round(r["quality"], 3),
                "name": r["name"], "crop": bool(r["crop"]),
                "indexable": bool(r.get("indexable", 1)),
                # visit still in progress -- the person may still be in frame
                "live": not bool(r.get("closed", 1)),
                "score": r.get("score")})
            p["_matched_rows"].append(r)
        out = []
        for g, p in people.items():
            p["sightings"].sort(key=lambda s: s["t_start"])
            p["cameras"] = sorted({s["camera"] for s in p["sightings"]})
            p["names"] = sorted(p["names"])
            p["n_sightings"] = len(p["sightings"])
            # The full path, not only the sightings that matched the query: the
            # question is always "where has this person been", and a description
            # only ever matches the crops where they looked like the description.
            path = self.history.path_of(g, limit=200)
            p["path"] = [{"id": r["id"], "camera": r["camera"], "t_start": r["t_start"],
                          "t_end": r["t_end"], "wall_start": r["wall_start"],
                          "name": r["name"], "crop": bool(r["crop"]),
                          "live": not bool(r.get("closed", 1)),
                          "indexable": bool(r.get("indexable", 1))} for r in path]
            # "Where is he NOW" has to survive id churn: pass this person's stored
            # appearance so the live store can be matched by body as well as by id.
            cam = how = None
            if self.live_lookup:
                try:
                    cam, how = self.live_lookup(g, self._app_vector(p))
                except Exception:
                    cam, how = None, None
            p["live_camera"], p["live_match"] = cam, how
            # The thumbnail is chosen for HUMAN confirmation, not for the match: the
            # sighting that matched is often a torso or a back view. The matched crops
            # stay in `sightings` as evidence.
            try:
                # If this result carries a name, the thumbnail must come from a
                # sighting the face confirmed -- a contaminated id otherwise shows
                # a stranger under someone else's name.
                nm = p["names"][0] if p.get("names") else None
                if nm:
                    p["best_crop"] = self.history.best_display_crop(g, name=nm)
                else:
                    p["best_crop"] = self._best_matched_crop(p["_matched_rows"])
                if p["best_crop"] is None and not nm:
                    p["best_crop"] = self.history.best_display_crop(g)
            except Exception:
                p["best_crop"] = None
            p.pop("_matched_rows", None)
            out.append(p)
        out.sort(key=lambda p: (-p["score"], -p["last_seen"]))
        return out[:top_k]

    def stats(self) -> dict:
        try:
            have = len(self.history.vectors("irra", limit=100000))
            todo = len(self.history.vectors("irra", missing_only_kind="irra",
                                            limit=100000))
        except Exception:
            have = todo = 0
        return {"status": self.status, "indexed": have, "pending": todo,
                "this_session": self.indexed, "last_query": self.last_query}
