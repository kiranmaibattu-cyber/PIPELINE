"""Platform dashboard server -- the PLATFORM's own UI, separate from the MTMC app.

Two modes:
  --stream FILE   one-shot: replay a recorded observation stream, serve the result.
  --live   DIR    LIVE: tail the backbone's OBS_DUMP_DIR as it grows, feed new
                  observations through a PERSISTENT plugin host, and serve the
                  PersonStore + live boxes updating in real time.

The frontend is JUST the live view: each camera's video (proxied from the backbone,
the sensor) with the re-id output drawn on it -- person boxes + global ids. Nothing
else runs on the live unless the user picks a use-case, and a use-case is only
pickable once it is actually built + configured. Right now only Re-ID is ready; the
other use-cases (face, intrusion, loitering, counting, absence, BEV map) are listed
in the UI but disabled. No analytics plugin runs uninvited.

Own port (default 8090); touches neither :8082 nor the :8083 two-tier app. The backbone
is untouched: the platform is a downstream CONSUMER of the observation stream it dumps.

    python -m PLATF.server --stream obs.jsonl --gallery real
    python -m PLATF.server --live /tmp/obs_cap --gallery real
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re as _re
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from PLATF.core import Event, EventBus, IdentityLink, PersonStore, PluginHost, TrackObservation
from PLATF.plugins.reid import ReIDPlugin
from PLATF.replay import build_gallery

STATE: dict = {"summary": {}, "persons": [], "events": []}
_LOCK = threading.Lock()

# source frame pixel space the backbone boxes are in (canvas coord system).
# fallback coordinate space if frame-size detection fails (engine serves 640x360 frames;
# bbox/foot-points are in that space). The app detects the real size at startup.
FRAME_W, FRAME_H = (int(x) for x in os.environ.get("PLATF_FRAME", "640x360").split("x"))

# the backbone (MTMC pipeline) is the SENSOR: it serves the camera frames the platform
# proxies so the whole live app lives on one port. It is not a second UI.
BACKBONE_URL = os.environ.get("BACKBONE_URL", "http://localhost:8083")
CAM_SID: dict = {}          # camera name -> backbone stream id (for frame proxy)


class _ManagementOutbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        raw = os.environ.get("MANAGEMENT_EVENTS_PATH")
        return cls(Path(raw)) if raw else None

    def write(self, event: dict) -> None:
        row = dict(event)
        row.setdefault("solution_pack", "surveillance")
        row.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")


def _app_id_for_event(event_type: str | None) -> str:
    mapping = {
        "identity": "reid",
        "face_recognized": "face_recognition",
        "unauthorised": "face_recognition",
        "face_enrolled": "face_recognition",
        "intrusion": "intrusion",
        "loitering": "loitering",
        "count": "people_counting",
        "absence": "absence",
        "person_merged": "reid",
    }
    return mapping.get(str(event_type or ""), str(event_type or "analytics"))


def _event_needs_snapshot(event_type: str | None) -> bool:
    return str(event_type or "") in {
        "intrusion",
        "loitering",
        "count",
        "absence",
        "face_recognized",
        "unauthorised",
    }


def refresh_cam_sid():
    """Map camera name -> backbone stream id from the backbone's /api/metrics (the
    camera name is inferred from the source path, e.g. NVR_ch9_...)."""
    try:
        with urllib.request.urlopen(BACKBONE_URL + "/api/metrics", timeout=3) as r:
            m = json.loads(r.read())
        for s in m.get("streams", []):
            sid = s.get("id")
            cam = s.get("camera")
            if not cam:
                mm = _re.search(r"(ch\d+)", str(s.get("source", "")))
                cam = mm.group(1) if mm else None
            if cam and sid is not None:
                CAM_SID[cam] = sid
    except Exception:
        pass


class LivePlatform:
    """A persistent plugin host + the running accumulators the UI reads.

    Only the identity plugin (Re-ID) runs. It binds each track to a global id; the UI
    draws those ids as boxes on the live video. No zone/analytics plugin is loaded --
    those are added only when the user selects a completed, configured use-case.
    """

    def __init__(self, gallery_kind: str, person_max_age_s: float = 600.0):
        self.gallery_kind = gallery_kind
        # bounded: drop persons unseen for this long so the store + snapshot stay cheap
        # (was 1e9 = never prune -> unbounded growth as gids churn).
        self.store = PersonStore(max_age_s=float(person_max_age_s))
        self.bus = EventBus()
        self.events: list = []      # identity events, as dicts
        self.bus.subscribe(EventBus.ALL, self._append_event)
        self._management_outbox = _ManagementOutbox.from_env()
        self.management_snapshot = None
        self._watch_alerted: dict[tuple[str, str, int], float] = {}
        self.bus.subscribe("unauthorised", self._note_watch_alert)
        # When each enrolled face was last recognised, for the roster. Stamped with
        # WALL time on arrival rather than the event's own `t`, which is the camera's
        # video clock. The gallery's own Vec.hits/last_hit cannot serve this: they are
        # in-memory only and are discarded whenever the gallery is reloaded from disk.
        self.face_last_seen: dict = {}
        self.bus.subscribe("face_recognized", self._note_face_seen)
        self.bus.subscribe("face_recognized", self._stabilize_face_gid)
        self.host = PluginHost(self.store, self.bus, [])
        self._cam_frame = {}   # camera -> (frame_idx, [ {bbox, gid, id} ])  latest frame
        self._cam_t = {}       # camera -> latest observation clock (for alert display TTL)
        # Engine's per-frame tracker boxes (set by the app when it owns the engine). The
        # observation stream is sparse by design -- a track only reports when it is
        # re-sync-due -- so it is the wrong source for overlay geometry. This is geometry
        # only; identity comes from _track_gid below, learned from the observations.
        self.box_state = None
        self._track_gid = {}   # (camera, track) -> (gid, last observation clock)
        # Live names are deliberately track-local.  A canonical gid can be contaminated
        # by a bad merge; if the UI borrowed IdentityLink.name from that gid, a different
        # visible person could inherit "Kiran".  Only show a name when THIS local track
        # recently produced fresh face evidence, then hold it briefly for display.
        self._track_name = {}  # (camera, track) -> (name, last verified observation clock)
        self.alert_display_s = float(os.environ.get("ALERT_DISPLAY_S", "30"))
        self.camera_wh = {}    # camera -> its own bbox/frame coordinate space
        self.camera_display_wh = {}  # camera -> JPEG coordinate space served to the UI
        self.gid_cam_obs = defaultdict(lambda: defaultdict(int))
        self.gid_cam_crop = defaultdict(dict)     # gid -> camera -> latest crop rel-path
        self.obs_base_dir = ""                    # where crop rel-paths resolve from
        self.detections = 0
        self.stream_name = ""
        # use-case layer: PER-CAMERA enablement. uc_cams[name] is None = ALL cameras,
        # a set = those cameras, empty/absent = off. Re-ID defaults ON everywhere but is a
        # use-case like any other (the user can stop it per camera or entirely).
        from PLATF.plugins.zones import zones_from_dict
        self.known_cameras: set = set()
        self.uc_cams: dict = {"reid": None}       # usecase name -> None(all)|set(cameras)
        # the engine's frame/bbox/foot-point coordinate space. Zones scale to THIS so the
        # point-in-poly test matches the foot-points, and the UI canvas uses THIS so the
        # overlay lines up with the video. Detected from a real frame at startup.
        self.frame_wh = (FRAME_W, FRAME_H)
        self.zones_raw: dict = {}                 # normalised zones (as the UI drew them)
        self.zones_cfg = zones_from_dict({"frame": list(self.frame_wh)})
        self.face_gallery = None                  # optional EnrollmentFaceGallery
        self._enroll_lock = threading.RLock()
        self.session = None                       # live EnrollmentSession, if any
        # Watchlist: enrolled name -> group ("unauthorised" | "authorised" | custom).
        # Kept OUT of the face gallery on purpose -- the gallery is the read-only
        # recognition store and gets rewritten wholesale by re-embedding, which would
        # take the tags with it. This lives in the platform's runtime config instead.
        self.face_groups: dict = {}
        # camera name -> stream URL, injected by the app. Enrollment opens its OWN
        # capture (clean pixels, measured yaw); the engine's frame has the overlay
        # drawn on it and its observations carry no pose.
        self.camera_source = None
        self.counting = None                      # CountingPlugin handle (for tallies)
        self.topo = self._load_topology()         # camera transition windows (mapper gate)
        # GBSL offline mapper stats (see run_mapper_pass)
        self.mapper_stats = {"on": True, "passes": 0, "merges": 0, "last_clusters": 0,
                             "before": 0, "after": 0, "updated": 0.0}
        try:
            from PLATF.plugins.enroll_gallery import EnrollmentGalleryAdapter

            g = EnrollmentGalleryAdapter.load(os.environ.get("FACE_GALLERY", "FACE/gallery"))
            if g is not None:
                self.face_gallery = g
                self.uc_cams["face"] = None
        except Exception:
            pass
        self._apply("reid")                       # bring up identity (all cameras) by default
        if self.face_gallery is not None:
            try:
                self._apply("face")
            except Exception:
                pass
        # Durable history last: if it cannot open, the live platform still runs.
        self.history = None
        self.search = None
        if os.environ.get("HISTORY", "1") == "1":
            try:
                from PLATF.history import History
                self.history = History(
                    os.environ.get("HISTORY_DIR",
                                   str(Path(__file__).resolve().parent / "history")),
                    # same learned windows the mapper gates on, so history can judge
                    # whether a cross-camera link was physically possible
                    topology=(self.topo.windows if self.topo is not None else None))
                self.bus.subscribe(EventBus.ALL, self._record_event)
                threading.Thread(target=self._history_loop, daemon=True).start()
                from PLATF.search import SemanticSearch
                self.search = SemanticSearch(self.history, self.face_gallery,
                                             live_lookup=self.live_camera_of)
                if os.environ.get("SEARCH_INDEX_ON_START", "1") == "1":
                    self.search.start()
                bf = self.history.backfill_crop_shapes()
                s = self.history.summary()
                print(f"[hist] {self.history.root} — {s['sightings']} sightings, "
                      f"{s['indexable']}/{s['with_crop']} crops indexable"
                      + (f"; back-filled {bf['checked']}, excluded {bf['now_excluded']}"
                         if bf["checked"] else ""), flush=True)
            except Exception as exc:
                print(f"[hist] disabled: {type(exc).__name__}: {exc}", flush=True)

    def _append_event(self, event) -> None:
        row = event.as_dict()
        row.setdefault("wall", time.time())
        self.events.append(row)
        if self._management_outbox is not None:
            self._management_outbox.write(self._management_row(row))

    def _management_row(self, row: dict) -> dict:
        payload = dict(row.get("payload") or {})
        event_type = row.get("type")
        camera = row.get("camera")
        if camera is None and row.get("person_id") is not None:
            try:
                person = self.store.get(self.store.canonical_gid(int(row["person_id"])))
                camera = person.current_camera if person is not None else None
            except Exception:
                camera = None
        snapshot_assets = {}
        snapshot_ref = row.get("snapshot_ref") or payload.get("snapshot_ref") or payload.get("crop")
        if (not snapshot_ref and camera and self.management_snapshot is not None
                and _event_needs_snapshot(event_type)):
            try:
                snapshot_result = self.management_snapshot(
                    str(camera), str(event_type or "event"), row
                )
                if isinstance(snapshot_result, dict):
                    snapshot_ref = snapshot_result.get("frame")
                    snapshot_assets = snapshot_result
                else:
                    snapshot_ref = snapshot_result
            except Exception:
                snapshot_ref = None
                snapshot_assets = {}
        out = {
            "event_type": event_type,
            "type": event_type,
            "app_id": _app_id_for_event(event_type),
            "camera_id": camera,
            "person_id": row.get("person_id"),
            "global_id": row.get("person_id"),
            "zone": row.get("zone"),
            "timestamp": row.get("t"),
            "payload": payload,
        }
        if snapshot_ref:
            out["snapshot_ref"] = str(snapshot_ref)
        if snapshot_assets:
            out["snapshot_refs"] = snapshot_assets
        return out

    def history_crop_path(self, sighting_id: int):
        """Absolute path of one sighting's stored evidence crop."""
        if self.history is None:
            return None
        try:
            with self.history._lock:
                row = self.history.db.execute(
                    "SELECT crop FROM sighting WHERE id=?", (int(sighting_id),)).fetchone()
        except Exception:
            return None
        if row is None or not row["crop"]:
            return None
        path = self.history.root / row["crop"]
        return str(path) if path.exists() else None

    def live_camera_of(self, canonical_gid: int, app_emb=None, fresh_s: float = 20.0):
        """Which camera this person is on RIGHT NOW, or None if they have left.

        Two ways to answer, because id equality alone is not enough. A global id
        survives roughly fifteen minutes on this deployment before the person is
        re-minted under a new one (measured: 2635 sightings across 265 identities).
        So a search hit on a crop from half an hour ago carries a DEAD id, and asking
        "is that id live" says no while the person stands in front of the camera.

        1. exact   the searched id is itself still active -> certain.
        2. likely  its stored appearance matches a live person within the pipeline's
                   calibrated body threshold -> report the camera, flagged as a
                   probable match rather than a fact.

        Returns (camera, how) where how is "id" | "appearance" | None.
        """
        now = time.monotonic()
        try:
            person = self.store.get(self.store.canonical_gid(int(canonical_gid)))
        except Exception:
            person = None
        if person is not None and now - float(
                getattr(person, "last_active_mono", -1e30)) <= fresh_s:
            return person.current_camera, "id"

        if app_emb is None:
            return None, None
        q = np.asarray(app_emb, np.float32).reshape(-1)
        qn = float(np.linalg.norm(q))
        if qn < 1e-6:
            return None, None
        q = q / qn
        thr = float(os.environ.get("LIVE_MATCH_THR", "0.145"))   # calibrated INT8 body
        best, best_d = None, 1e9
        for p in self.store.all():
            if now - float(getattr(p, "last_active_mono", -1e30)) > fresh_s:
                continue
            if not p.current_camera:
                continue
            for ex in getattr(p, "app_embs", []):
                v = np.asarray(ex.emb, np.float32).reshape(-1)
                vn = float(np.linalg.norm(v))
                if vn < 1e-6 or v.shape != q.shape:
                    continue
                d = 1.0 - float(q @ (v / vn))
                if d < best_d:
                    best_d, best = d, p.current_camera
        if best is not None and best_d <= thr:
            return best, "appearance"
        return None, None

    def _record_event(self, event) -> None:
        """Every published event, durably. `person_id` is the gid at emit time, so
        resolve the canonical id too -- a later merge re-points it either way."""
        if self.history is None:
            return
        try:
            canon = (self.store.canonical_gid(event.person_id)
                     if event.person_id is not None else None)
            self.history.add_event(event.as_dict(), canonical=canon)
            payload = getattr(event, "payload", None) or {}
            if (event.type == "identity" and payload.get("authoritative_remap")
                    and payload.get("from_gid") is not None and payload.get("to_gid") is not None):
                self.history.add_merge(int(payload["to_gid"]), int(payload["from_gid"]),
                                       reason="authoritative_backbone_gid")
        except Exception:
            pass

    def _note_watch_alert(self, event) -> None:
        payload = getattr(event, "payload", None) or {}
        name = str(payload.get("employee_id") or "").strip()
        camera = str(getattr(event, "camera", "") or "").strip()
        if not name or not camera or event.person_id is None:
            return
        try:
            gid = int(self.store.canonical_gid(int(event.person_id)))
        except Exception:
            return
        raw_gid = int(event.person_id)
        if gid != raw_gid:
            event.person_id = gid
            event.payload = dict(payload, stable_gid=gid, raw_gid=raw_gid)
        self._watch_alerted[(name.lower(), camera, gid)] = time.monotonic()

    def _watch_realert_s(self) -> float:
        raw = os.environ.get(
            "WATCHLIST_REALERT_S",
            os.environ.get("FACE_ACTIVITY_LEAVE_S",
                           os.environ.get("FACE_EVENT_COOLDOWN_S", "30")),
        )
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 30.0

    def _record_history(self, o, gid: int, crop: str | None) -> None:
        """Fold one observation into the durable sighting log.

        Runs AFTER the plugin host, so any name the face plugin just attached is
        already on the Person and lands on this visit. Never allowed to break
        ingest: history is a recorder, not a dependency of the live pipeline.
        """
        if self.history is None:
            return
        try:
            name, dist = self._fresh_identity_for_observation(o, gid)
            self.history.observe(
                gid=gid, camera=o.camera, t=float(o.t), quality=float(o.quality or 0.0),
                crop_abs=(os.path.join(self.obs_base_dir, crop) if crop else None),
                app_emb=o.app_emb, face_emb=o.face_emb, name=name, face_dist=dist,
                face_meta=(o.meta or {}).get("face_meta"), bbox=o.bbox)
            # Face-recognition events are cooldown-gated, but history sees every fresh
            # named observation. Use those strong named observations to stabilize gid
            # churn too; otherwise a person can be recognized as Kiran under P69, P78,
            # P89, P116 while no second face_recognized event fires to merge them.
            if name and dist is not None:
                self._stabilize_named_gid(str(name), int(gid), float(dist),
                                          float(o.t), reason="face_name_history")
        except Exception:
            pass

    def _emit_watchlist_for_fresh_identity(self, o, gid: int) -> None:
        """Raise a watchlist alert for every fresh named gid, even after canonicalization.

        FacePlugin suppresses repeated arrivals by (name, camera), which is correct for
        a continuous local track. The engine can later face-confirm a new raw/canonical
        gid as the same flagged name without publishing a second face_recognized event.
        Those fresh named observations still need the unauthorised event that drives the
        phone-call dispatcher.
        """
        name, dist = self._fresh_identity_for_observation(o, gid)
        if not name or dist is None:
            return
        group = str(self.face_groups.get(str(name), "") or "").strip().lower()
        if group != "unauthorised":
            return
        if float(dist) > float(os.environ.get("FACE_ALERT_THR",
                                              os.environ.get("FACE_THR", "0.45"))):
            return
        try:
            canon = int(self.store.canonical_gid(int(gid)))
        except Exception:
            canon = int(gid)
        key = (str(name).lower(), str(o.camera), canon)
        now = time.monotonic()
        last = self._watch_alerted.get(key)
        if last is not None and now - float(last) < self._watch_realert_s():
            return
        self._watch_alerted[key] = now
        if len(self._watch_alerted) > 4096:
            keep_after = now - max(self._watch_realert_s(), 30.0) * 2.0
            self._watch_alerted = {k: v for k, v in self._watch_alerted.items()
                                   if float(v) >= keep_after}
        self.bus.publish(Event("unauthorised", float(o.t), o.camera, canon,
                               payload={"employee_id": str(name), "group": group,
                                        "dist": round(float(dist), 3)}))

    def _fresh_identity_for_observation(self, o, gid: int):
        """Return (name, distance) only if this observation just verified the face.

        IdentityLink can persist on a Person after the face disappears, and merges can
        move unrelated tracks onto a named canonical gid.  For live display/history,
        stale gid-level identity is not enough; the local observation clock must match
        the face plugin's verification timestamp.
        """
        person = self.store.get(self.store.canonical_gid(gid))
        if person is None:
            return None, None
        link = self.store.identity.get(person.person_uuid)
        if link is None or not link.name:
            return None, None
        fresh_s = float(os.environ.get("FACE_HISTORY_FRESH_S", "0.25"))
        try:
            fresh = abs(float(link.last_verified) - float(o.t)) <= fresh_s
        except Exception:
            fresh = False
        if not fresh:
            return None, None
        # IdentityLink stores confidence = 1 - distance (plugins/face.py)
        return str(link.name), round(1.0 - float(link.confidence), 3)

    def _usecase_enabled_for(self, name: str, camera: str) -> bool:
        scope = self.uc_cams.get(str(name), set())
        return scope is None or str(camera) in set(scope or ())

    def _face_observation_ok(self, o) -> bool:
        if not o.has_face():
            return False
        meta = (o.meta or {}).get("face_meta")
        if not meta:
            return os.environ.get("FACE_REQUIRE_META", "0") != "1"
        try:
            det = float(meta.get("det") or 0.0)
            px = float(meta.get("w") or 0.0)
        except (TypeError, ValueError):
            return os.environ.get("FACE_REQUIRE_META", "0") != "1"
        return (det >= float(os.environ.get("FACE_MIN_DET", "0.65"))
                and px >= float(os.environ.get("FACE_MIN_PX", "40")))

    def _existing_named_gid(self, name: str) -> int | None:
        for p in self.store.all():
            link = self.store.identity.get(p.person_uuid)
            if link is not None and str(link.name) == str(name):
                return int(p.global_id)
        return None

    def _bootstrap_face_gid(self, o) -> int | None:
        """Bind a grey track immediately when its own face strongly matches enrollment.

        Re-ID is allowed to leave a track unassigned while waiting for body/gait evidence.
        That is correct for weak/no-face boxes, but wrong for a large frontal enrolled
        face: FacePlugin cannot run because it receives `person=None`. This creates or
        reuses the named Person before plugins run, then FacePlugin emits the normal
        recognition event on the same observation.
        """
        if (self.face_gallery is None or not self._usecase_enabled_for("face", o.camera)
                or self.store.resolve_local(o.camera, o.local_id) is not None
                or not self._face_observation_ok(o)):
            return None
        try:
            res = self.face_gallery.search(o.face_emb, k=1)
        except Exception:
            return None
        if not res:
            return None
        name, dist = res[0]
        try:
            dist = float(dist)
        except (TypeError, ValueError):
            return None
        if dist > float(os.environ.get("FACE_THR", "0.45")):
            return None
        gid = (self._best_verified_gid_for_name(str(name))
               or self._existing_named_gid(str(name)))
        if gid is None:
            p = self.store.mint(float(o.t))
            gid = int(p.global_id)
        p = self.store.get_or_create(int(gid), float(o.t))
        self.store.bind_local(o.camera, o.local_id, int(p.global_id))
        self.store.identity.link(
            p.person_uuid,
            IdentityLink(employee_id=str(name), name=str(name),
                         confidence=round(1.0 - dist, 3), source="face_bootstrap",
                         last_verified=float(o.t)))
        return int(p.global_id)

    def _update_track_name(self, o, gid: int) -> None:
        key = (o.camera, int(o.local_id))
        hold_s = float(os.environ.get("NAME_HOLD_S", "8"))
        name, _dist = self._fresh_identity_for_observation(o, gid)
        if name:
            self._track_name[key] = (name, float(o.t))
            return
        old = self._track_name.get(key)
        if old and float(o.t) - float(old[1]) <= hold_s:
            return
        self._track_name.pop(key, None)

    def _history_loop(self, every_s: float = 10.0) -> None:
        """Close quiet visits, snapshot the re-id memory, and prune on a slow cadence."""
        last_prune = last_ex = 0.0
        while True:
            time.sleep(every_s)
            try:
                self.history.flush()
                now = time.time()
                # Snapshot exemplars rather than writing on every change: the set
                # churns constantly, and only the CURRENT contents matter.
                if now - last_ex > float(os.environ.get("EXEMPLAR_SAVE_S", "30")):
                    last_ex = now
                    self._save_exemplars()
                if now - last_prune > 3600.0:
                    last_prune = now
                    self.history.prune()
            except Exception:
                pass

    def _save_exemplars(self) -> int:
        """Persist the rolling re-id memory of every person currently in the store."""
        n = 0
        for p in self.store.all():
            for modality in ("app", "face", "gait"):
                bucket = getattr(p, f"{modality}_embs", None)
                if not bucket:
                    continue
                try:
                    n += self.history.save_exemplars(p.global_id, modality, bucket,
                                                     crop_root=self.obs_base_dir)
                except Exception:
                    pass
        return n

    def ingest(self, obs_list: list):
        if not obs_list:
            return
        by_frame = defaultdict(list)
        for o in obs_list:
            wh = (o.meta or {}).get("frame_wh")
            if wh and len(wh) == 2:
                self.camera_wh[o.camera] = (int(wh[0]), int(wh[1]))
            dwh = (o.meta or {}).get("display_wh")
            if dwh and len(dwh) == 2:
                self.camera_display_wh[o.camera] = (int(dwh[0]), int(dwh[1]))
            self._cam_t[o.camera] = float(o.t)
            if o.gid is None:
                self._bootstrap_face_gid(o)
            by_frame[o.frame_idx].append(o)
        for fr in sorted(by_frame):
            batch = by_frame[fr]
            self.host.process(batch)
            for o in batch:
                gid = self.store.resolve_local(o.camera, o.local_id)
                if gid is None:
                    continue
                self.gid_cam_obs[gid][o.camera] += 1
                crop = (o.meta or {}).get("crop")
                if crop:
                    self.gid_cam_crop[gid][o.camera] = crop
                self._record_history(o, gid, crop)
                self._emit_watchlist_for_fresh_identity(o, gid)
                self._update_track_name(o, gid)
                # latest-frame boxes per camera, for the live video overlay
                cur, boxes = self._cam_frame.get(o.camera, (-1, []))
                if o.frame_idx != cur:
                    cur, boxes = o.frame_idx, []
                    self._cam_frame[o.camera] = (cur, boxes)
                boxes.append({"bbox": [round(float(c), 1) for c in o.bbox],
                              "gid": gid, "id": (o.meta or {}).get("raw_lid"),
                              "track": int(o.local_id)})
                # remember which gid this track carries, so the per-frame boxes the
                # engine publishes between syncs can still be labelled.
                self._track_gid[(o.camera, int(o.local_id))] = (gid, float(o.t))
        if len(self._track_gid) > 4096:
            keep = sorted(self._track_gid.items(), key=lambda kv: kv[1][1])[-2048:]
            self._track_gid = dict(keep)
            self._track_name = {k: v for k, v in self._track_name.items()
                                if k in self._track_gid}
        self.detections += len(obs_list)

    def live_view(self):
        """The Live overlay: per camera the latest re-id boxes + ids, the user's zones/
        lines (px), and recent use-case alerts (so the tile can draw them on top of the
        engine's annotated frame)."""
        from PLATF.plugins.zones import zones_for
        w, h = self.frame_wh
        # Recent analytics alerts per camera. Prefer wall time when present: live engine
        # video clocks can reset on process/stream restart, while operators still expect
        # the alert banner to remain visible for ALERT_DISPLAY_S real seconds.
        recent = defaultdict(list)
        wall_now = time.time()
        for e in self.events[-200:]:
            if e.get("type") in ("intrusion", "loitering", "absence", "unauthorised"):
                cam = e.get("camera")
                if e.get("wall") is not None:
                    try:
                        if wall_now - float(e.get("wall")) > self.alert_display_s:
                            continue
                    except (TypeError, ValueError):
                        pass
                else:
                    now = self._cam_t.get(cam)
                    if now is not None and now - float(e.get("t", now)) > self.alert_display_s:
                        continue
                raw_gid = e.get("person_id")
                gid = self.store.canonical_gid(raw_gid) if raw_gid is not None else None
                p = self.store.get(gid) if gid is not None else None
                link = self.store.identity.get(p.person_uuid) if p is not None else None
                who = str((e.get("payload") or {}).get("who", ""))
                track = None
                prefix = f"{cam}:"
                if who.startswith(prefix):
                    try:
                        track = int(who[len(prefix):])
                    except ValueError:
                        pass
                if e["type"] == "absence":
                    label = f"zone {e.get('zone') or ''} empty"
                elif e["type"] == "unauthorised":
                    # Name the person: the whole point of a watchlist hit is WHO it is,
                    # and the payload carries the matched enrollment name even if the
                    # IdentityLink has since been cleared.
                    emp = (e.get("payload") or {}).get("employee_id")
                    label = f"unauthorised · {emp or (link.name if link else 'unknown')}"
                else:
                    label = link.name if link and link.name else (
                        f"P{gid}" if gid is not None else (who or "unknown person"))
                recent[cam].append({"type": e["type"], "zone": e.get("zone"),
                                    "t": e.get("t"), "gid": gid, "track": track,
                                    "name": link.name if link else None, "label": label})
        # Overlay geometry: prefer the engine's per-frame tracker boxes, which cover every
        # live track. Fall back to the observation boxes when the engine is not publishing
        # (standalone/replay ingest), so this degrades to the old behaviour rather than to
        # an empty wall. A track with no gid yet draws an unlabelled box -- honest about
        # "seen but not identified" rather than borrowing a neighbour's id.
        live_boxes = {}
        if self.box_state is not None:
            try:
                snap = dict(self.box_state)
            except Exception:
                snap = {}
            for cam, st in snap.items():
                wh = (st or {}).get("wh")
                if wh and len(wh) == 2:
                    self.camera_wh.setdefault(cam, (int(wh[0]), int(wh[1])))
                live_boxes[cam] = (int((st or {}).get("frame", -1)),
                                   [{"bbox": b.get("bbox"), "track": b.get("track"),
                                     "id": b.get("track"),
                                     "gid": self._track_gid.get((cam, int(b.get("track", -1))),
                                                                (None, 0.0))[0]}
                                    for b in ((st or {}).get("boxes") or [])])
        cams = {}
        for cam, (fr, boxes) in (live_boxes or self._cam_frame).items():
            z = zones_for(self.zones_cfg, cam)

            def named_box(b):
                raw_gid = b.get("gid")
                cg = self.store.canonical_gid(raw_gid) if raw_gid is not None else None
                track = int(b.get("track", -1))
                tn = self._track_name.get((cam, track))
                now_t = self._cam_t.get(cam, 0.0)
                hold_s = float(os.environ.get("NAME_HOLD_S", "8"))
                name = tn[0] if tn and now_t - float(tn[1]) <= hold_s else None
                bb = list(b.get("bbox") or [])
                src = self.camera_wh.get(cam)
                dst = self.camera_display_wh.get(cam)
                if len(bb) == 4 and src and dst and src[0] and src[1]:
                    sx, sy = dst[0] / src[0], dst[1] / src[1]
                    bb = [round(bb[0] * sx, 1), round(bb[1] * sy, 1),
                          round(bb[2] * sx, 1), round(bb[3] * sy, 1)]
                return {**b, "bbox": bb, "gid": cg, "name": name}

            out_boxes = [named_box(b) for b in boxes]
            cams[cam] = {
                "frame": fr, "frame_wh": list(self.camera_display_wh.get(
                    cam, self.camera_wh.get(cam, self.frame_wh))),
                "boxes": out_boxes,
                "zones": [{"name": zz["name"], "kind": zz["kind"],
                           "poly": [[round(x, 1), round(y, 1)] for x, y in zz["poly"]]}
                          for zz in z["zones"]],
                "lines": [{"name": ll["name"], "a": [round(ll["a"][0], 1), round(ll["a"][1], 1)],
                           "b": [round(ll["b"][0], 1), round(ll["b"][1], 1)]} for ll in z["lines"]],
                "alerts": recent.get(cam, [])[-6:],
            }
        return {"frame": [w, h], "cameras": cams}

    def face_gallery_status(self, reload: bool = False, detail: bool = False) -> dict:
        """Report the enrollment gallery and optionally load a new atomic snapshot."""
        if self.face_gallery is None:
            return {"loaded": False, "people": [], "person_count": 0, "vectors": 0,
                    "roster": []}
        changed = False
        error = None
        try:
            changed = self.face_gallery.reload_if_changed(force=reload)
        except Exception as exc:
            error = str(exc)
        out = self.face_gallery.status()
        out.update({"changed": changed, "error": error,
                    "groups": dict(self.face_groups)})
        if detail:
            out["roster"] = self.face_roster()
        return out

    def _note_face_seen(self, event) -> None:
        emp = (event.payload or {}).get("employee_id")
        if emp:
            self.face_last_seen[str(emp)] = {"t": time.time(),
                                             "camera": event.camera}

    def _best_verified_gid_for_name(self, name: str) -> int | None:
        """Stable numeric id for an enrolled person, chosen from face-confirmed history.

        The live engine can mint transient ids (198 -> 218 -> 203) while the face plugin
        correctly says "this is Kiran". Operator-facing identity should not drift once a
        strong historical face anchor exists. Pick the gid with the best historical face
        distance under a strict bar; never use body-only/name-only rows.
        """
        if self.history is None or not name:
            return None
        max_dist = float(os.environ.get("FACE_STABLE_ANCHOR_MAX_DIST", "0.35"))
        min_hits = int(os.environ.get("FACE_STABLE_ANCHOR_MIN_HITS", "1"))
        try:
            with self.history._lock:
                row = self.history.db.execute(
                    "SELECT canonical_gid gid, MIN(face_dist) best, COUNT(*) n "
                    "FROM sighting WHERE name=? AND face_dist IS NOT NULL "
                    "AND face_dist<=? GROUP BY canonical_gid HAVING n>=? "
                    "ORDER BY best ASC, n DESC, MAX(wall_start) ASC LIMIT 1",
                    (str(name), max_dist, min_hits)).fetchone()
            return int(row["gid"]) if row is not None and row["gid"] is not None else None
        except Exception:
            return None

    def _fold_platform_gid_maps(self, keep: int, drop: int) -> None:
        """Keep live counters/crop maps aligned after a non-mapper merge."""
        keep, drop = int(keep), int(drop)
        for cam, n in dict(self.gid_cam_obs.pop(drop, {})).items():
            self.gid_cam_obs[keep][cam] += n
        for cam, rel in dict(self.gid_cam_crop.pop(drop, {})).items():
            self.gid_cam_crop[keep].setdefault(cam, rel)

    def _stabilize_face_gid(self, event) -> None:
        """Fresh enrolled-face recognition may stabilize a drifting gid.

        FacePlugin deliberately names a Person without changing its gid. That keeps
        recognition separate from Re-ID, but it leaves known people with new ids on every
        return after a restart. Here, only after a fresh face_recognized event, fold the
        current gid into the best verified historical gid for that same enrolled name.
        Body/gait never trigger this path.
        """
        name = (event.payload or {}).get("employee_id")
        if not name or event.person_id is None:
            return
        try:
            dist = float((event.payload or {}).get("dist", 999.0))
        except Exception:
            dist = 999.0
        if dist > float(os.environ.get("FACE_STABLE_EVENT_MAX_DIST", "0.45")):
            return
        out = self._stabilize_named_gid(str(name), int(event.person_id), dist,
                                        float(event.t or time.time()),
                                        reason=f"face_name_stable:{name}")
        if out is None:
            return
        anchor, cur = out
        event.person_id = int(anchor)
        event.payload = dict(event.payload or {}, stable_gid=int(anchor),
                             merged_gid=int(cur))
        self.bus.publish(Event("person_merged", time.time(), None, int(anchor),
                               payload={"dropped": int(cur),
                                        "reason": f"face_name_stable:{name}"}))

    def _stabilize_named_gid(self, name: str, gid: int, dist: float, t: float,
                             reason: str = "face_name_history") -> tuple[int, int] | None:
        """Fold a fresh named gid into the best verified gid for that same enrolled name."""
        if not name or gid is None:
            return None
        try:
            dist = float(dist)
        except Exception:
            return None
        if dist > float(os.environ.get("FACE_STABLE_EVENT_MAX_DIST", "0.45")):
            return None
        cur = self.store.canonical_gid(int(gid))
        anchor = self._best_verified_gid_for_name(str(name))
        if anchor is None or int(anchor) == int(cur):
            return None
        if self.history is not None:
            verdict = self.history.add_merge(int(anchor), int(cur), reason=str(reason))
            if not verdict.get("applied", False):
                return None
        self.store.get_or_create(int(anchor), float(t or time.time()))
        self.store.merge(int(anchor), int(cur))
        self._fold_platform_gid_maps(int(anchor), int(cur))
        return int(anchor), int(cur)

    def face_roster(self) -> list:
        """Who is enrolled, what was stored for them, and their watchlist group --
        the gallery as something you can SEE, not a count. Chips are addressed by
        index so the UI can render the actual stored faces."""
        if self.face_gallery is None:
            return []
        gal = self.face_gallery.gallery
        out = []
        for person in gal.people():
            vecs = gal.of(person)
            cov = self.face_gallery.coverage(person)
            seen = self.face_last_seen.get(person) or {}
            out.append({
                "name": person,
                "group": str(self.face_groups.get(person, "authorised")),
                "vectors": len(vecs),
                "coverage": cov,
                # how many pose bins are still empty -- the "worth re-enrolling" signal
                "missing": sum(1 for c in cov.values() if not c["vectors"]),
                "sources": sorted({v.source for v in vecs}),
                "chips": [i for i, v in enumerate(vecs) if v.chip_path],
                "last_seen": round(float(seen.get("t") or 0.0), 1),
                "last_camera": seen.get("camera"),
                "last_hit": round(max((v.last_hit for v in vecs), default=0.0), 1),
                "hits": sum(int(v.hits) for v in vecs),
            })
        out.sort(key=lambda r: (r["group"] != "unauthorised", r["name"].lower()))
        return out

    def face_chip_path(self, person: str, index: int):
        """Absolute path of one stored chip, for the gallery board."""
        if self.face_gallery is None:
            return None
        vecs = self.face_gallery.gallery.of(str(person))
        if not 0 <= int(index) < len(vecs):
            return None
        rel = vecs[int(index)].chip_path
        if not rel:
            return None
        path = os.path.join(str(self.face_gallery.root), rel)
        return path if os.path.exists(path) else None

    def set_face_group(self, name: str, group: str) -> dict:
        """Tag one enrolled person. 'authorised' (or empty) clears the tag.

        The FacePlugin holds this dict by reference, so the change is live on the
        next recognition -- no plugin rebuild, no re-alerting whoever is on screen.
        """
        name = str(name).strip()
        if not name:
            raise ValueError("name is required")
        group = str(group or "").strip().lower() or "authorised"
        if group == "authorised":
            self.face_groups.pop(name, None)
        else:
            self.face_groups[name] = group
        return {"name": name, "group": group, "groups": dict(self.face_groups)}

    def delete_face_person(self, name: str) -> dict:
        """Delete one enrollment and its watchlist label from persistent state."""
        if self.face_gallery is None:
            raise ValueError("face enrollment gallery is not loaded")
        out = self.face_gallery.delete_person(name)
        self.face_groups.pop(str(out["deleted"]), None)
        out["groups"] = dict(self.face_groups)
        return out

    def start_enrollment(self, name: str, camera: str) -> dict:
        """Open a guided session: own capture, measured yaw, Gallery.consider().

        Replaces the old flow, which averaged 5 face embeddings off a track and
        filed them all as "frontal" because the engine publishes no pose.
        """
        from PLATF.enrollment import EnrollmentSession

        name, camera = str(name).strip(), str(camera).strip()
        if not name or not camera:
            raise ValueError("name and camera are required")
        if self.face_gallery is None:
            raise ValueError("face enrollment gallery is not loaded")
        source = self.camera_source(camera) if self.camera_source else None
        if not source:
            raise ValueError(f"no stream source for camera {camera}")
        with self._enroll_lock:
            if self.session is not None and self.session.state in ("loading", "capturing"):
                raise ValueError(f"already enrolling {self.session.name}")
            self.session = EnrollmentSession(self.face_gallery, name, camera, source)
        return self.enrollment_status()

    def enrollment_status(self) -> dict:
        with self._enroll_lock:
            sess = self.session
        if sess is None:
            return {"state": "idle", "name": "", "camera": "", "coverage": {},
                    "missing": [], "tip": "", "captured": 0, "live": {}, "log": [],
                    "has_preview": False, "can_save": False, "can_retake": False}
        out = sess.status()
        # Saving is only meaningful once at least one vector exists; the frontal bin
        # is the one view every identity must have, so gate the finish on it.
        cov = out.get("coverage") or {}
        out["can_save"] = bool(cov.get("frontal", {}).get("vectors"))
        out["can_retake"] = out["state"] in ("loading", "capturing")
        return out

    def enrollment_preview_path(self):
        """Absolute path of the most recently STORED chip (not a person crop)."""
        with self._enroll_lock:
            sess = self.session
        rel = getattr(sess, "last_chip", None) if sess else None
        if not rel:
            return None
        path = os.path.join(str(self.face_gallery.root), rel)
        return path if os.path.exists(path) else None

    def cancel_enrollment(self) -> dict:
        """Stop and UNDO -- vectors are persisted as they are accepted, so a cancel
        has to remove this session's writes rather than drop an unsaved buffer."""
        with self._enroll_lock:
            sess = self.session
            self.session = None
        if sess is not None:
            sess.stop()
            sess.rollback()
        return self.enrollment_status()

    def retake_enrollment(self) -> dict:
        """Discard what this session stored and keep capturing under the same name."""
        with self._enroll_lock:
            sess = self.session
        if sess is None:
            raise ValueError("no enrollment session")
        sess.rollback()
        return self.enrollment_status()

    def save_enrollment(self) -> dict:
        """Finish the session. The vectors are already on disk; this closes the
        capture, reports what was stored, and announces the new identity.

        No IdentityLink is attached here on purpose: the enrolled face now lives in
        the read-only recognition gallery, so FacePlugin will bind the name to
        whichever Person it recognises next. That keeps identity (who is this track)
        and recognition (whose face is this) as the two separate galleries the
        platform is built around, instead of hard-wiring a name to one gid.
        """
        with self._enroll_lock:
            sess = self.session
        if sess is None:
            raise ValueError("no enrollment session")
        cov = self.face_gallery.coverage(sess.name)
        if not cov.get("frontal", {}).get("vectors"):
            raise ValueError("need at least one frontal capture before saving")
        sess.stop()
        stored = sum(c["vectors"] for c in cov.values())
        self.bus.publish(Event("face_enrolled", time.time(), sess.camera, None,
                               payload={"name": sess.name, "vectors": stored,
                                        "coverage": cov}))
        out = {**self.face_gallery.status(), "enrolled": sess.name,
               "samples": len(sess.added), "coverage": cov}
        with self._enroll_lock:
            self.session = None
        return out

    def audit(self):
        """Cross-camera crop board data: every gid seen in >1 camera, with its per-camera
        observation counts + which cameras have a crop. The UI shows the crops side by side
        so a false cross-cam merge (one gid = different people) is caught BY EYE -- the
        provable-merge metric only sees same-frame merges, never cross-cam smears."""
        out = []
        for gid, cams in self.gid_cam_obs.items():
            if len(cams) < 2:
                continue
            crops = self.gid_cam_crop.get(gid, {})
            out.append({"gid": gid, "cameras": sorted(cams.keys()),
                        "per_cam": {c: cams[c] for c in cams},
                        "crop_cams": sorted(crops.keys()),
                        "n_obs": sum(cams.values())})
        out.sort(key=lambda r: -r["n_obs"])   # most-observed (most-confident) ids first
        return out

    def crop_path(self, gid: int, camera: str):
        rel = self.gid_cam_crop.get(int(gid), {}).get(camera)
        if not rel:
            return None
        path = os.path.join(self.obs_base_dir, rel)
        return path if os.path.exists(path) else None

    # --- use-case toggling (PER CAMERA) + zone config (runtime) ----------------
    def set_cameras(self, cams):
        """Register the running cameras (so per-camera disables can materialise 'all')."""
        self.known_cameras = set(cams)
        for p in getattr(self.host, "plugins", []):
            if hasattr(p, "live_cameras"):
                p.live_cameras = set(self.known_cameras)

    def set_frame_size(self, w, h):
        """Set the real engine coordinate space; re-scale zones + re-apply plugins so the
        point-in-poly test and the UI overlay both line up with the video."""
        self.frame_wh = (int(w), int(h))
        self.set_zones(self.zones_raw)

    def _build_plugin(self, name):
        from PLATF.plugins.analytics import CountingPlugin, IntrusionPlugin, LoiteringPlugin
        from PLATF.plugins.absence import AbsencePlugin
        from PLATF.plugins.face import FacePlugin
        if name == "reid":
            return ReIDPlugin(build_gallery(self.gallery_kind))
        if name == "intrusion":
            return IntrusionPlugin(self.zones_cfg)
        if name == "loitering":
            return LoiteringPlugin(self.zones_cfg)
        if name == "counting":
            self.counting = CountingPlugin(self.zones_cfg)
            return self.counting
        if name == "absence":
            return AbsencePlugin(self.zones_cfg)
        if name == "face":
            return FacePlugin(self.face_gallery, groups=self.face_groups)
        return None

    def _apply(self, name):
        """Reconcile the host with uc_cams[name]. None => all cameras; a non-empty set =>
        those cameras; absent/empty => remove the plugin."""
        scope = self.uc_cams.get(name, set())          # None (all) | set
        self.host.remove_plugin(name)
        if name == "counting" and not scope and scope is not None:
            self.counting = None
        if scope is None or len(scope) > 0:
            p = self._build_plugin(name)
            if p is None:
                return False
            p.cameras = None if scope is None else set(scope)   # host enforces per camera
            if hasattr(p, "live_cameras"):
                p.live_cameras = set(self.known_cameras)
            self.host.add_plugin(p, first=(name == "reid"))
        return True

    def enable_usecase(self, name: str, camera: str = None):
        cur = self.uc_cams.get(name, set())
        if camera is None:
            self.uc_cams[name] = None                  # all cameras
        elif cur is None:
            pass                                        # already all
        else:
            self.uc_cams[name] = set(cur) | {camera}
        return self._apply(name)

    def disable_usecase(self, name: str, camera: str = None):
        cur = self.uc_cams.get(name, set())
        if camera is None:
            self.uc_cams[name] = set()                  # off everywhere
        elif cur is None:                               # was all -> materialise minus one
            self.uc_cams[name] = set(self.known_cameras) - {camera}
        else:
            self.uc_cams[name] = set(cur) - {camera}
        return self._apply(name)

    def set_zones(self, raw: dict):
        """Apply zones/lines drawn in the UI (normalised). Rebuilds any active zone-driven
        plugin so it sees the new geometry (preserving its per-camera scope)."""
        from PLATF.plugins.zones import zones_from_dict
        self.zones_raw = raw or {}
        cfg = dict(self.zones_raw)
        cfg["frame"] = list(self.frame_wh)       # scale to the REAL coord space, not the UI's guess
        self.zones_cfg = zones_from_dict(cfg)
        for uc in ("intrusion", "loitering", "counting", "absence"):
            if self.uc_cams.get(uc):
                self._apply(uc)

    @staticmethod
    def _load_topology():
        """Learned per-pair transition windows (MTMC/reports/learned_transitions.json,
        'chA|chB' -> {min_s,max_s}) as a CameraTopology, for the cross-cam mapper gate."""
        try:
            from PLATF.core.schemas import CameraTopology
            path = os.environ.get("LEARNED_TRANSITIONS", "MTMC/reports/learned_transitions.json")
            lt = json.load(open(path, encoding="utf-8"))
            w = {}
            for k, v in lt.items():
                if "|" in k and isinstance(v, dict):
                    a, b = k.split("|", 1)
                    w[(a.strip(), b.strip())] = (float(v.get("min_s", 0.0)),
                                                 float(v.get("max_s", 1e9)))
            return CameraTopology(windows=w)
        except Exception:
            return None

    # --- GBSL offline mapper (restore ids by re-clustering the Person store) ----
    def run_mapper_pass(self):
        """One offline pass: cluster the accumulated Persons + merge duplicates into their
        survivor (also folds the platform-side obs/crop accumulators). The merge re-points
        future obs of the dropped gids, so the collapse persists + ids stabilise."""
        from PLATF.offline_mapper import _min_dist, cluster
        app_thr = float(os.environ.get("MAP_APP_THR", "0.20"))
        # cross-camera merging via the OFFLINE recipe: appearance + the torso COLOUR GATE
        # (the offline precision key) + topology. This is how the offline pipeline mapped
        # people across cameras on faceless footage; the online backbone can't (greedy), so
        # the SIDE mapper does it. cross_thr<=0 disables cross-cam.
        cross_thr = float(os.environ.get("MAP_CROSS_THR", "0.14"))
        colour_gate = float(os.environ.get("MAP_COLOUR_GATE", "0.45"))
        # cameras that TRULY share floor (co-located) -> allowed to be seen at once; every
        # other cross-cam pair is gated by concurrent-exclusion + learned transition window.
        overlapping = []
        for pair in os.environ.get("MAP_OVERLAP_PAIRS", "ch9-ch10").split(","):
            ab = pair.strip().split("-")
            if len(ab) == 2 and ab[0] and ab[1]:
                overlapping.append((ab[0].strip(), ab[1].strip()))
        min_cross_obs = int(os.environ.get("MAP_CROSS_MIN_OBS", "5"))
        # off-chain cameras (not in the ch1-ch2-ch9-ch10 chain) never cross-link
        exclude = [c.strip() for c in os.environ.get("MAP_CROSS_EXCLUDE", "ch16").split(",") if c.strip()]
        # FACE-only cross-cam: a STRONG face match links across cameras regardless of clothing
        # (the reliable non-overlap signal). Set MAP_FACE_MATCH_THR>0 + MAP_CROSS_THR=0 to run
        # face-only cross-cam (appearance cross-cam off -> no uniform false-merges).
        face_match_thr = float(os.environ.get("MAP_FACE_MATCH_THR", "0.0"))
        persons = self.store.all()
        log_path = os.environ.get("MAPPER_LOG")
        pid = {p.global_id: p for p in persons}
        before = len(persons)
        clusters = cluster(persons, app_thr=app_thr, cross_thr=cross_thr,
                           colour_gate=colour_gate, topo=self.topo,
                           overlapping=overlapping, min_cross_obs=min_cross_obs,
                           exclude=exclude, face_match_thr=face_match_thr)
        # The engine is authoritative for within-camera fragmentation repair. The
        # side mapper must never collapse a cluster containing two members that have
        # both appeared in the same camera: they may be co-visible right now, and a
        # canonical store merge is irreversible for the lifetime of this process.
        # Keep only one-member-per-camera clusters here; cross-camera face mapping
        # remains available while same-camera look-alike merges are impossible.
        safe_clusters = []
        for c in clusters:
            members = [pid[g] for g in c["gids"] if g in pid]
            member_cams = [set(getattr(m, "cameras_seen", []) or []) for m in members]
            if any(member_cams[i] & member_cams[j]
                   for i in range(len(member_cams))
                   for j in range(i + 1, len(member_cams))):
                continue
            safe_clusters.append(c)
        clusters = safe_clusters
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    for c in clusters:
                        members = [pid[g] for g in c["gids"] if g in pid]
                        cams = sorted({
                            cam
                            for m in members
                            for cam in (getattr(m, "cameras_seen", []) or [])
                        })
                        cross_cam = len(cams) > 1
                        min_face_d = None
                        min_app_d = None
                        for i, a in enumerate(members):
                            for b in members[i + 1:]:
                                a_faces = [e.emb for e in a.face_embs]
                                b_faces = [e.emb for e in b.face_embs]
                                if a_faces and b_faces:
                                    d = _min_dist(a_faces, b_faces)
                                    min_face_d = d if min_face_d is None else min(min_face_d, d)
                                a_apps = [e.emb for e in a.app_embs]
                                b_apps = [e.emb for e in b.app_embs]
                                if a_apps and b_apps:
                                    d = _min_dist(a_apps, b_apps)
                                    min_app_d = d if min_app_d is None else min(min_app_d, d)
                        line = {
                            "t": round(time.time(), 1),
                            "survivor": c["survivor"],
                            "gids": c["gids"],
                            "cameras": cams,
                            "cross_cam": cross_cam,
                            "min_face_d": round(min_face_d, 4) if min_face_d is not None else None,
                            "min_app_d": round(min_app_d, 4) if min_app_d is not None else None,
                        }
                        f.write(json.dumps(line) + "\n")
            except Exception:
                pass
        merges = 0
        for c in clusters:
            surv = c["survivor"]
            for g in c["gids"]:
                if g == surv:
                    continue
                for cam, n in dict(self.gid_cam_obs.pop(g, {})).items():
                    self.gid_cam_obs[surv][cam] += n
                for cam, rel in dict(self.gid_cam_crop.pop(g, {})).items():
                    self.gid_cam_crop[surv].setdefault(cam, rel)
                self.store.merge(surv, g)
                if self.history is not None:
                    # re-point the dropped id's past sightings onto the survivor,
                    # so the mapper's cross-camera link shows up as ONE path
                    try:
                        self.history.add_merge(surv, g, reason="mapper")
                    except Exception:
                        pass
                merges += 1
                self.bus.publish(Event("person_merged", time.time(), None, surv,
                                       payload={"dropped": g}))
        self.mapper_stats.update(passes=self.mapper_stats["passes"] + 1,
                                 merges=self.mapper_stats["merges"] + merges,
                                 last_clusters=len(clusters), before=before,
                                 after=len(self.store.all()), updated=round(time.time(), 1))
        return merges

    def mapper_loop(self, every: float = 25.0):
        while True:
            time.sleep(every)
            if self.mapper_stats.get("on"):
                try:
                    self.run_mapper_pass()
                except Exception:
                    pass

    def snapshot(self) -> dict:
        persons = []
        for p in sorted(self.store.all(), key=lambda x: x.global_id):
            g = p.global_id
            persons.append({
                "gid": g, "cameras": sorted(p.cameras_seen), "multi_cam": p.multi_camera,
                "n_obs": sum(self.gid_cam_obs[g].values()), "per_cam": dict(self.gid_cam_obs[g]),
                "app": len(p.app_embs), "face": len(p.face_embs), "gait": len(p.gait_embs),
                "rejected": int(getattr(p, "rejected_exemplars", 0)),
                "first_seen": round(p.first_seen, 1), "last_seen": round(p.last_seen, 1),
                "crop_cams": sorted(self.gid_cam_crop.get(g, {}).keys()),
                "name": ((self.store.identity.get(p.person_uuid).name
                          if self.store.identity.get(p.person_uuid) else None)),
            })
        live = set(self.known_cameras) if self.known_cameras else set(self._cam_frame)
        def live_event(e):
            cam = e.get("camera")
            return cam is None or not live or cam in live
        visible_events = [e for e in self.events if live_event(e)]
        identity = [e for e in visible_events if e.get("type") == "identity"]
        alerts = [e for e in visible_events
                  if e.get("type") in ("intrusion", "loitering", "absence", "unauthorised")]
        counts = [e for e in visible_events if e.get("type") == "count"]
        def visible_scope(v):
            if v is None:
                return "all"
            scoped = sorted(set(v) & live) if live else sorted(v)
            return scoped if scoped else None
        summary = {
            "mode": getattr(self, "mode", "static"), "gallery": self.gallery_kind,
            "stream": self.stream_name, "detections": self.detections,
            "cameras": len(self._cam_frame),
            "persons": len(persons), "multi_cam": sum(1 for p in persons if p["multi_cam"]),
            "identity_events": len(identity),
            # exemplars refused because they contradicted the identity's own memory —
            # a rising number means gid bindings are flipping between people
            "exemplars_rejected": sum(int(getattr(p, "rejected_exemplars", 0))
                                      for p in self.store.all()),
            # faces refused before matching because the DETECTION was too weak to
            # be a face at all -- the dominant source of false recognitions
            "faces_rejected_weak": sum(int(getattr(pl, "weak_rejects", 0))
                                       for pl in getattr(self.host, "plugins", [])),
            "usecases": {k: s for k, v in self.uc_cams.items()
                         for s in [visible_scope(v)] if s is not None},
            "intrusions": sum(1 for e in alerts if e["type"] == "intrusion"),
            "loiter_alerts": sum(1 for e in alerts if e["type"] == "loitering"),
            "absence_alerts": sum(1 for e in alerts if e["type"] == "absence"),
            "unauthorised_alerts": sum(1 for e in alerts if e["type"] == "unauthorised"),
            "count_in": sum(1 for e in counts if (e.get("payload") or {}).get("direction") == "in"),
            "count_out": sum(1 for e in counts if (e.get("payload") or {}).get("direction") == "out"),
            "updated": round(time.time(), 1),
        }
        summary["face_gallery"] = self.face_gallery_status()
        # Keep every canonical event type visible to the API.  Returning only identity
        # events made successful face recognition impossible to observe externally.
        # The Activity list never renders identity/person_merged, and those dominate
        # the stream (781 identity vs 7 face_recognized on a real morning). Truncating
        # a mixed list to 200 therefore left the feed with three renderable rows and
        # an Activity panel that looked broken. Keep BOTH: `events` stays complete for
        # any API consumer, `feed` is the last 200 events worth showing.
        feed = [e for e in visible_events
                if e.get("type") not in ("identity", "person_merged")]
        return {"summary": summary, "persons": persons, "events": visible_events[-200:],
                "feed": feed[-200:],
                "alerts": alerts[-100:], "counting": (self.counting.tallies if self.counting else {})}


def _obs_from_dict(d):
    def emb(v):
        return np.asarray(v, np.float32) if v is not None else None
    raw = int(d["local_id"])
    key = int(d.get("stable_id", raw))
    gid = d.get("gid")
    return TrackObservation(
        camera=str(d["camera"]), local_id=key, bbox=tuple(d["bbox"]),
        t=float(d["t"]), frame_idx=int(d["frame"]), quality=float(d.get("quality", 1.0)),
        gid=int(gid) if gid is not None else None,
        app_emb=emb(d.get("app_emb")), face_emb=emb(d.get("face_emb")),
        gait_emb=emb(d.get("gait_emb")), color=emb(d.get("color")),
        meta={"raw_lid": raw, "crop": d.get("crop"), "frame_wh": d.get("frame_wh"),
              "display_wh": d.get("display_wh"),
              # face DETECTION quality behind face_emb: {det, w, h, sharp, q}
              "face_meta": d.get("face_meta")})


PLAT: "LivePlatform" = None   # the live/static instance, for crop serving


def _publish(snap):
    with _LOCK:
        STATE.update(snap)


def run_static(stream_path: str, gallery_kind: str):
    global PLAT
    plat = LivePlatform(gallery_kind)
    plat.mode = "static"
    plat.stream_name = os.path.basename(stream_path)
    plat.obs_base_dir = os.path.dirname(os.path.abspath(stream_path))
    obs = []
    with open(stream_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    obs.append(_obs_from_dict(json.loads(line)))
                except Exception:
                    pass
    plat.ingest(obs)
    PLAT = plat
    _publish(plat.snapshot())
    print(f"[PLATF] {STATE['summary']}", flush=True)


def run_live(obs_dir: str, gallery_kind: str, poll_s: float = 1.0):
    """Tail every obs_*.jsonl in obs_dir, feeding new complete lines to the platform."""
    global PLAT
    plat = LivePlatform(gallery_kind)
    plat.mode = "live"
    plat.stream_name = os.path.basename(obs_dir.rstrip("/"))
    plat.obs_base_dir = os.path.abspath(obs_dir)
    PLAT = plat
    # LIVE = tail. Start each existing dump at its CURRENT end so a restart ingests only
    # NEW appends -- never replays the whole history (the dump grows to many GB, and
    # re-reading + parsing every embedding from offset 0 would hang / OOM the process).
    offsets: dict[str, int] = {}
    for path in glob.glob(os.path.join(obs_dir, "obs_*.jsonl")):
        try:
            offsets[path] = os.path.getsize(path)
        except Exception:
            pass
    print(f"[PLATF] LIVE tailing {obs_dir} from end (gallery={gallery_kind}, "
          f"{len(offsets)} existing files)", flush=True)
    refresh_cam_sid()

    def loop():
        tick = 0
        while True:
            tick += 1
            if tick % 10 == 1:
                refresh_cam_sid()   # keep camera->sid fresh as streams come/go
            new_obs = []
            for path in sorted(glob.glob(os.path.join(obs_dir, "obs_*.jsonl"))):
                try:
                    size = os.path.getsize(path)
                    off = offsets.get(path, 0)
                    if size < off:          # dump was rotated/truncated -> retail from top
                        off = 0
                    if size <= off:
                        continue
                    with open(path, "r", encoding="utf-8") as f:
                        f.seek(off)
                        data = f.read()
                    # only consume up to the last complete line
                    cut = data.rfind("\n")
                    if cut < 0:
                        continue
                    offsets[path] = off + cut + 1
                    for line in data[:cut].splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            new_obs.append(_obs_from_dict(json.loads(line)))
                        except Exception:
                            pass
                except Exception:
                    continue
            if new_obs:
                plat.ingest(new_obs)
                _publish(plat.snapshot())
            time.sleep(poll_s)

    threading.Thread(target=loop, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (Path(__file__).resolve().parent / "ui" / "dashboard.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html")
        if self.path.startswith("/api/crop"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            gid = q.get("gid", [None])[0]
            cam = q.get("cam", [None])[0]
            path = PLAT.crop_path(gid, cam) if (PLAT and gid and cam) else None
            if not path:
                return self._send(404, b"", "image/jpeg")
            try:
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            except Exception:
                return self._send(404, b"", "image/jpeg")
        if self.path.startswith("/api/camera_frame"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cam = q.get("cam", [None])[0]
            sid = CAM_SID.get(cam)
            if sid is None:
                return self._send(404, b"", "image/jpeg")
            try:
                with urllib.request.urlopen(f"{BACKBONE_URL}/api/frame/{sid}", timeout=4) as r:
                    return self._send(200, r.read(), "image/jpeg")
            except Exception:
                return self._send(404, b"", "image/jpeg")
        if self.path == "/api/live":
            return self._send(200, json.dumps(PLAT.live_view() if PLAT else {}))
        if self.path == "/api/cameras":
            return self._send(200, json.dumps(sorted(PLAT._cam_frame.keys()) if PLAT else []))
        if self.path == "/api/audit":
            return self._send(200, json.dumps(PLAT.audit() if PLAT else []))
        with _LOCK:
            if self.path == "/api/summary":
                return self._send(200, json.dumps(STATE["summary"]))
            if self.path == "/api/persons":
                return self._send(200, json.dumps(STATE["persons"]))
            if self.path == "/api/events":
                return self._send(200, json.dumps(STATE["events"][-200:]))
        return self._send(404, "{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", help="one-shot: replay this recorded JSONL")
    ap.add_argument("--live", help="LIVE: tail this OBS_DUMP_DIR as it grows")
    # default "none" = pure identity-INGEST: trust the backbone gid, run NO second
    # gallery. "real"/"fake" reconstruct a gid for legacy gid-less dumps (offline only).
    ap.add_argument("--gallery", choices=["none", "fake", "real"], default="none")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PLATF_PORT", 8090)))
    a = ap.parse_args()
    if a.live:
        run_live(a.live, a.gallery)
    elif a.stream:
        run_static(a.stream, a.gallery)
    else:
        ap.error("need --stream FILE or --live DIR")
    print(f"[PLATF] dashboard on http://0.0.0.0:{a.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
