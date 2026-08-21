"""Analytics use-case plugins -- intrusion, loitering, people-counting.

These are the CHEAP plugins the platform plan calls for: they need only a tracked
person + a zone/line + time, all of which the observation stream already carries
(foot-point + the re-id person binding done by the ReIDPlugin that runs first). Each is
a Plugin on the same substrate as re-id; each emits typed Events onto the bus.

They key on the resolved person id when available (so an alert follows a person across a
tracker break), else on a camera-local track key.
"""
from __future__ import annotations

import time

from PLATF.core import Event, Plugin
from PLATF.plugins.zones import observation_in_zone, observation_line_side, zones_for


def _who(obs):
    # `local_id` is the engine's stable camera-local id (survives short tracker
    # breaks). Global gids can merge/change while someone dwells or crosses a line,
    # which must not reset the timer/crossing state.
    return f"{obs.camera}:{obs.local_id}"


class IntrusionPlugin(Plugin):
    name = "intrusion"

    def __init__(self, zones_cfg):
        self.cfg = zones_cfg
        self._active = set()   # (who, camera, zone) currently flagged inside
        self._outside = {}     # consecutive outside observations (boundary debounce)

    def process(self, obs, person, ctx):
        foot = obs.foot_point
        if foot is None:
            return
        who = _who(obs)
        for z in zones_for(self.cfg, obs.camera)["zones"]:
            if z["kind"] != "intrusion":
                continue
            key = (who, obs.camera, z["name"])
            inside = observation_in_zone(obs, z, self.cfg)
            if inside and key not in self._active:
                self._active.add(key)
                self._outside.pop(key, None)
                ctx.emit(Event("intrusion", obs.t, obs.camera, obs.person_id,
                               zone=z["name"], payload={"who": str(who)}))
            elif inside:
                self._outside.pop(key, None)
            elif key in self._active:
                self._outside[key] = self._outside.get(key, 0) + 1
                if self._outside[key] >= 2:
                    self._active.discard(key)
                    self._outside.pop(key, None)


class LoiteringPlugin(Plugin):
    name = "loitering"

    def __init__(self, zones_cfg):
        self.cfg = zones_cfg
        self._entry = {}       # (who, camera, zone) -> first time inside
        self._alerted = set()

    def process(self, obs, person, ctx):
        foot = obs.foot_point
        if foot is None:
            return
        who = _who(obs)
        for z in zones_for(self.cfg, obs.camera)["zones"]:
            if z["kind"] != "loiter":
                continue
            key = (who, obs.camera, z["name"])
            if observation_in_zone(obs, z, self.cfg):
                t0 = self._entry.setdefault(key, obs.t)
                dwell = obs.t - t0
                if dwell >= z["dwell_s"] and key not in self._alerted:
                    self._alerted.add(key)
                    ctx.emit(Event("loitering", obs.t, obs.camera, obs.person_id,
                                   zone=z["name"],
                                   payload={"who": str(who), "dwell_s": round(dwell, 1)}))
            else:
                self._entry.pop(key, None)
                self._alerted.discard(key)


class CountingPlugin(Plugin):
    name = "counting"

    def __init__(self, zones_cfg):
        self.cfg = zones_cfg
        self._side = {}        # (who, camera, line) -> last signed side
        self.tallies = {}      # camera -> line -> {"in": n, "out": n}
        self._last_seen = {}   # camera -> who -> (event timestamp, monotonic timestamp)
        self._occupancy = {}   # last emitted current count per camera
        self._active_ttl_s = 2.0

    def _tally(self, camera, line):
        return self.tallies.setdefault(camera, {}).setdefault(line, {"in": 0, "out": 0})

    def process(self, obs, person, ctx):
        foot = obs.foot_point
        if foot is None:
            return
        who = _who(obs)
        lines = zones_for(self.cfg, obs.camera)["lines"]
        if not lines:
            self._last_seen.setdefault(obs.camera, {})[who] = (obs.t, time.monotonic())
            return
        for ln in lines:
            s = observation_line_side(obs, ln, self.cfg)
            if s is None:
                continue
            key = (who, obs.camera, ln["name"])
            prev = self._side.get(key)
            self._side[key] = s
            if prev is None or (prev > 0) == (s > 0) or s == 0:
                continue
            # crossed. "right" side of a->b is line_side < 0.
            new_right = s < 0
            enters = new_right if ln["in_side"] == "right" else (not new_right)
            direction = "in" if enters else "out"
            self._tally(obs.camera, ln["name"])[direction] += 1
            ctx.emit(Event("count", obs.t, obs.camera, obs.person_id, zone=ln["name"],
                           payload={"mode": "line_crossing", "who": str(who),
                                    "direction": direction,
                                    "tally": dict(self._tally(obs.camera, ln["name"]))}))

    def on_tick(self, t, ctx):
        """Without a configured line, publish current tracked-person occupancy."""
        self._publish_occupancy(time.monotonic(), ctx)

    def on_idle(self, ctx):
        self._publish_occupancy(time.monotonic(), ctx)

    def _publish_occupancy(self, now_mono, ctx):
        for camera, seen in list(self._last_seen.items()):
            active = {
                who: last_seen for who, last_seen in seen.items()
                if now_mono - last_seen[1] <= self._active_ttl_s
            }
            self._last_seen[camera] = active
            count = len(active)
            if self._occupancy.get(camera) == count:
                continue
            self._occupancy[camera] = count
            newest = max(seen.values(), default=(0.0, now_mono))
            event_t = newest[0] + max(0.0, now_mono - newest[1])
            ctx.emit(Event(
                "count", event_t, camera, None,
                payload={"mode": "occupancy", "count": count},
            ))
