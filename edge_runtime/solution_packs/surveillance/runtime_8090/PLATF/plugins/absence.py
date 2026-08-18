"""Absence plugin -- expected presence in a zone that goes missing past a timeout.

A zone of kind "absence" is one that SHOULD be occupied (a manned desk, a guard post).
The plugin tracks the last time any person's foot-point was inside it; if the zone then
stays empty longer than its timeout it raises one `absence` alert (cleared when someone
returns). Time-driven, so it fires on `on_tick` with no new observation needed.
"""
from __future__ import annotations

from PLATF.core import Event, Plugin
from PLATF.plugins.zones import observation_in_zone, zones_for


class AbsencePlugin(Plugin):
    name = "absence"

    def __init__(self, zones_cfg, timeout_s: float = 30.0):
        self.cfg = zones_cfg
        self.timeout_s = float(timeout_s)
        self._last_seen: dict = {}   # (camera, zone) -> last t someone was inside
        self._alerted: set = set()
        self._started = False
        self.live_cameras = None      # set by the platform; None only in offline tests

    def process(self, obs, person, ctx):
        foot = obs.foot_point
        if foot is None:
            return
        for z in zones_for(self.cfg, obs.camera)["zones"]:
            if z.get("kind") != "absence":
                continue
            if observation_in_zone(obs, z, self.cfg):
                key = (obs.camera, z["name"])
                self._last_seen[key] = obs.t
                self._alerted.discard(key)   # occupied again -> re-arm

    def _timeout_for(self, camera, zname):
        for z in zones_for(self.cfg, camera)["zones"]:
            if z["name"] == zname and z.get("dwell_s"):
                return float(z["dwell_s"])
        return self.timeout_s

    def on_tick(self, t, ctx):
        if not self._started:
            self._started = True
            # An absence zone that starts empty must still alert. Previously its
            # timer existed only after the first occupant was seen, so permanently
            # empty desks could never trigger—the opposite of the use case.
            live = set(self.live_cameras) if self.live_cameras is not None else None
            if self.cameras is None:
                cameras = live if live is not None else set(self.cfg.get("cameras", {}))
            else:
                cameras = set(self.cameras)
                if live is not None:
                    cameras &= live
            for cam in cameras:
                for z in zones_for(self.cfg, cam)["zones"]:
                    if z.get("kind") == "absence":
                        self._last_seen.setdefault((cam, z["name"]), t)
        if self.live_cameras is not None:
            live = set(self.live_cameras)
            self._last_seen = {k: v for k, v in self._last_seen.items() if k[0] in live}
            self._alerted = {k for k in self._alerted if k[0] in live}
        for (cam, zname), last in list(self._last_seen.items()):
            key = (cam, zname)
            if key in self._alerted:
                continue
            if t - last > self._timeout_for(cam, zname):
                self._alerted.add(key)
                ctx.emit(Event("absence", t, cam, None, zone=zname,
                               payload={"empty_s": round(t - last, 1)}))
