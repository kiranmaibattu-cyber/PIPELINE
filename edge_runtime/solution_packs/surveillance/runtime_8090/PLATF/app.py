"""PLATF -- the Person Intelligence Platform as ONE proper application.

This is the whole app in a single process tree: it EMBEDS the tested backbone engine
(`MTMC.streamapp_2tier`) verbatim -- same decode/detect/track/re-id, same shared gallery,
same tested quality -- and feeds every observation IN-PROCESS to the plugin host. No disk
tail, no HTTP dependency on a separate :8083. The `:8083` app is just the reference this
reuses.

    engine (reused: reid_service + infer_pool + run_stream workers)
        --OBS_Q (in-process mp queue)-->  plugin host (re-id ingest + use-cases)
        --/dev/shm annotated frames-->    the 3-page UI (Live · Use-cases · Metrics)

Run:
    python -m PLATF.app --streams PLATF/config/streams.yaml --port 8090
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import MTMC.streamapp_2tier as bb
from edge_runtime.runtime.source_resolver import CameraSourceResolver
from PLATF.autocall import DEFAULT_CONFIG as AUTOCALL_DEFAULT_CONFIG
from PLATF.autocall import AutoCallDispatcher
from PLATF.bev import BEV
from PLATF.core import Event
from PLATF.investigations import InvestigationStore
from PLATF.server import LivePlatform, _obs_from_dict

UI = Path(__file__).resolve().parent / "ui" / "dashboard.html"


def _safe_name(value: str) -> str:
    text = str(value or "")
    safe = "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")
    return safe or "item"


class App:
    """Owns the embedded engine + the plugin-host platform + the camera<->sid map."""

    def __init__(self, crops_dir: str, gallery: str = "none", streams_path: str | None = None):
        self.crops_dir = os.path.abspath(crops_dir)
        self.streams_path = Path(streams_path).resolve() if streams_path else None
        self.stream_specs: dict[str, dict] = {}     # camera -> persisted stream spec
        self.cam_sid: dict[str, int] = {}          # camera -> engine stream id
        self._next_sid = 1
        self.autocall_config = dict(AUTOCALL_DEFAULT_CONFIG)
        self.investigations = InvestigationStore()
        self.bev = BEV(self._load_calibration())   # ground-plane projection + map association
        self.plat = LivePlatform(gallery)          # gallery=none: identity INGESTED from engine gid
        self.plat.mode = "live"
        self.plat.stream_name = "engine"
        self.plat.obs_base_dir = self.crops_dir    # crops/<...> resolve here
        self.plat.camera_source = self.camera_source   # enrollment opens its own capture
        self.plat.management_snapshot = self._save_management_snapshot
        self._load_runtime_config()
        self.autocall = AutoCallDispatcher(self.autocall_config, outcome_callback=self._autocall_outcome)
        self.plat.bus.subscribe("unauthorised", self.autocall.on_alert)
        self._snap = {"summary": {}, "persons": [], "events": []}
        self._snap_t = 0.0
        self._lock = threading.Lock()
        self._frame_cache = {}                      # (camera, width) -> (mtime, jpeg)
        self._placeholder_cache = {}                # (camera, width, label) -> jpeg

    @staticmethod
    def _calib_path():
        return Path(__file__).resolve().parent / "config" / "calibration.yaml"

    @staticmethod
    def _runtime_path():
        configured = os.environ.get("PLATF_RUNTIME_CONFIG")
        return Path(configured) if configured else Path(__file__).resolve().parent / "config" / "runtime_usecases.json"

    def _load_runtime_config(self):
        """Restore user-authored zones and per-camera toggles after a restart."""
        try:
            data = json.loads(self._runtime_path().read_text(encoding="utf-8"))
        except Exception:
            return
        self.plat.set_zones(data.get("zones") or {})
        self.plat.face_groups.update(
            {str(k): str(v) for k, v in (data.get("face_groups") or {}).items()})
        if isinstance(data.get("autocall"), dict):
            self.autocall_config.update(data.get("autocall") or {})
        if isinstance(data.get("mapper"), dict):
            self.plat.mapper_stats["on"] = bool(data["mapper"].get("on", True))
        for name, scope in (data.get("usecases") or {}).items():
            if name not in {"reid", "face", "intrusion", "loitering", "counting", "absence"}:
                continue
            self.plat.uc_cams[name] = None if scope == "all" else set(scope or [])
            self.plat._apply(name)

    def persist_runtime_config(self):
        data = {
            "usecases": {k: ("all" if v is None else sorted(v))
                         for k, v in self.plat.uc_cams.items()},
            "zones": self.plat.zones_raw,
            "face_groups": dict(self.plat.face_groups),
            "autocall": dict(self.autocall_config),
            "mapper": {"on": bool(self.plat.mapper_stats.get("on", True))},
        }
        path = self._runtime_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)

    def persist_stream_config(self):
        """Write runtime stream add/remove changes back to --streams.

        Operators add cameras from the UI, then expect restart to keep the same camera
        set. The previous path only changed in-memory workers, so a restart reverted to
        PLATF/config/streams.yaml and made the UI/runtime disagree with the config.
        """
        if self.streams_path is None:
            return
        streams = [dict(v) for _cam, v in sorted(self.stream_specs.items())]
        data = {"streams": streams}
        path = self.streams_path
        tmp = path.with_suffix(path.suffix + ".tmp")
        if path.suffix.lower() in {".yaml", ".yml"}:
            import yaml
            txt = yaml.safe_dump(data, sort_keys=False)
        else:
            txt = json.dumps(data, indent=2)
        tmp.write_text(txt, encoding="utf-8")
        tmp.replace(path)

    def _autocall_outcome(
        self,
        call_id: str,
        source: dict,
        outcome: dict,
        row: dict,
    ) -> None:
        src_event = source.get("event") or {}
        recipient = source.get("recipient") or {}
        status = str((outcome or {}).get("status") or "unclear")
        event_type = "autocall_investigated" if status == "acknowledged" else "autocall_followup_needed"
        payload = dict(src_event.get("payload") or {})
        payload.update({
            "employee_id": payload.get("employee_id") or src_event.get("name") or "",
            "call_id": call_id,
            "call_status": status,
            "call_note": str((outcome or {}).get("note") or ""),
            "recipient": recipient.get("name") or "",
            "phone": recipient.get("phone") or "",
        })
        self.plat.bus.publish(Event(
            event_type,
            time.time(),
            src_event.get("camera"),
            src_event.get("person_id"),
            src_event.get("zone"),
            payload=payload,
        ))

    @classmethod
    def _load_calibration(cls):
        try:
            import yaml
            return yaml.safe_load(cls._calib_path().read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def set_calibration(self, camera, image, mp):
        """Set ONE camera's floor<->map correspondence: update the live homography AND
        persist to calibration.yaml (survives restart). Returns True on success."""
        if not self.bev.set_camera(camera, image, mp):
            return False
        try:
            import yaml
            data = self._load_calibration() or {}
            data.setdefault("cameras", {})[camera] = {
                "image": [[float(x), float(y)] for x, y in image],
                "map": [[float(x), float(y)] for x, y in mp]}
            self._calib_path().write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        except Exception as e:
            print(f"[APP] calib persist failed: {e}", flush=True)
        return True

    def calibration(self):
        c = self._load_calibration() or {}
        return {"map": c.get("map", {"w": self.bev.map_w, "h": self.bev.map_h}),
                "cameras": c.get("cameras", {}),
                "live": sorted(self.cam_sid.keys())}

    def bev_view(self, radius: float = 60.0):
        """Project the latest foot-point per person to the shared map + associate by
        position (cross-camera link by geometry, no appearance). Projections that fall
        outside the map bounds are dropped -- a homography extrapolates wildly for
        detections beyond the calibrated floor quad (garbage points like (1447,-706))."""
        W, H = self.bev.map_w, self.bev.map_h
        mar = 0.15                                   # allow 15% past the edge, then reject
        pts = []
        for cam, (fr, boxes) in self.plat._cam_frame.items():
            for b in boxes:
                x1, y1, x2, y2 = b["bbox"]
                m = self.bev.project(cam, [(x1 + x2) / 2.0, y2])
                if m is None:
                    continue
                if not (-mar * W <= m[0] <= W * (1 + mar) and -mar * H <= m[1] <= H * (1 + mar)):
                    continue                         # off-map extrapolation -> drop
                pts.append({"gid": b.get("gid"), "camera": cam, "map": m})
        return {"map": {"w": W, "h": H}, "radius": radius,
                "points": pts, "clusters": self.bev.associate(pts, radius=radius),
                "cameras": sorted(self.cam_sid.keys()),
                "calibrated": [c for c in self.cam_sid if self.bev.calibrated(c)]}

    def churn_audit(self, thr: float = 0.16):
        """Within-camera churn: cluster the store's persons by SAME-CAMERA appearance +
        matching torso COLOUR (non-co-visible) -- a cluster with >1 gid ~ ONE physical person
        who got multiple ids = churn. Appearance is weak on this footage (over-groups at loose
        bars), so keep the bar TIGHT + require colour agreement; still verify by eye."""
        from PLATF.offline_mapper import cluster, _color_dist
        try:
            persons = self.plat.store.all()
        except Exception:
            return {"groups": [], "n_persons": 0}
        # TIGHT same-camera appearance grouping (cross_thr=0 disables cross-cam).
        groups = cluster(persons, app_thr=float(thr), cross_thr=0.0, face_veto=1.1,
                         colour_gate=0.30, topo=None, max_cluster=12)
        by_gid = {p.global_id: p for p in persons}
        cgate = float(os.environ.get("CHURN_COLOUR_GATE", "0.28"))
        out = []
        for g in groups:
            gids = [x for x in g.get("gids", []) if x in by_gid]
            # COLOUR post-filter: appearance over-groups on this footage, so keep only members
            # whose torso colour agrees with the survivor's (Bhattacharyya <= cgate).
            surv = g.get("survivor")
            if surv in by_gid and len(gids) > 1:
                sc = dict(by_gid[surv].color_hists)
                gids = [x for x in gids
                        if x == surv or _color_dist(sc, dict(by_gid[x].color_hists)) <= cgate]
            if len(gids) < 2:
                continue
            cams = sorted({c for x in gids for c in by_gid[x].cameras_seen})
            crops = []
            for x in sorted(gids):
                for c in sorted(by_gid[x].cameras_seen):
                    if self.plat.crop_path(x, c):
                        crops.append({"gid": x, "cam": c})
                        break
            out.append({"survivor": g.get("survivor"), "gids": sorted(gids),
                        "cameras": cams, "n": len(gids), "crops": crops})
        out.sort(key=lambda z: -z["n"])
        return {"groups": out, "n_persons": len(persons),
                "n_churned": len(out), "churn_ids": sum(z["n"] for z in out)}

    # --- engine bring-up -------------------------------------------------------
    def start(self, streams: list):
        os.makedirs(os.path.join(self.crops_dir, "crops"), exist_ok=True)
        bb.setup_engine()                          # reused engine: manager + gallery + pools
        bb.OBS_Q = bb.MGR.Queue(maxsize=20000)     # in-process observation bus
        bb.OBS_DUMP_DIR = self.crops_dir           # crops only (OBS_Q set => no JSONL)
        # per-frame tracker boxes for the live overlay, kept off OBS_Q so high-rate
        # geometry can never evict an observation the re-id path needs.
        bb.BOX_STATE = bb.MGR.dict()
        self.plat.box_state = bb.BOX_STATE
        self.plat.bus.subscribe("*", self._engine_alert)
        for s in streams:
            source = CameraSourceResolver().resolve(str(s["source"]))
            print(f"Camera {s.get('camera')} source loaded from mounted Secret", flush=True)
            self.add_stream(source, s.get("camera"),
                            bool(s.get("face", True)), bool(s.get("gait", True)),
                            warm=0.4, persist=False)
        self.plat.set_cameras(self.cam_sid.keys())
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._detect_frame_size, daemon=True).start()
        threading.Thread(target=self._startup_stream_watchdog, daemon=True).start()
        threading.Thread(target=self._runtime_stream_watchdog, daemon=True).start()
        threading.Thread(target=self._prune_crops, daemon=True).start()
        every = float(os.environ.get("MAP_EVERY", "25"))   # GBSL offline mapper cadence
        threading.Thread(target=self.plat.mapper_loop, args=(every,), daemon=True).start()
        if os.environ.get("BEV_LINK", "0") == "1":         # overlapping cross-cam by POSITION
            threading.Thread(target=self._bev_link_loop, daemon=True).start()

    def _startup_stream_watchdog(self):
        """During high-FPS startup, a worker can exit before publishing frames while
        the shared OpenVINO pools are still warming. Restart only app-owned workers
        that are dead or still at 0 FPS after warmup."""
        checks = int(os.environ.get("STARTUP_STREAM_RECOVERY_CHECKS", "3"))
        warm_s = float(os.environ.get("STARTUP_STREAM_RECOVERY_WARM_S", "35"))
        gap_s = float(os.environ.get("STARTUP_STREAM_RECOVERY_GAP_S", "15"))
        time.sleep(warm_s)
        for _ in range(max(0, checks)):
            for sid, w in list(bb.WORKERS.items()):
                try:
                    snap = w.snapshot()
                    dead = not bool(snap.get("running", True))
                    cold = float(snap.get("fps") or 0.0) <= 0.01 and int(snap.get("frames") or 0) == 0
                    if dead or cold:
                        print(f"[APP] startup watchdog restarting stream {sid}: "
                              f"dead={dead} cold={cold}", flush=True)
                        self.restart_stream(int(sid))
                except Exception as exc:
                    print(f"[APP] startup watchdog stream {sid} failed: {exc}", flush=True)
            time.sleep(gap_s)

    def _runtime_stream_watchdog(self):
        """Continuously recover camera workers that exit after startup.

        The parent app can stay healthy while one RTSP/decode worker dies. Without
        this, `/api/live` keeps serving the last stale frame and the UI looks frozen.
        Restart only after repeated stalled checks and use a cooldown to avoid a
        restart loop if the physical camera/network is down.
        """
        if os.environ.get("RUNTIME_STREAM_WATCHDOG", "1") in ("0", "false", "False"):
            return
        warm_s = float(os.environ.get("RUNTIME_STREAM_WATCHDOG_WARM_S", "60"))
        gap_s = float(os.environ.get("RUNTIME_STREAM_WATCHDOG_GAP_S", "10"))
        cooldown_s = float(os.environ.get("RUNTIME_STREAM_RESTART_COOLDOWN_S", "30"))
        max_stalled = int(os.environ.get("RUNTIME_STREAM_STALL_CHECKS", "3"))
        time.sleep(warm_s)
        prev: dict[int, tuple[int, int]] = {}  # sid -> (frames, stalled_count)
        last_restart: dict[str, float] = {}
        while True:
            try:
                now = time.time()
                for sid, w in list(bb.WORKERS.items()):
                    sid = int(sid)
                    try:
                        snap = w.snapshot()
                    except Exception as exc:
                        print(f"[APP] runtime watchdog snapshot failed stream {sid}: {exc}", flush=True)
                        snap = {}
                    camera = next((cam for cam, cur_sid in self.cam_sid.items()
                                   if int(cur_sid) == sid), str(sid))
                    frames = int(snap.get("frames") or 0)
                    fps = float(snap.get("fps") or 0.0)
                    running = bool(snap.get("running", True))
                    proc_alive = bool(getattr(getattr(w, "proc", None), "is_alive", lambda: True)())
                    old_frames, stalled_count = prev.get(sid, (-1, 0))
                    if frames == old_frames and fps <= 0.01:
                        stalled_count += 1
                    else:
                        stalled_count = 0
                    prev[sid] = (frames, stalled_count)
                    dead = (not running) or (not proc_alive)
                    stalled = stalled_count >= max_stalled
                    if not (dead or stalled):
                        continue
                    if now - last_restart.get(camera, 0.0) < cooldown_s:
                        continue
                    print(f"[APP] runtime watchdog restarting stream {sid}/{camera}: "
                          f"dead={dead} stalled={stalled} running={running} "
                          f"proc_alive={proc_alive} fps={fps:.2f} frames={frames}",
                          flush=True)
                    try:
                        out = self.restart_stream(sid)
                        last_restart[camera] = now
                        prev.pop(sid, None)
                        print(f"[APP] runtime watchdog restarted {camera}: {out}", flush=True)
                    except Exception as exc:
                        last_restart[camera] = now
                        print(f"[APP] runtime watchdog restart failed {sid}/{camera}: {exc}",
                              flush=True)
            except Exception as exc:
                print(f"[APP] runtime watchdog failed: {exc}", flush=True)
            time.sleep(gap_s)

    @staticmethod
    def _engine_alert(event):
        """Temporarily recolor the engine's EXISTING synchronized identity box.

        The event's `who` is camera:stable_local_id, using the same stable id the
        worker renders. A gid key is also published as a fallback. Absence has no
        person box, so it remains a camera-header alert only.
        """
        if bb.ALERT_TRACKS is None or event.type not in {"intrusion", "loitering",
                                                         "unauthorised"}:
            return
        cam = str(event.camera or "")
        until = float(event.t) + float(os.environ.get("ENGINE_ALERT_S", "5"))
        who = str((event.payload or {}).get("who", ""))
        prefix = f"{cam}:"
        if who.startswith(prefix):
            try:
                bb.ALERT_TRACKS[f"{cam}:track:{int(who[len(prefix):])}"] = until
            except ValueError:
                pass
        if event.person_id is not None:
            bb.ALERT_TRACKS[f"{cam}:gid:{int(event.person_id)}"] = until

    def _bev_link_loop(self, every_s: float = 1.0, confirm: int = 4):
        """OVERLAPPING cross-cam by POSITION (the reliable, appearance-free path): gids from
        two OVERLAPPING cameras (ch9/ch10) that co-locate on the shared BEV map for `confirm`
        consecutive checks are the SAME physical person -> merge into one identity in the
        store, so they show ONE id across the overlap. Env-gated (BEV_LINK=1): only correct
        once the ch9/ch10 calibration is accurate, else it would merge by a wrong projection."""
        radius = float(os.environ.get("BEV_LINK_RADIUS", "70"))
        pend: dict = {}
        while True:
            time.sleep(every_s)
            try:
                v = self.bev_view(radius=radius)
                live = set()
                for cl in v.get("clusters", []):
                    if len(cl.get("cameras", [])) < 2:
                        continue                            # only cross-camera clusters
                    gids = sorted(g for g in cl.get("gids", []) if g is not None)
                    for i in range(len(gids)):
                        for k in range(i + 1, len(gids)):
                            key = (gids[i], gids[k])
                            live.add(key)
                            pend[key] = pend.get(key, 0) + 1
                            if pend[key] == confirm:        # persisted -> commit the link
                                try:
                                    self.plat.store.merge(gids[i], gids[k])
                                    if self.plat.history is not None:
                                        self.plat.history.add_merge(gids[i], gids[k],
                                                                    reason="bev")
                                except Exception:
                                    pass
                for key in list(pend):                       # reset pairs that broke apart
                    if key not in live:
                        pend.pop(key, None)
            except Exception:
                pass

    def _prune_crops(self, cap: int = None, every_s: float = 60.0):
        """Keep the crop cache bounded (it grows unbounded otherwise -- 240k files/8GB).
        Deletes the oldest crops when over the cap."""
        cap = cap or int(os.environ.get("PLATF_CROP_CAP", "20000"))
        cdir = os.path.join(self.crops_dir, "crops")
        while True:
            time.sleep(every_s)
            try:
                files = [os.path.join(cdir, f) for f in os.listdir(cdir)]
                if len(files) > cap:
                    files.sort(key=lambda p: os.path.getmtime(p))
                    for p in files[:len(files) - cap]:
                        try:
                            os.remove(p)
                        except Exception:
                            pass
            except Exception:
                pass
        print(f"[APP] engine up; {len(streams)} streams; cameras={list(self.cam_sid)}; "
              f"mapper every {every}s", flush=True)

    def _detect_frame_size(self):
        """Read the real WxH of the engine's annotated frame so zones + overlays use the
        SAME coordinate space as the bboxes/foot-points."""
        import cv2
        for _ in range(90):
            for sid in list(self.cam_sid.values()):
                try:
                    im = cv2.imread(bb._shm_path(sid))
                    if im is not None and im.shape[0] > 0:
                        self.plat.set_frame_size(im.shape[1], im.shape[0])
                        print(f"[APP] frame coord space = {im.shape[1]}x{im.shape[0]}", flush=True)
                        return
                except Exception:
                    pass
            time.sleep(1)

    def camera_source(self, camera: str):
        """The stream URL behind a camera name, from its running worker."""
        sid = self.cam_sid.get(camera)
        w = bb.WORKERS.get(sid) if sid is not None else None
        if w is None:
            return None
        try:
            return w.snapshot().get("source") or getattr(w, "source", None)
        except Exception:
            return getattr(w, "source", None)

    def add_stream(self, source: str, camera: str = None, face: bool = True,
                   gait: bool = True, warm: float = 0.0, persist: bool = True,
                   update_spec: bool = True):
        """Start one more camera at runtime (the tested WorkerHandle)."""
        sid = self._next_sid
        self._next_sid += 1
        camera = camera or f"cam{sid}"
        if camera in self.cam_sid:
            raise ValueError(f"camera already exists: {camera}")
        w = bb.WorkerHandle(sid, source, camera, face, gait)
        bb.WORKERS[sid] = w
        w.start()
        self.cam_sid[camera] = sid
        if update_spec:
            self.stream_specs[camera] = {
                "source": source, "camera": camera,
                "face": bool(face), "gait": bool(gait),
            }
        self.plat.set_cameras(self.cam_sid.keys())
        # New cameras should not silently miss identity just because runtime_usecases.json
        # was scoped to the previous camera set. Re-ID is the identity substrate; if it is
        # enabled for a subset, include the new camera. Explicitly disabled usecases
        # remain disabled.
        if update_spec:
            scope = self.plat.uc_cams.get("reid")
            if isinstance(scope, set):
                scope.add(camera)
            self.persist_runtime_config()
        if persist:
            self.persist_stream_config()
        if warm:
            time.sleep(warm)                        # stagger worker starts (pool warmup)
        return {"id": sid, "camera": camera}

    def remove_stream(self, sid: int, persist: bool = True, update_spec: bool = True):
        """Stop one runtime worker and remove its camera mapping."""
        sid = int(sid)
        w = bb.WORKERS.pop(sid, None)
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass
        removed = []
        for cam, cur_sid in list(self.cam_sid.items()):
            if int(cur_sid) == sid:
                removed.append(cam)
                self.cam_sid.pop(cam, None)
                if update_spec:
                    self.stream_specs.pop(cam, None)
                    for scope in self.plat.uc_cams.values():
                        if isinstance(scope, set):
                            scope.discard(cam)
        self.plat.set_cameras(self.cam_sid.keys())
        if update_spec:
            self.persist_runtime_config()
        if persist:
            self.persist_stream_config()
        return {"id": sid, "removed": bool(w is not None or removed), "cameras": removed}

    def restart_stream(self, sid: int):
        """Restart a stream from its current worker metadata."""
        sid = int(sid)
        w = bb.WORKERS.get(sid)
        if w is None:
            raise KeyError(f"stream {sid} not found")
        snap = w.snapshot()
        camera = next((cam for cam, cur_sid in self.cam_sid.items()
                       if int(cur_sid) == sid), snap.get("camera") or f"cam{sid}")
        source = snap.get("source") or getattr(w, "source", "")
        face = bool(snap.get("face", True))
        gait = bool(snap.get("gait", True))
        # Stream restart is operational, not a config edit. Do not let watchdog/API
        # restart races rewrite streams.yaml from a transient worker set.
        self.remove_stream(sid, persist=False, update_spec=False)
        return self.add_stream(source, camera, face, gait, warm=0.4,
                               persist=False, update_spec=False)

    # --- in-process observation drain -> plugin host ---------------------------
    def _drain(self):
        buf = []
        last_flush = 0.0
        while True:
            try:
                obs = bb.OBS_Q.get(timeout=0.3)
            except Exception:
                obs = None
            now = time.time()
            if obs is not None:
                buf.append(obs)
            elif not buf:
                self.plat.host.idle()
            if buf and (len(buf) >= 256 or obs is None or now - last_flush > 0.5):
                try:
                    self.plat.ingest([_obs_from_dict(d) for d in buf])
                except Exception:
                    pass
                buf = []
                last_flush = now
                if now - self._snap_t > 1.0:       # refresh cached snapshot ~1/s
                    snap = self.plat.snapshot()
                    with self._lock:
                        self._snap = snap
                        self._snap_t = now

    def snapshot(self):
        with self._lock:
            return self._snap

    # --- live frame straight from the engine's annotated /dev/shm frame ---------
    def frame(self, camera: str, max_w: int = 0):
        sid = self.cam_sid.get(camera)
        if sid is None:
            return None
        p = bb._shm_path(sid)
        try:
            if max_w and max_w > 0:
                st = os.stat(p)
                key = (camera, int(max_w))
                cached = self._frame_cache.get(key)
                if cached and cached[0] == st.st_mtime_ns:
                    return cached[1]
                import cv2
                im = cv2.imread(p)
                if im is None:
                    return None
                h, w = im.shape[:2]
                if w > max_w:
                    nh = max(1, int(round(h * max_w / w)))
                    im = cv2.resize(im, (int(max_w), nh), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 72])
                if not ok:
                    return None
                out = buf.tobytes()
                self._frame_cache[key] = (st.st_mtime_ns, out)
                return out
            with open(p, "rb") as f:
                return f.read()
        except Exception:
            return None

    def _save_management_snapshot(self, camera: str, event_type: str, event=None):
        """Persist event evidence and return state-relative correlated assets."""
        frame = self.frame(camera)
        if not frame:
            return None
        root = Path(os.environ.get("MANAGEMENT_SNAPSHOT_DIR", "/state/surveillance/snapshots"))
        root.mkdir(parents=True, exist_ok=True)
        safe_cam = _safe_name(camera)
        safe_type = _safe_name(event_type)
        stamp = _safe_name(datetime.now(timezone.utc).isoformat())[:40]
        filename = f"{safe_cam}_{safe_type}_{stamp}.jpg"
        path = root / filename
        path.write_bytes(frame)
        assets = {"frame": f"snapshots/{filename}"}
        person_crop = self._event_person_crop(frame, camera, event or {})
        if person_crop:
            crop_name = f"{safe_cam}_{safe_type}_{stamp}_person.jpg"
            (root / crop_name).write_bytes(person_crop)
            assets["person_crop"] = f"snapshots/{crop_name}"
        return assets

    def _event_person_crop(self, frame: bytes, camera: str, event: dict) -> bytes | None:
        """Find the alert track/global id in live boxes and encode its person crop."""
        try:
            import cv2
            import numpy as np

            payload = event.get("payload") or {}
            who = str(payload.get("who") or "")
            track = None
            if who.startswith(f"{camera}:"):
                track = int(who.rsplit(":", 1)[1])
            gid = event.get("person_id")
            live = self.plat.live_view().get("cameras", {}).get(camera, {})
            candidates = live.get("boxes") or []
            box = next((b for b in candidates if track is not None and b.get("track") == track), None)
            if box is None and gid is not None:
                box = next((b for b in candidates if b.get("gid") == gid), None)
            coords = list((box or {}).get("bbox") or [])
            image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or len(coords) != 4:
                return None
            h, w = image.shape[:2]
            x1, y1, x2, y2 = [int(round(value)) for value in coords]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                return None
            ok, encoded = cv2.imencode(
                ".jpg", image[y1:y2, x1:x2], [cv2.IMWRITE_JPEG_QUALITY, 90]
            )
            return encoded.tobytes() if ok else None
        except Exception:
            return None

    def placeholder_frame(self, camera: str | None, max_w: int = 0, label: str = "waiting for frame"):
        """A valid JPEG for temporarily missing stream frames.

        Returning a JPEG instead of 404 keeps the dashboard stable when an RTSP camera
        times out or a worker is warming. This is render-only; it does not fake live
        detections, tracking, Re-ID, or use-case state.
        """
        w = int(max_w) if max_w and max_w > 0 else 960
        w = max(320, min(1920, w))
        h = max(180, int(round(w * 9 / 16)))
        key = (str(camera or ""), w, label)
        cached = self._placeholder_cache.get(key)
        if cached:
            return cached
        try:
            import cv2
            import numpy as np

            im = np.zeros((h, w, 3), np.uint8)
            im[:] = (18, 24, 32)
            text = f"{camera or 'camera'} · {label}"
            font = cv2.FONT_HERSHEY_SIMPLEX
            scale = max(0.45, w / 1400.0)
            thickness = max(1, int(round(scale * 2)))
            (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
            org = (max(12, (w - tw) // 2), max(th + 12, (h + th) // 2))
            cv2.putText(im, text, org, font, scale, (150, 160, 176), thickness, cv2.LINE_AA)
            ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ok:
                return None
            out = buf.tobytes()
            self._placeholder_cache[key] = out
            return out
        except Exception:
            return None

    def _app_ram_metrics(self) -> dict:
        """RAM used by this PLATF process tree.

        Prefer PSS on Linux so shared model/library pages across worker processes are
        charged proportionally instead of being counted once per process as RSS.
        """
        try:
            import psutil
            root = psutil.Process(os.getpid())
            procs = [root] + root.children(recursive=True)
            total = psutil.virtual_memory().total or 1
            seen: set[int] = set()
            rss = pss = uss = 0
            counted = 0
            for proc in procs:
                if proc.pid in seen:
                    continue
                seen.add(proc.pid)
                try:
                    info = proc.memory_full_info()
                except Exception:
                    continue
                counted += 1
                rss += int(getattr(info, "rss", 0) or 0)
                pss += int(getattr(info, "pss", 0) or 0)
                uss += int(getattr(info, "uss", 0) or 0)
            used = pss or rss
            gib = 1024 ** 3
            return {
                "app_ram_gb": round(used / gib, 3),
                "app_ram_pct": round(100.0 * used / total, 2),
                "app_ram_rss_gb": round(rss / gib, 3),
                "app_ram_pss_gb": round(pss / gib, 3) if pss else None,
                "app_ram_uss_gb": round(uss / gib, 3) if uss else None,
                "app_proc_count": counted,
            }
        except Exception:
            return {}

    def metrics(self):
        try:
            m = bb._global()                       # reused engine metrics (streams+devices+reid)
        except Exception as e:
            return {"error": str(e)}
        m.setdefault("devices", {}).update(self._app_ram_metrics())
        # The engine keys streams by numeric id; the whole UI speaks camera names. Attach
        # the name here so a dashboard can label a stream without a second round trip.
        by_sid = {int(sid): cam for cam, sid in self.cam_sid.items()}
        for s in m.get("streams", []):
            s.setdefault("camera", by_sid.get(int(s.get("id", -1))))
        return m


APP: "App" = None


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
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_mjpeg(self, camera: str):
        """Stream the latest already-rendered engine JPEG over one persistent
        connection. This reduces dashboard overhead without changing detection,
        tracking, Re-ID, face, gait, zones, or alert generation."""
        fps = max(1.0, min(30.0, float(os.environ.get("PLATF_MJPEG_FPS", "15"))))
        delay = 1.0 / fps
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        while True:
            img = APP.frame(camera) if APP and camera else None
            if img:
                try:
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(img)}\r\n\r\n".encode())
                    self.wfile.write(img)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
            time.sleep(delay)

    @staticmethod
    def _do_search(query: str, camera=None, hours=None) -> dict:
        if APP is None or APP.plat.search is None:
            return {"people": [], "status": "history/search disabled"}
        since = None
        if hours:
            try:
                since = time.time() - float(hours) * 3600.0
            except ValueError:
                since = None
        try:
            return APP.plat.search.search(query, since=since, camera=camera or None)
        except Exception as exc:
            return {"people": [], "status": f"{type(exc).__name__}: {exc}"[:160]}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if p in ("/", "/index.html"):
            return self._send(200, UI.read_text(encoding="utf-8"), "text/html")
        if p.startswith("/api/camera_mjpeg"):
            q = urllib.parse.parse_qs(parsed.query)
            return self._send_mjpeg(q.get("cam", [None])[0])
        if p.startswith("/api/camera_frame"):
            q = urllib.parse.parse_qs(parsed.query)
            try:
                max_w = int(q.get("w", [0])[0] or 0)
            except Exception:
                max_w = 0
            cam = q.get("cam", [None])[0]
            img = APP.frame(cam, max_w=max_w) if APP else None
            if not img and APP:
                img = APP.placeholder_frame(cam, max_w=max_w)
            return self._send(200, img, "image/jpeg") if img else self._send(503, b"", "image/jpeg")
        if p.startswith("/api/crop"):
            q = urllib.parse.parse_qs(parsed.query)
            path = APP.plat.crop_path(q.get("gid", [None])[0], q.get("cam", [None])[0]) if APP else None
            if not path:
                return self._send(404, b"", "image/jpeg")
            try:
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            except Exception:
                return self._send(404, b"", "image/jpeg")
        if p.startswith("/api/enrollment/preview"):
            path = APP.plat.enrollment_preview_path() if APP else None
            if not path:
                return self._send(404, b"", "image/jpeg")
            try:
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            except Exception:
                return self._send(404, b"", "image/jpeg")
        if p.startswith("/api/exemplar_crop"):
            q = urllib.parse.parse_qs(parsed.query)
            hist = APP.plat.history if APP else None
            try:
                path = hist.exemplar_crop_path(int(q.get("gid", ["0"])[0]),
                                               q.get("modality", ["app"])[0],
                                               int(q.get("slot", ["0"])[0])) if hist else None
            except ValueError:
                path = None
            if not path:
                return self._send(404, b"", "image/jpeg")
            try:
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            except Exception:
                return self._send(404, b"", "image/jpeg")
        if p.startswith("/api/history_crop"):
            q = urllib.parse.parse_qs(parsed.query)
            try:
                sid = int(q.get("sighting", ["0"])[0])
            except ValueError:
                sid = 0
            path = APP.plat.history_crop_path(sid) if APP else None
            if not path:
                return self._send(404, b"", "image/jpeg")
            try:
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            except Exception:
                return self._send(404, b"", "image/jpeg")
        if p == "/api/alert_history":
            # The DURABLE log: what history.db has kept, not the in-memory list that
            # /api/events serves and that empties on restart.
            hist = APP.plat.history if APP else None
            if hist is None:
                return self._send(200, json.dumps([]))
            q = urllib.parse.parse_qs(parsed.query)
            try:
                limit = min(2000, max(1, int(q.get("limit", ["500"])[0])))
            except ValueError:
                limit = 500
            try:
                # Do not let old weak face matches keep appearing in Activity after
                # thresholds are tightened.  The DB remains an audit log; the operator
                # feed shows only rows that would pass the current runtime policy.
                face_thr = float(os.environ.get("FACE_THR", "0.45"))

                def strong_enough(ev):
                    if ev.get("type") not in ("face_recognized", "unauthorised"):
                        return True
                    payload = ev.get("payload") or {}
                    try:
                        dist = float(payload.get("dist"))
                    except (TypeError, ValueError):
                        return True
                    return dist <= face_thr

                rows = [e for e in hist.recent_events(limit=min(2000, limit * 5))
                        if strong_enough(e)]
                if APP is not None and getattr(APP, "autocall", None) is not None:
                    rows = APP.autocall.annotate_events(rows)
                if APP is not None and getattr(APP, "investigations", None) is not None:
                    rows = APP.investigations.annotate(rows)
                return self._send(200, json.dumps(rows[:limit]))
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}))
        if p == "/api/history":
            hist = APP.plat.history if APP else None
            out = hist.summary() if hist is not None else {"enabled": False}
            if APP and APP.plat.search is not None:
                out["search"] = APP.plat.search.stats()
            return self._send(200, json.dumps(out))
        if p.startswith("/api/search"):
            q = urllib.parse.parse_qs(parsed.query)
            return self._send(200, json.dumps(self._do_search(
                q.get("q", [""])[0], q.get("camera", [None])[0],
                q.get("hours", [None])[0])))
        # exact match: startswith("/api/person") also swallowed /api/persons
        if p == "/api/person":
            q = urllib.parse.parse_qs(parsed.query)
            hist = APP.plat.history if APP else None
            if hist is None:
                return self._send(404, json.dumps({"error": "history disabled"}))
            try:
                gid = int(q.get("gid", ["0"])[0])
            except ValueError:
                return self._send(400, json.dumps({"error": "bad gid"}))
            cam, how = APP.plat.live_camera_of(gid)
            out = hist.dossier(gid)
            out["live_camera"], out["live_match"] = cam, how
            # what the RECOGNITION side holds for this identity right now: a rolling
            # best-N per modality, which is a different thing from the per-visit
            # history above and is the usual source of confusion.
            try:
                p = APP.plat.store.get(APP.plat.store.canonical_gid(gid))
                out["live_exemplars"] = ({"app": len(p.app_embs), "face": len(p.face_embs),
                                          "gait": len(p.gait_embs),
                                          "cap": APP.plat.store.exemplar_cap}
                                         if p is not None else None)
            except Exception:
                out["live_exemplars"] = None
            return self._send(200, json.dumps(out))
        if p == "/api/person_audit":
            q = urllib.parse.parse_qs(parsed.query)
            hist = APP.plat.history if APP else None
            if hist is None:
                return self._send(404, json.dumps({"error": "history disabled"}))
            try:
                gid = int(q.get("gid", ["0"])[0])
            except ValueError:
                return self._send(400, json.dumps({"error": "bad gid"}))
            return self._send(200, json.dumps(hist.identity_audit(gid)))
        if p == "/api/cameras":
            return self._send(200, json.dumps(sorted(APP.cam_sid.keys()) if APP else []))
        if p == "/api/live":
            return self._send(200, json.dumps(APP.plat.live_view() if APP else {}))
        if p == "/api/audit":
            return self._send(200, json.dumps(APP.plat.audit() if APP else []))
        if p == "/api/metrics":
            return self._send(200, json.dumps(APP.metrics() if APP else {}))
        if p == "/api/summary":
            return self._send(200, json.dumps(APP.snapshot().get("summary", {}) if APP else {}))
        if p == "/api/persons":
            return self._send(200, json.dumps(APP.snapshot().get("persons", []) if APP else []))
        if p == "/api/events":
            # `feed` = what the Activity list can actually draw. ?all=1 returns every
            # type, including the identity events the UI filters out.
            q = urllib.parse.parse_qs(parsed.query)
            key = "events" if q.get("all", ["0"])[0] not in ("0", "", "false") else "feed"
            snap = APP.snapshot() if APP else {}
            return self._send(200, json.dumps(snap.get(key, snap.get("events", []))[-200:]))
        if p == "/api/face_gallery":
            q = urllib.parse.parse_qs(parsed.query)
            detail = q.get("detail", ["0"])[0] not in ("0", "", "false")
            return self._send(200, json.dumps(APP.plat.face_gallery_status(detail=detail)
                                              if APP else {"loaded": False}))
        if p.startswith("/api/face_chip"):
            q = urllib.parse.parse_qs(parsed.query)
            try:
                idx = int(q.get("i", ["0"])[0])
            except ValueError:
                idx = 0
            path = APP.plat.face_chip_path(q.get("person", [""])[0], idx) if APP else None
            if not path:
                return self._send(404, b"", "image/jpeg")
            try:
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
            except Exception:
                return self._send(404, b"", "image/jpeg")
        if p == "/api/enrollment":
            return self._send(200, json.dumps(APP.plat.enrollment_status() if APP else
                                              {"state": "idle"}))
        if p == "/api/alerts":
            return self._send(200, json.dumps(APP.snapshot().get("alerts", [])[-200:] if APP else []))
        if p == "/api/counting":
            return self._send(200, json.dumps(APP.snapshot().get("counting", {}) if APP else {}))
        if p == "/api/mapper":
            return self._send(200, json.dumps(APP.plat.mapper_stats if APP else {}))
        if p.startswith("/api/bev"):
            q = urllib.parse.parse_qs(parsed.query)
            try:
                r = float(q.get("radius", [60])[0])
            except Exception:
                r = 60.0
            return self._send(200, json.dumps(APP.bev_view(radius=r) if APP else {}))
        if p == "/api/calibration":
            return self._send(200, json.dumps(APP.calibration() if APP else {}))
        if p.startswith("/api/churn"):
            q = urllib.parse.parse_qs(parsed.query)
            try:
                thr = float(q.get("thr", [0.16])[0])
            except Exception:
                thr = 0.16
            return self._send(200, json.dumps(APP.churn_audit(thr=thr) if APP else {}))
        if p == "/api/usecases":
            plat = APP.plat if APP else None
            live = set(APP.cam_sid.keys()) if APP else set()
            def visible_scope(v):
                if v is None:
                    return "all"
                scoped = sorted(set(v) & live) if live else sorted(v)
                return scoped if scoped else None
            uc = {k: s for k, v in (plat.uc_cams.items() if plat else [])
                  for s in [visible_scope(v)] if s is not None}
            return self._send(200, json.dumps({
                "usecases": uc,                              # name -> "all" | [cameras]
                "cameras": sorted(APP.cam_sid.keys()) if APP else [],
                "zones": plat.zones_raw if plat else {},
                "available": ["reid", "intrusion", "loitering", "counting", "absence", "face"],
            }))
        return self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or "{}")
        except Exception:
            body = {}
        plat = APP.plat if APP else None
        if self.path == "/api/usecase" and plat is not None:
            name = str(body.get("name", "")).strip()
            on = bool(body.get("on", True))
            camera = body.get("camera")                      # None => all cameras
            ok = plat.enable_usecase(name, camera) if on else plat.disable_usecase(name, camera)
            if ok and APP is not None:
                APP.persist_runtime_config()
            return self._send(200 if ok else 400,
                              json.dumps({"name": name, "camera": camera, "on": on, "ok": bool(ok)}))
        if self.path == "/api/zones" and plat is not None:
            plat.set_zones(body or {})
            if APP is not None:
                APP.persist_runtime_config()
            return self._send(200, json.dumps({"ok": True, "zones": plat.zones_raw}))
        if self.path == "/api/streams" and APP is not None:
            src = str(body.get("source", "")).strip()
            if not src:
                return self._send(400, json.dumps({"error": "no source"}))
            res = APP.add_stream(src, body.get("camera"),
                                 bool(body.get("face", True)), bool(body.get("gait", True)))
            return self._send(200, json.dumps(res))
        if self.path == "/api/streams/restart" and APP is not None:
            try:
                return self._send(200, json.dumps(APP.restart_stream(int(body.get("id")))))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
        if self.path == "/api/mapper" and plat is not None:
            changed = False
            if "on" in body:
                plat.mapper_stats["on"] = bool(body["on"])
                changed = True
            if changed and APP is not None:
                APP.persist_runtime_config()
            merged = plat.run_mapper_pass() if body.get("run") else None
            return self._send(200, json.dumps({**plat.mapper_stats, "ran": merged}))
        if self.path == "/api/face_group" and plat is not None:
            try:
                out = plat.set_face_group(body.get("name", ""), body.get("group", ""))
                if APP is not None:
                    APP.persist_runtime_config()
                return self._send(200, json.dumps(out))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
        if self.path == "/api/alert_investigate" and APP is not None:
            try:
                event_id = int(body.get("id"))
                row = APP.investigations.mark(event_id)
                return self._send(200, json.dumps({"ok": True, "id": event_id, **row}))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
        if self.path == "/api/face_gallery/reload" and plat is not None:
            out = plat.face_gallery_status(reload=True)
            return self._send(200 if out.get("loaded") and not out.get("error") else 400,
                              json.dumps(out))
        if self.path == "/api/enrollment/start" and plat is not None:
            try:
                out = plat.start_enrollment(body.get("name", ""), body.get("camera", ""))
                return self._send(200, json.dumps(out))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
        if self.path == "/api/enrollment/retake" and plat is not None:
            try:
                return self._send(200, json.dumps(plat.retake_enrollment()))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
        if self.path in ("/api/enrollment/cancel", "/api/enrollment/revoke") and plat is not None:
            try:
                return self._send(200, json.dumps(plat.cancel_enrollment()))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
        if self.path == "/api/enrollment/save" and plat is not None:
            try:
                return self._send(200, json.dumps(plat.save_enrollment()))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
        if self.path == "/api/calibrate" and APP is not None:
            cam = str(body.get("camera", "")).strip()
            image = body.get("image") or []
            mp = body.get("map") or []
            if not cam or len(image) < 4 or len(image) != len(mp):
                return self._send(400, json.dumps({"error": "need camera + 4+ matched image/map points"}))
            ok = APP.set_calibration(cam, image, mp)
            return self._send(200 if ok else 400,
                              json.dumps({"ok": bool(ok), "camera": cam, "calibrated": APP.bev.calibrated(cam)}))
        if self.path == "/api/reid_thr":
            # tune the re-id match bar live. HIGHER = re-recognises the same person more
            # (fewer ids, ids persist across loops) but risks merging different look-alikes.
            # Resets the shared gallery (the engine handler does the same).
            try:
                thr = float(body.get("thr"))
                bb.DESIRED_THR = thr
                bb.REID_REQ_Q.put({"t": "thr", "thr": thr})
                return self._send(200, json.dumps({"thr": thr, "note": "gallery reset"}))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        return self._send(404, "{}")

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        prefix = "/api/face_gallery/"
        if parsed.path.startswith(prefix) and APP is not None:
            name = urllib.parse.unquote(parsed.path[len(prefix):]).strip()
            if not name:
                return self._send(400, json.dumps({"error": "name is required"}))
            try:
                out = APP.plat.delete_face_person(name)
                APP.persist_runtime_config()
                return self._send(200, json.dumps(out))
            except KeyError:
                return self._send(404, json.dumps({"error": "person not found"}))
            except Exception as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
        m = re.match(r"/api/streams/(\d+)", self.path)
        if m and APP is not None:
            return self._send(200, json.dumps(APP.remove_stream(int(m.group(1)))))
        return self._send(404, "{}")


def _load_streams(path: str) -> list:
    txt = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml
        data = yaml.safe_load(txt)
    else:
        data = json.loads(txt)
    return data["streams"] if isinstance(data, dict) else data


def main():
    global APP
    ap = argparse.ArgumentParser()
    ap.add_argument("--streams", required=True, help="YAML/JSON list of {source,camera,face,gait}")
    ap.add_argument("--crops", default=os.environ.get("PLATF_CROPS", "/tmp/platf_crops"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PLATF_PORT", 8090)))
    a = ap.parse_args()
    APP = App(a.crops, streams_path=a.streams)
    APP.start(_load_streams(a.streams))

    def _shutdown(*_):
        # stop stream workers gracefully (stop_ev + join + /dev/shm cleanup) so a restart
        # starts clean -- a hard SIGKILL leaks GPU/shm and stalls the next launch's decode.
        print("[APP] shutting down; stopping workers + pool...", flush=True)
        for w in list(bb.WORKERS.values()):
            try:
                w.stop()
            except Exception:
                pass
        try:
            if bb.REID_STOP is not None:   # stop reid_service + infer_pool device servers
                bb.REID_STOP.set()
        except Exception:
            pass
        time.sleep(0.6)
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    print(f"[APP] Person Intelligence Platform on http://0.0.0.0:{a.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
