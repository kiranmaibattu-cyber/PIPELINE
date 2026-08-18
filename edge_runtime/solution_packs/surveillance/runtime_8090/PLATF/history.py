"""Durable history: who was seen, where, when -- and the evidence for it.

Everything the platform knew was in RAM: events in a list, identities in a
PersonStore that purges after `max_age_s`, crop paths in a dict, and the crop files
themselves in a FIFO cache that deletes the oldest once it hits its cap. A restart
was total amnesia, and the cache was already saturated, so evidence was being
deleted continuously. Nothing could reconstruct where a person had been.

This module is the spine that fixes it. The unit is a SIGHTING: one person, on one
camera, for one visit -- not one row per frame. A person crossing three cameras
leaves three rows, each with its own time window, best crop and best embeddings.

    sighting   gid + canonical_gid, camera, t_start..t_end, n_obs, quality,
               name (if a face was recognised), map position, best crop
    event      the Activity log, durable, joined to sightings by gid + camera + time
    merge      every identity merge, so history RE-POINTS via canonical_gid instead
               of being rewritten -- an audit trail you can undo by eye
    vec        per-sighting appearance / face / IRRA embeddings: the search indexes

Two rules this file is built on, both learned the hard way elsewhere in this project:

  Raw data is the truth; indexes are derived. Vectors live here as blobs. A FAISS
  or IRRA index is rebuilt from them and can be deleted at any time without loss
  (same split MTMC/persistent_gallery.py uses: store.npz persisted, faiss derived).

  Evidence gets its own copy. A sighting's best crop is COPIED out of the volatile
  cache into the history store, because the cache deletes oldest-first and would
  silently erase exactly the old sightings a search is most useful for.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS sighting(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  gid           INTEGER NOT NULL,
  canonical_gid INTEGER NOT NULL,
  camera        TEXT    NOT NULL,
  t_start       REAL    NOT NULL,   -- observation clock (camera video/wall)
  t_end         REAL    NOT NULL,
  wall_start    REAL    NOT NULL,   -- wall clock, for human-readable history
  n_obs         INTEGER NOT NULL DEFAULT 0,
  quality       REAL    NOT NULL DEFAULT 0,
  name          TEXT,               -- enrolled name, once a face confirms one
  face_dist     REAL,
  face_det      REAL,               -- detector confidence behind the best face_emb
  face_px       REAL,               -- face width in pixels
  map_x         REAL,
  map_y         REAL,
  crop          TEXT,               -- path relative to the history root
  crop_h        INTEGER,
  crop_w        INTEGER,
  indexable     INTEGER NOT NULL DEFAULT 1,  -- 0 = crop unfit for text search
  closed        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sighting_canon ON sighting(canonical_gid);
CREATE INDEX IF NOT EXISTS ix_sighting_cam   ON sighting(camera, t_start);
CREATE INDEX IF NOT EXISTS ix_sighting_name  ON sighting(name);
CREATE INDEX IF NOT EXISTS ix_sighting_wall  ON sighting(wall_start);

CREATE TABLE IF NOT EXISTS event(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  t             REAL NOT NULL,
  wall          REAL NOT NULL,
  type          TEXT NOT NULL,
  camera        TEXT,
  gid           INTEGER,
  canonical_gid INTEGER,
  zone          TEXT,
  name          TEXT,
  payload       TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_wall ON event(wall);
CREATE INDEX IF NOT EXISTS ix_event_type ON event(type, wall);
CREATE INDEX IF NOT EXISTS ix_event_gid  ON event(canonical_gid);

CREATE TABLE IF NOT EXISTS merge(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  wall     REAL NOT NULL,
  survivor INTEGER NOT NULL,
  dropped  INTEGER NOT NULL,
  reason   TEXT,
  verdict  TEXT,                      -- plausible | implausible | impossible
  applied  INTEGER NOT NULL DEFAULT 1 -- 0 = refused, kept as a record
);
CREATE INDEX IF NOT EXISTS ix_merge_dropped ON merge(dropped);

-- The re-id working memory: up to `exemplar_cap` per (person, modality), spread
-- across the cameras that saw them. Unlike `vec`, which is one row per VISIT and
-- only grows, this is a rolling set that is replaced as better exemplars arrive.
-- Each row keeps the crop the embedding was computed from, copied out of the
-- volatile cache, so a stored re-id memory can be looked at and not only matched.
CREATE TABLE IF NOT EXISTS exemplar(
  gid       INTEGER NOT NULL,
  modality  TEXT    NOT NULL,        -- app | face | gait
  slot      INTEGER NOT NULL,
  camera    TEXT,
  quality   REAL,
  t         REAL,
  dim       INTEGER NOT NULL,
  data      BLOB    NOT NULL,
  crop      TEXT,                    -- path relative to the history root
  src       TEXT,                    -- the cache path it was copied from
  updated   REAL    NOT NULL,
  PRIMARY KEY (gid, modality, slot)
);
CREATE INDEX IF NOT EXISTS ix_exemplar_gid ON exemplar(gid);

CREATE TABLE IF NOT EXISTS vec(
  sighting_id INTEGER NOT NULL,
  kind        TEXT    NOT NULL,     -- app | face | irra
  dim         INTEGER NOT NULL,
  data        BLOB    NOT NULL,
  PRIMARY KEY (sighting_id, kind)
);
"""


def _blob(v) -> bytes:
    return np.asarray(v, np.float32).reshape(-1).tobytes()


def _unblob(b, dim: int) -> np.ndarray:
    return np.frombuffer(b, np.float32, count=int(dim))


class History:
    """SQLite history. One writer thread's worth of traffic, so a single connection
    with a lock is enough and avoids a connection pool's failure modes.

    Writes are batched at sighting CLOSE, not per observation: a busy camera
    produces ~15 observations/second per person and one row per visit instead.
    """

    def __init__(self, root: str, gap_s: float | None = None,
                 keep_days: float | None = None, topology=None,
                 min_obs: int | None = None, min_dur_s: float | None = None,
                 strict_merge: bool | None = None, min_aspect: float | None = None,
                 min_crop_h: int | None = None):
        self.root = Path(root)
        self.crops = self.root / "crops"
        self.crops.mkdir(parents=True, exist_ok=True)
        # A visit ends after this long without an observation. Shorter than the
        # tracker's max_age would split one visit into several rows; much longer
        # would glue two separate visits into one and ruin the timeline.
        self.gap_s = float(gap_s if gap_s is not None
                           else os.environ.get("HISTORY_GAP_S", "20"))
        self.keep_days = float(keep_days if keep_days is not None
                               else os.environ.get("HISTORY_KEEP_DAYS", "1"))
        # Evidence gate. A one-frame blip is a detection artefact, not a visit, and a
        # path assembled from blips is noise that looks like data. A visit must clear
        # BOTH bars to be written.
        self.min_obs = int(min_obs if min_obs is not None
                           else os.environ.get("HISTORY_MIN_OBS", "3"))
        self.min_dur_s = float(min_dur_s if min_dur_s is not None
                               else os.environ.get("HISTORY_MIN_DUR_S", "0.5"))
        # How often an in-flight visit's row is refreshed. Every observation would be
        # ~15 writes/second per person; this keeps a live row current enough to search
        # while the write rate stays proportional to the number of people, not frames.
        self.live_write_s = float(os.environ.get("HISTORY_LIVE_WRITE_S", "3"))
        # Learned camera transition windows, {(a, b): (min_s, max_s)}, for judging
        # whether a claimed cross-camera link is physically possible.
        self.topology = topology
        # Which crops are worth indexing for text search. Text->image retrieval is
        # trained on FULL-BODY pedestrian images; a head-and-shoulders crop of
        # someone seated at a desk is out of distribution and returns noise that
        # looks like a result. Measured on 1200 real crops from these cameras:
        # aspect >= 1.8 and height >= 120 keeps ~17%, and inspection of the kept vs
        # dropped sets shows it separates standing/walking people from seated,
        # occluded and back-of-head crops. The sighting is still RECORDED either
        # way -- this only decides what the description index sees.
        self.min_aspect = float(min_aspect if min_aspect is not None
                                else os.environ.get("SEARCH_MIN_ASPECT", "1.8"))
        self.min_crop_h = int(min_crop_h if min_crop_h is not None
                              else os.environ.get("SEARCH_MIN_CROP_H", "120"))
        self.strict_merge = (strict_merge if strict_merge is not None
                             else os.environ.get("HISTORY_STRICT_MERGE", "1") == "1")
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.root / "history.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # WAL: the search reads while ingest writes, and the default rollback
        # journal makes readers block writers.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self._open: dict = {}      # (gid, camera) -> in-flight sighting dict
        self.stats = {"sightings": 0, "events": 0, "merges": 0, "crops": 0}

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently leaves an existing table alone, so a
        store created by an earlier build keeps the old shape and the first INSERT
        against a new column fails at runtime. Adding columns is the one schema
        change SQLite does cheaply and without rewriting the table.
        """
        wanted = {
            "merge": [("verdict", "TEXT"), ("applied", "INTEGER NOT NULL DEFAULT 1")],
            "sighting": [("crop_h", "INTEGER"), ("crop_w", "INTEGER"),
                         ("indexable", "INTEGER NOT NULL DEFAULT 1"),
                         ("face_det", "REAL"), ("face_px", "REAL")],
        }
        for table, cols in wanted.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # -- ingest ----------------------------------------------------------

    def observe(self, gid: int, camera: str, t: float, quality: float = 0.0,
                crop_abs: str | None = None, app_emb=None, face_emb=None,
                name: str | None = None, face_dist: float | None = None,
                map_xy=None, face_meta: dict | None = None, bbox=None) -> None:
        """Fold one observation into the open sighting for (gid, camera).

        Keeps the BEST evidence rather than the latest: the crop and embeddings
        stored are the highest-quality ones seen during the visit, because a
        person's last frame is often the worst (leaving the frame, motion blur).
        """
        gid = int(gid)
        key = (gid, str(camera))
        with self._lock:
            s = self._open.get(key)
            if s is not None and t - s["t_end"] > self.gap_s:
                self._close_locked(key, s)
                s = None
            if s is None:
                s = {"gid": gid, "camera": str(camera), "t_start": float(t),
                     "t_end": float(t), "wall_start": time.time(), "n_obs": 0,
                     "quality": -1.0, "crop": None, "app": None, "face": None,
                     "name": None, "face_dist": None, "map": None,
                     "face_det": 0.0, "face_px": 0.0, "display": -1.0,
                     "row_id": None, "last_write": 0.0, "crop_written": None,
                     "name_crop_locked": False}
                self._open[key] = s
            s["t_end"] = max(s["t_end"], float(t))
            s["n_obs"] += 1
            if map_xy is not None:
                s["map"] = (float(map_xy[0]), float(map_xy[1]))
            # A name, once confirmed by a face, sticks to the visit even if later
            # frames show the back of the head.
            name_this_obs = bool(name and face_dist is not None)
            if name_this_obs and (s["name"] is None or (face_dist is not None
                                                        and (s["face_dist"] is None
                                                             or face_dist < s["face_dist"]))):
                s["name"], s["face_dist"] = str(name), face_dist
            if face_emb is not None and s["face"] is None:
                s["face"] = np.asarray(face_emb, np.float32).copy()
            # keep the STRONGEST face detection of the visit, so a weak chip aligned
            # from hair cannot masquerade as the evidence for this sighting
            if face_meta:
                det = float(face_meta.get("det") or 0.0)
                if det > float(s.get("face_det") or 0.0):
                    s["face_det"] = det
                    s["face_px"] = float(face_meta.get("w") or 0.0)
            q = float(quality or 0.0)
            if q > s["quality"]:
                s["quality"] = q
                if app_emb is not None:
                    s["app"] = np.asarray(app_emb, np.float32).copy()
            # The stored crop is chosen by DISPLAY score, not by raw quality. Quality
            # rewards a sharp, well-exposed crop -- which a seated torso often is --
            # while what a person needs to confirm an identity, and what a full-body
            # retrieval model needs to embed, is the WHOLE person. Derived from the
            # bbox so it costs no file read on the observation path.
            if crop_abs and os.path.exists(crop_abs):
                ds = 0.0
                if bbox is not None and len(bbox) == 4:
                    bw = abs(float(bbox[2]) - float(bbox[0]))
                    bh = abs(float(bbox[3]) - float(bbox[1]))
                    ds = self.display_score(bh, bw, q)
                # Once a visit has a crop from the observation that freshly matched
                # an enrolled face, do not let later no-face frames replace it.  That
                # is the exact stale-name contamination path seen in history: a good
                # looking body crop from a tracker/gid switch can otherwise become
                # the hero image for an enrolled name.
                if s.get("name_crop_locked") and not name_this_obs:
                    pass
                elif ds > s.get("display", -1.0):
                    s["display"] = ds
                    s["crop"] = crop_abs
                    if name_this_obs:
                        s["name_crop_locked"] = True
            # Make the visit visible while it is still happening: create the row once
            # the evidence gate is met, then refresh it on a cadence. Without this a
            # person standing in front of the camera has no row, so search cannot find
            # them and "where are they now" can only ever answer for people who left.
            now_w = time.time()
            if s.get("row_id") is None:
                if self._gate_met(s):
                    self._materialise_locked(s)
                    s["last_write"] = now_w
            elif now_w - s.get("last_write", 0.0) >= self.live_write_s:
                self._update_locked(s)

    def flush(self, now: float | None = None, force: bool = False) -> int:
        """Close visits that have gone quiet. Called on a timer, so an in-flight
        sighting is never lost to a crash for longer than one gap window."""
        now = float(now if now is not None else time.time())
        closed = 0
        with self._lock:
            for key, s in list(self._open.items()):
                if force or now - s["t_end"] > self.gap_s:
                    self._close_locked(key, s)
                    closed += 1
        return closed

    def _gate_met(self, s) -> bool:
        """Enough evidence that this is a visit and not a detection blip."""
        return (s["n_obs"] >= self.min_obs
                and (s["t_end"] - s["t_start"]) >= self.min_dur_s)

    def _write_crop_locked(self, sid, s) -> None:
        """Copy the visit's current best crop out of the volatile cache.

        Re-copied when a better view arrives mid-visit, because a person who stands
        up halfway through should be represented by the standing frame, not by
        whatever happened to be stored first.
        """
        if not s["crop"] or s["crop"] == s.get("crop_written"):
            return
        try:
            day = time.strftime("%Y%m%d", time.localtime(s["wall_start"]))
            dst_dir = self.crops / day
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / f"{sid}.jpg"
            shutil.copyfile(s["crop"], dst)
            rel = str(dst.relative_to(self.root)).replace("\\", "/")
            h, w, ok = self._crop_shape(dst)
            self.db.execute(
                "UPDATE sighting SET crop=?, crop_h=?, crop_w=?, indexable=? WHERE id=?",
                (rel, h, w, int(ok), sid))
            s["crop_written"] = s["crop"]
            self.stats["crops"] += 1
            if not ok:
                self.stats["not_indexable"] = self.stats.get("not_indexable", 0) + 1
        except OSError:
            pass

    def _write_vectors_locked(self, sid, s) -> None:
        for kind, vec in (("app", s["app"]), ("face", s["face"])):
            if vec is not None and getattr(vec, "size", 0):
                self.db.execute(
                    "INSERT OR REPLACE INTO vec(sighting_id,kind,dim,data) VALUES(?,?,?,?)",
                    (sid, kind, int(vec.size), _blob(vec)))

    def _materialise_locked(self, s) -> int:
        """Write the row for a visit that is still IN PROGRESS.

        Sightings used to be written only when a visit ended, which meant anyone
        currently in front of a camera had no row at all -- so a search could never
        find the people you most want to find, and "where are they now" was answerable
        only for people who had already left. The row is created as soon as the
        evidence gate is met (a few tenths of a second) with closed=0, then updated on
        a cadence rather than on every observation, which would be ~15 writes/second
        per person.
        """
        cur = self.db.execute(
            "INSERT INTO sighting(gid,canonical_gid,camera,t_start,t_end,wall_start,"
            "n_obs,quality,name,face_dist,face_det,face_px,map_x,map_y,crop,closed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (s["gid"], s["gid"], s["camera"], s["t_start"], s["t_end"], s["wall_start"],
             s["n_obs"], max(0.0, s["quality"]), s["name"], s["face_dist"],
             s.get("face_det") or None, s.get("face_px") or None,
             (s["map"] or (None, None))[0], (s["map"] or (None, None))[1], None))
        sid = int(cur.lastrowid)
        s["row_id"] = sid
        self._write_crop_locked(sid, s)
        self._write_vectors_locked(sid, s)
        self.db.commit()
        self.stats["sightings"] += 1
        self.stats["live_rows"] = self.stats.get("live_rows", 0) + 1
        return sid

    def _update_locked(self, s, closed: bool = False) -> None:
        sid = s.get("row_id")
        if sid is None:
            return
        self.db.execute(
            "UPDATE sighting SET canonical_gid=?, t_end=?, n_obs=?, quality=?, name=?, "
            "face_dist=?, face_det=?, face_px=?, map_x=?, map_y=?, closed=? WHERE id=?",
            (s["gid"], s["t_end"], s["n_obs"], max(0.0, s["quality"]), s["name"],
             s["face_dist"], s.get("face_det") or None, s.get("face_px") or None,
             (s["map"] or (None, None))[0], (s["map"] or (None, None))[1],
             int(closed), sid))
        self._write_crop_locked(sid, s)
        self._write_vectors_locked(sid, s)
        self.db.commit()
        s["last_write"] = time.time()

    def _close_locked(self, key, s) -> int:
        self._open.pop(key, None)
        sid = s.get("row_id")
        if sid is None:
            # Never reached the evidence gate: a blip, not a visit. Nothing was
            # written, so there is nothing to clean up.
            self.stats["dropped_blips"] = self.stats.get("dropped_blips", 0) + 1
            return 0
        self._update_locked(s, closed=True)
        return sid

    def _crop_shape(self, path):
        """(height, width, is_full_body_enough_to_index). Reads the JPEG header only."""
        try:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
        except Exception:
            try:
                import cv2
                im = cv2.imread(str(path))
                if im is None:
                    return None, None, False
                h, w = im.shape[:2]
            except Exception:
                return None, None, False
        ok = (h >= self.min_crop_h and (h / max(1, w)) >= self.min_aspect)
        return int(h), int(w), bool(ok)

    def save_exemplars(self, gid: int, modality: str, exemplars: list,
                       crop_root: str = "") -> int:
        """Persist one person's rolling re-id memory, crops included.

        Replaces the whole set for (gid, modality) because it IS a set -- slots churn
        as better views arrive. Crop files are keyed by their SOURCE basename, so a
        re-save that keeps the same underlying crop copies nothing; only genuinely new
        exemplars cost a write.
        """
        gid = int(gid)
        rows, keep = [], set()
        dst_dir = self.crops / "exemplars" / str(gid)
        for slot, ex in enumerate(exemplars):
            v = np.asarray(getattr(ex, "emb", None), np.float32).reshape(-1)
            if v.size == 0:
                continue
            src = str(getattr(ex, "crop", "") or "")
            rel = None
            if src:
                name = os.path.basename(src)
                out = dst_dir / name
                rel = str(out.relative_to(self.root)).replace("\\", "/")
                keep.add(name)
                if not out.exists() and crop_root:
                    try:
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(os.path.join(crop_root, src), out)
                    except OSError:
                        rel = None
            rows.append((gid, str(modality), slot, getattr(ex, "camera", None),
                         float(getattr(ex, "quality", 0.0)), float(getattr(ex, "t", 0.0)),
                         int(v.size), _blob(v), rel, src, time.time()))
        with self._lock:
            self.db.execute("DELETE FROM exemplar WHERE gid=? AND modality=?",
                            (gid, str(modality)))
            self.db.executemany(
                "INSERT INTO exemplar(gid,modality,slot,camera,quality,t,dim,data,crop,"
                "src,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()
            still = {os.path.basename(r["crop"]) for r in self.db.execute(
                "SELECT crop FROM exemplar WHERE gid=? AND crop IS NOT NULL", (gid,))}
        # drop crops no longer referenced by ANY modality of this person
        if dst_dir.exists():
            for f in dst_dir.iterdir():
                if f.name not in still:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        return len(rows)

    def exemplars(self, gid: int, modality: str | None = None) -> list:
        q = "SELECT gid,modality,slot,camera,quality,t,dim,crop,updated FROM exemplar WHERE gid=?"
        args = [int(gid)]
        if modality:
            q += " AND modality=?"; args.append(str(modality))
        q += " ORDER BY modality, slot"
        with self._lock:
            return [dict(r) for r in self.db.execute(q, args).fetchall()]

    def exemplar_crop_path(self, gid: int, modality: str, slot: int):
        with self._lock:
            r = self.db.execute(
                "SELECT crop FROM exemplar WHERE gid=? AND modality=? AND slot=?",
                (int(gid), str(modality), int(slot))).fetchone()
        if r is None or not r["crop"]:
            return None
        p = self.root / r["crop"]
        return str(p) if p.exists() else None

    def backfill_crop_shapes(self, limit: int = 5000) -> dict:
        """Judge crops that were stored before the gate existed.

        The migration gives every existing row `indexable=1` by default, which would
        leave pre-gate crops in the search index forever -- exactly the seated,
        head-and-shoulders crops the gate was added to keep out. This measures them
        and drops the text-search vectors of any that now fail, so the index ends up
        holding only what the gate would have admitted.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT id, crop FROM sighting WHERE crop IS NOT NULL AND crop_h IS NULL "
                "LIMIT ?", (int(limit),)).fetchall()
        checked = dropped = 0
        for r in rows:
            path = self.root / r["crop"]
            h, w, ok = self._crop_shape(path) if path.exists() else (None, None, False)
            with self._lock:
                self.db.execute(
                    "UPDATE sighting SET crop_h=?, crop_w=?, indexable=? WHERE id=?",
                    (h, w, int(ok), r["id"]))
                if not ok:
                    # remove it from the search index; the sighting itself stays
                    self.db.execute("DELETE FROM vec WHERE sighting_id=? AND kind='irra'",
                                    (r["id"],))
                    dropped += 1
            checked += 1
        if checked:
            with self._lock:
                self.db.commit()
        return {"checked": checked, "now_excluded": dropped}

    def add_event(self, ev: dict, canonical=None) -> None:
        payload = ev.get("payload") or {}
        with self._lock:
            self.db.execute(
                "INSERT INTO event(t,wall,type,camera,gid,canonical_gid,zone,name,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (float(ev.get("t") or 0.0), time.time(), str(ev.get("type")),
                 ev.get("camera"), ev.get("person_id"),
                 canonical if canonical is not None else ev.get("person_id"),
                 ev.get("zone"), payload.get("employee_id"),
                 json.dumps(payload, default=str)))
            self.db.commit()
        self.stats["events"] += 1

    def recent_events(self, limit: int = 500, since: float | None = None,
                      include_identity: bool = False) -> list[dict]:
        """Stored events, newest first, with a crop to show for each.

        add_event() has existed since the beginning; this is its missing counterpart.
        Without a reader, 1800 events accumulated in the table that nothing could ever
        display, and the dashboard fell back to the in-memory list -- which is capped at
        200 and starts empty after every restart.

        `identity` rows are excluded by default: they are the platform's own bookkeeping
        (1769 of 1813 rows here) and are not events anyone acts on.

        The event table holds no image of its own, so the snapshot is the sighting of the
        SAME identity closest in time to the event -- which is what an operator means by
        "the picture at that moment".

        That pairing is done in TWO queries, not a correlated subquery, because SQLite
        will not resolve an outer column inside a subquery's ORDER BY: `e.canonical_gid`
        binds fine in the WHERE, but `ABS(s.wall_start - e.wall)` raises "no such column:
        e.wall". Two queries plus a dict is also cheaper than 500 round trips, so this is
        the better shape regardless of the limitation.
        """
        sql = ["SELECT e.id, e.wall, e.t, e.type, e.camera, e.canonical_gid AS gid,",
               "       e.zone, e.name, e.payload",
               "  FROM event e WHERE 1=1"]
        args: list = []
        if not include_identity:
            sql.append("AND e.type NOT IN ('identity','person_merged')")
        if since is not None:
            sql.append("AND e.wall >= ?")
            args.append(float(since))
        sql.append("ORDER BY e.wall DESC LIMIT ?")
        args.append(max(1, int(limit)))

        with self._lock:
            rows = self.db.execute(" ".join(sql), args).fetchall()
            gids = sorted({r["gid"] for r in rows if r["gid"] is not None})
            crops: dict[int, list] = {}
            # chunked so a wide page cannot exceed SQLite's variable limit
            for i in range(0, len(gids), 400):
                chunk = gids[i:i + 400]
                q = ("SELECT id, canonical_gid, wall_start FROM sighting "
                     "WHERE crop IS NOT NULL AND canonical_gid IN (%s)"
                     % ",".join("?" * len(chunk)))
                for s in self.db.execute(q, chunk):
                    crops.setdefault(s["canonical_gid"], []).append(
                        (s["wall_start"], s["id"]))

        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"]) if r["payload"] else {}
            except Exception:
                payload = {}
            cand = crops.get(r["gid"])
            snap = min(cand, key=lambda c: abs((c[0] or 0.0) - (r["wall"] or 0.0)))[1] if cand else None
            out.append({"id": r["id"], "wall": r["wall"], "t": r["t"],
                        "type": r["type"], "camera": r["camera"],
                        "person_id": r["gid"], "zone": r["zone"],
                        "name": r["name"], "payload": payload,
                        "sighting": snap})
        return out

    def link_check(self, a: int, b: int) -> dict:
        """Could these two identities be the same person? Evidence, not opinion.

        impossible  they were on the SAME camera at the SAME time. Two bodies cannot
                    be one person, whatever the appearance score says. This is the
                    one verdict that is a hard fact rather than a threshold.
        implausible the cameras' learned transition window cannot accommodate the
                    time between the two visits (walked it too fast, or too slow).
        plausible   nothing contradicts it. NOT a claim that it is the same person.
        """
        rows_a = self.path_of(int(a), limit=500)
        rows_b = self.path_of(int(b), limit=500)
        if not rows_a or not rows_b:
            return {"verdict": "plausible", "reason": "no history for one side"}
        for ra in rows_a:
            for rb in rows_b:
                if ra["camera"] != rb["camera"]:
                    continue
                # Overlapping windows on one camera, compared on the WALL clock.
                # t_start/t_end are the camera's stream clock, which restarts at zero
                # every time the app restarts -- two sightings 34 minutes apart both
                # landed at "second 61-70" of their own run and were declared
                # co-visible, which refuses a CORRECT merge and records it as a
                # contradiction. Wall time is the only clock comparable across runs.
                a0, a1 = ra["wall_start"], ra["wall_start"] + (ra["t_end"] - ra["t_start"])
                b0, b1 = rb["wall_start"], rb["wall_start"] + (rb["t_end"] - rb["t_start"])
                if a0 <= b1 and b0 <= a1:
                    return {"verdict": "impossible",
                            "reason": f"co-visible on {ra['camera']} "
                                      f"({time.strftime('%H:%M:%S', time.localtime(a0))}"
                                      f"+{a1-a0:.0f}s vs "
                                      f"{time.strftime('%H:%M:%S', time.localtime(b0))}"
                                      f"+{b1-b0:.0f}s)"}
        if self.topology:
            last_a, first_b = rows_a[-1], rows_b[0]
            if last_a["camera"] != first_b["camera"]:
                win = (self.topology.get((last_a["camera"], first_b["camera"]))
                       or self.topology.get((first_b["camera"], last_a["camera"])))
                if win:
                    # wall clock again: a transition window is meaningless measured
                    # on two stream clocks that restart independently
                    gap = first_b["wall_start"] - (
                        last_a["wall_start"] + (last_a["t_end"] - last_a["t_start"]))
                    lo, hi = float(win[0]), float(win[1])
                    if gap >= 0 and not (lo <= gap <= hi):
                        return {"verdict": "implausible",
                                "reason": f"{last_a['camera']}->{first_b['camera']} gap "
                                          f"{gap:.0f}s outside learned {lo:.0f}-{hi:.0f}s"}
        return {"verdict": "plausible", "reason": ""}

    def add_merge(self, survivor: int, dropped: int, reason: str = "") -> dict:
        """Re-point history at the survivor, unless the link is provably impossible.

        Every merge is recorded with its verdict, including refused ones: a merge the
        history rejected is exactly what you want to see when a path looks wrong. The
        old rows keep their original gid, so nothing is erased and a bad merge stays
        visible instead of silently rewriting where someone was.
        """
        survivor, dropped = int(survivor), int(dropped)
        check = self.link_check(survivor, dropped)
        applied = not (self.strict_merge and check["verdict"] == "impossible")
        # The backbone can emit an authoritative remap before history has enough
        # evidence to contradict it. Treat that as a pending hint, not a historical
        # rewrite; these empty-history merges were the main path for gallery
        # contamination when two visually different people crossed or churned ids.
        if (str(reason or "").startswith("authoritative_backbone_gid")
                and check["reason"] == "no history for one side"):
            check = {**check, "verdict": "pending"}
            applied = False
        with self._lock:
            self.db.execute(
                "INSERT INTO merge(wall,survivor,dropped,reason,verdict,applied) "
                "VALUES(?,?,?,?,?,?)",
                (time.time(), survivor, dropped,
                 f"{reason}{': ' + check['reason'] if check['reason'] else ''}",
                 check["verdict"], int(applied)))
            if applied:
                self.db.execute("UPDATE sighting SET canonical_gid=? WHERE canonical_gid=?",
                                (survivor, dropped))
                self.db.execute("UPDATE event SET canonical_gid=? WHERE canonical_gid=?",
                                (survivor, dropped))
                for s in self._open.values():      # in-flight visits follow the merge
                    if s["gid"] == dropped:
                        s["gid"] = survivor
            self.db.commit()
        self.stats["merges"] += 1
        if not applied:
            self.stats["refused_merges"] = self.stats.get("refused_merges", 0) + 1
        return {**check, "applied": applied}

    # -- read ------------------------------------------------------------

    def sightings(self, canonical_gid=None, camera=None, name=None,
                  since=None, limit: int = 500) -> list:
        q = "SELECT * FROM sighting WHERE 1=1"
        args: list = []
        if canonical_gid is not None:
            q += " AND canonical_gid=?"; args.append(int(canonical_gid))
        if camera:
            q += " AND camera=?"; args.append(str(camera))
        if name:
            q += " AND name=?"; args.append(str(name))
        if since is not None:
            q += " AND wall_start>=?"; args.append(float(since))
        q += " ORDER BY t_start DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self.db.execute(q, args).fetchall()]

    def path_of(self, canonical_gid: int, limit: int = 200) -> list:
        """One person's movement, oldest first -- the cross-camera path."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM sighting WHERE canonical_gid=? ORDER BY t_start ASC LIMIT ?",
                (int(canonical_gid), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def dossier(self, canonical_gid: int, limit: int = 500) -> dict:
        """Everything recorded about ONE person, assembled into a single object.

        The tables are normalised -- sightings here, vectors there, crops on disk --
        which is right for writing but makes it hard to answer "show me this person".
        This is that view: per-camera totals, then every visit with its crop and the
        embeddings attached to it.

        Note this is the HISTORICAL record: one best appearance/face vector PER VISIT,
        accumulating for as long as the person is seen. It is not the recognition
        working set, which is a rolling best-10 per modality held by the live store
        and the rejoin store.
        """
        g = int(canonical_gid)
        with self._lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT * FROM sighting WHERE canonical_gid=? ORDER BY t_start ASC LIMIT ?",
                (g, int(limit))).fetchall()]
            kinds = {}
            if rows:
                marks = ",".join("?" * len(rows))
                for v in self.db.execute(
                        f"SELECT sighting_id, kind, dim FROM vec "
                        f"WHERE sighting_id IN ({marks})", [r["id"] for r in rows]):
                    kinds.setdefault(v["sighting_id"], {})[v["kind"]] = v["dim"]
            merges = [dict(m) for m in self.db.execute(
                "SELECT * FROM merge WHERE survivor=? OR dropped=? ORDER BY wall",
                (g, g)).fetchall()]
        cams: dict = {}
        visits = []
        for r in rows:
            vk = kinds.get(r["id"], {})
            c = cams.setdefault(r["camera"], {"visits": 0, "crops": 0, "vectors": {},
                                              "first": r["wall_start"],
                                              "last": r["wall_start"]})
            c["visits"] += 1
            c["crops"] += 1 if r["crop"] else 0
            c["last"] = max(c["last"], r["wall_start"])
            for k in vk:
                c["vectors"][k] = c["vectors"].get(k, 0) + 1
            visits.append({
                "sighting": r["id"], "gid": r["gid"], "camera": r["camera"],
                "wall_start": r["wall_start"], "t_start": r["t_start"],
                "duration_s": round(r["t_end"] - r["t_start"], 1),
                "n_obs": r["n_obs"], "quality": round(r["quality"], 3),
                "name": r["name"], "face_dist": r["face_dist"],
                "crop": r["crop"], "crop_wh": [r["crop_w"], r["crop_h"]],
                "indexable": bool(r["indexable"]), "vectors": vk})
        return {
            "person": g,
            "names": sorted({r["name"] for r in rows if r["name"]}),
            "n_sightings": len(rows),
            "cameras": cams,
            "first_seen": rows[0]["wall_start"] if rows else None,
            "last_seen": rows[-1]["wall_start"] if rows else None,
            "source_gids": sorted({r["gid"] for r in rows}),
            "merges": merges,
            "exemplars": self.exemplars(g),   # the rolling re-id memory, with crops
            "visits": visits,
        }

    def identity_audit(self, canonical_gid: int, limit: int = 1000) -> dict:
        """Read-only contamination report for one canonical identity.

        A canonical id is allowed to have many raw gids after merges, but two raw
        gids under the same canonical id should never be visible on the same camera
        at the same wall-clock time. When that happens, the identity is contaminated
        and needs a split, not another merge.
        """
        g = int(canonical_gid)
        with self._lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT * FROM sighting WHERE canonical_gid=? ORDER BY wall_start ASC LIMIT ?",
                (g, int(limit))).fetchall()]
            merges = [dict(m) for m in self.db.execute(
                "SELECT * FROM merge WHERE survivor=? OR dropped=? ORDER BY wall DESC LIMIT 200",
                (g, g)).fetchall()]

        sources: dict = {}
        for r in rows:
            sid = int(r["gid"])
            s = sources.setdefault(sid, {
                "gid": sid, "visits": 0, "named": 0, "unnamed": 0, "names": {},
                "cameras": set(), "first_seen": r["wall_start"],
                "last_seen": r["wall_start"], "best_crop": None,
            })
            s["visits"] += 1
            s["cameras"].add(r["camera"])
            s["first_seen"] = min(s["first_seen"], r["wall_start"])
            s["last_seen"] = max(s["last_seen"], r["wall_start"])
            if r.get("name"):
                s["named"] += 1
                s["names"][r["name"]] = s["names"].get(r["name"], 0) + 1
            else:
                s["unnamed"] += 1
            if r.get("crop"):
                score = self.display_score(r.get("crop_h"), r.get("crop_w"),
                                           r.get("quality"))
                cur = s.get("best_crop")
                if cur is None or score > cur["score"]:
                    s["best_crop"] = {
                        "sighting": r["id"], "crop": r["crop"], "camera": r["camera"],
                        "wall_start": r["wall_start"], "score": score,
                        "wh": [r.get("crop_w"), r.get("crop_h")],
                    }

        overlaps = []
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if a["gid"] == b["gid"] or a["camera"] != b["camera"]:
                    continue
                a0 = a["wall_start"]
                a1 = a["wall_start"] + (a["t_end"] - a["t_start"])
                b0 = b["wall_start"]
                b1 = b["wall_start"] + (b["t_end"] - b["t_start"])
                if a0 <= b1 and b0 <= a1:
                    overlaps.append({
                        "camera": a["camera"],
                        "a": {"gid": a["gid"], "sighting": a["id"], "wall_start": a0,
                              "name": a["name"], "crop": a["crop"]},
                        "b": {"gid": b["gid"], "sighting": b["id"], "wall_start": b0,
                              "name": b["name"], "crop": b["crop"]},
                    })
                    if len(overlaps) >= 50:
                        break
            if len(overlaps) >= 50:
                break

        src = []
        for s in sources.values():
            s["cameras"] = sorted(s["cameras"])
            s["names"] = dict(sorted(s["names"].items()))
            src.append(s)
        src.sort(key=lambda s: (-s["visits"], s["gid"]))
        name_counts = {}
        for r in rows:
            if r.get("name"):
                name_counts[r["name"]] = name_counts.get(r["name"], 0) + 1
        refused = [m for m in merges if not int(m.get("applied", 1))]
        return {
            "person": g,
            "n_sightings": len(rows),
            "source_gids": src,
            "names": dict(sorted(name_counts.items())),
            "overlaps": overlaps,
            "refused_merges": refused,
            "contaminated": bool(overlaps or len(name_counts) > 1),
        }

    @staticmethod
    def display_score(crop_h, crop_w, quality) -> float:
        """How well does this crop let a HUMAN confirm who the person is?

        Different question from the search index, which only needs the crop to be
        embeddable. Measured on this deployment: a whole standing person lands at
        aspect 1.8-3.5 and 200px+ tall; a seated torso cropped at desk level is
        ~150px at aspect 2.1-2.6 (so aspect alone cannot reject it -- height must);
        and a legs-only sliver reaches aspect 11, which is MORE elongated than any
        real person, so the shape term has to be a band and not a floor.
        """
        try:
            h, w, q = float(crop_h or 0), float(crop_w or 0), float(quality or 0)
        except (TypeError, ValueError):
            return 0.0
        if h <= 0 or w <= 0:
            return 0.0
        a = h / w
        shape = 1.0 if 2.0 <= a <= 3.5 else max(0.0, 1.0 - abs(a - 2.75) / 2.75)
        size = min(1.0, h / 300.0)          # taller crop = more of the person visible
        return round(0.45 * shape + 0.35 * size + 0.20 * q, 4)

    def best_display_crop(self, canonical_gid: int, name: str | None = None) -> dict | None:
        """The crop that best shows WHO this person is, across their sightings.

        Search results otherwise show whichever sighting happened to match, which is
        frequently a torso or a back view -- technically the right identity, but a
        human cannot confirm it from the picture.

        `name` is a CORRECTNESS constraint, not a preference. A global id can hold
        sightings of more than one person (gid 61 held a seated man, two women in
        kurtas, and Kiran). Choosing the best-looking crop across all of them showed
        a stranger under Kiran's name. When the result is named, only sightings whose
        FACE confirmed that name may supply the thumbnail -- the picture then always
        agrees with the label, whatever else the id has absorbed.
        """
        q = ("SELECT s.id, s.crop, s.crop_w, s.crop_h, s.quality, s.camera, "
             "s.wall_start, s.name FROM sighting s "
             "WHERE s.canonical_gid=? AND s.crop IS NOT NULL")
        args = [int(canonical_gid)]
        if name:
            # `sighting.name` from older runs is not enough.  The audit found many
            # named rows with no face vector at all, because identity links were
            # carried by gid/uuid after the face disappeared.  A named thumbnail must
            # come from a visit with actual face evidence and an accepted match
            # distance; otherwise return no picture rather than a confident stranger.
            max_dist = float(os.environ.get("HISTORY_NAME_CROP_MAX_DIST", "0.45"))
            q += (" AND s.name=? AND s.face_dist IS NOT NULL AND s.face_dist<=? "
                  "AND EXISTS(SELECT 1 FROM vec v "
                  "WHERE v.sighting_id=s.id AND v.kind='face')")
            args.extend([str(name), max_dist])
        with self._lock:
            rows = [dict(r) for r in self.db.execute(q, args).fetchall()]
        if not rows and name:      # named but no confirmed crop: better none than wrong
            return None
        best, best_s = None, -1.0
        for r in rows:
            s = self.display_score(r["crop_h"], r["crop_w"], r["quality"])
            if s > best_s:
                best, best_s = r, s
        if best is None:
            return None
        return {"sighting": best["id"], "crop": best["crop"], "camera": best["camera"],
                "wall_start": best["wall_start"], "score": best_s,
                "confirmed": bool(best.get("name")),
                "wh": [best["crop_w"], best["crop_h"]]}

    def names_seen(self, canonical_gid: int) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT name FROM sighting WHERE canonical_gid=? AND name IS NOT NULL",
                (int(canonical_gid),)).fetchall()
        return [r["name"] for r in rows]

    def vectors(self, kind: str, missing_only_kind: str | None = None,
                limit: int = 256) -> list:
        """Sightings with a `kind` vector -- or, with missing_only_kind, those that
        HAVE `kind` but still lack `missing_only_kind` (the indexer's work queue)."""
        with self._lock:
            if missing_only_kind:
                rows = self.db.execute(
                    "SELECT s.id, s.crop FROM sighting s WHERE s.crop IS NOT NULL "
                    "AND s.indexable=1 "        # full-body crops only: see min_aspect
                    "AND NOT EXISTS(SELECT 1 FROM vec v WHERE v.sighting_id=s.id AND v.kind=?)"
                    " ORDER BY s.id DESC LIMIT ?", (missing_only_kind, int(limit))).fetchall()
                return [dict(r) for r in rows]
            rows = self.db.execute(
                "SELECT sighting_id, dim, data FROM vec WHERE kind=? LIMIT ?",
                (kind, int(limit))).fetchall()
        return [{"sighting_id": r["sighting_id"], "vec": _unblob(r["data"], r["dim"])}
                for r in rows]

    def put_vector(self, sighting_id: int, kind: str, vec) -> None:
        v = np.asarray(vec, np.float32).reshape(-1)
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO vec(sighting_id,kind,dim,data) VALUES(?,?,?,?)",
                (int(sighting_id), str(kind), int(v.size), _blob(v)))
            self.db.commit()

    def summary(self) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) n, MIN(wall_start) a, MAX(wall_start) b FROM sighting"
            ).fetchone()
            ev = self.db.execute("SELECT COUNT(*) n FROM event").fetchone()["n"]
            people = self.db.execute(
                "SELECT COUNT(DISTINCT canonical_gid) n FROM sighting").fetchone()["n"]
            vecs = {r["kind"]: r["n"] for r in self.db.execute(
                "SELECT kind, COUNT(*) n FROM vec GROUP BY kind").fetchall()}
            idx = self.db.execute(
                "SELECT COUNT(*) n FROM sighting WHERE crop IS NOT NULL AND indexable=1"
            ).fetchone()["n"]
            with_crop = self.db.execute(
                "SELECT COUNT(*) n FROM sighting WHERE crop IS NOT NULL").fetchone()["n"]
        return {"sightings": row["n"], "people": people, "events": ev,
                "oldest": row["a"], "newest": row["b"], "vectors": vecs,
                "with_crop": with_crop, "indexable": idx,
                "crop_gate": {"min_aspect": self.min_aspect, "min_h": self.min_crop_h},
                "open": len(self._open), "session": dict(self.stats),
                "keep_days": self.keep_days}

    # -- retention -------------------------------------------------------

    def prune(self) -> dict:
        """Drop history past the retention window, evidence included.

        Bounded by AGE, not by a row cap: a fixed cap silently deletes the oldest
        sighting the moment traffic spikes, which is exactly when history matters.
        """
        if self.keep_days <= 0:
            return {"sightings": 0, "events": 0, "exemplars": 0, "files": 0}
        cutoff = time.time() - self.keep_days * 86400.0
        with self._lock:
            doomed = [r["crop"] for r in self.db.execute(
                "SELECT crop FROM sighting WHERE wall_start<? AND crop IS NOT NULL",
                (cutoff,)).fetchall()]
            doomed_ex = [r["crop"] for r in self.db.execute(
                "SELECT crop FROM exemplar WHERE updated<? AND crop IS NOT NULL",
                (cutoff,)).fetchall()]
            self.db.execute("DELETE FROM vec WHERE sighting_id IN "
                            "(SELECT id FROM sighting WHERE wall_start<?)", (cutoff,))
            n_s = self.db.execute("DELETE FROM sighting WHERE wall_start<?",
                                  (cutoff,)).rowcount
            n_e = self.db.execute("DELETE FROM event WHERE wall<?", (cutoff,)).rowcount
            n_x = self.db.execute("DELETE FROM exemplar WHERE updated<?",
                                  (cutoff,)).rowcount
            self.db.commit()
        files = 0
        for rel in doomed + doomed_ex:
            try:
                (self.root / rel).unlink()
                files += 1
            except OSError:
                pass
        for day in sorted(self.crops.glob("*")):    # drop emptied crop folders
            try:
                if not day.is_dir():
                    continue
                if day.name == "exemplars":
                    for gid_dir in sorted(day.glob("*")):
                        try:
                            if gid_dir.is_dir() and not any(gid_dir.iterdir()):
                                gid_dir.rmdir()
                        except OSError:
                            pass
                if not any(day.iterdir()):
                    day.rmdir()
            except OSError:
                pass
        return {"sightings": n_s, "events": n_e, "exemplars": n_x, "files": files}

    def close(self) -> None:
        self.flush(force=True)
        with self._lock:
            self.db.close()
