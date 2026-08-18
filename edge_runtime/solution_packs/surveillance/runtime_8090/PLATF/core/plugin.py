"""Plugin contract + PluginHost -- how every use-case attaches to the platform.

Each use-case (re-id, face, intrusion, loitering, counting, absence, ...) is a
Plugin that consumes TrackObservations + the resolved Person and emits Events. The
host runs the shared observation stream through the plugins IN ORDER: the identity
(re-id) plugin runs first and binds (camera, local_id) -> global Person in the store,
so every downstream plugin receives the resolved Person for free. Plugins never
re-process video and never talk to each other directly -- only through the store
(shared person state) and the event bus (emitted events).
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Optional

from .events import Event, EventBus
from .observation import TrackObservation
from .person import Person, PersonStore


class PluginContext:
    """What a plugin is allowed to touch: shared person state, the bus, config."""

    def __init__(self, store: PersonStore, bus: EventBus, config: Optional[dict] = None):
        self.store = store
        self.bus = bus
        self.config = config or {}

    def emit(self, event: Event):
        self.bus.publish(event)


class Plugin(ABC):
    name: str = "plugin"
    # cameras this plugin runs on. None = all cameras. A set restricts it, so a use-case
    # can be turned on for ch2 but off for ch1 (per-camera selection). The host enforces it.
    cameras: Optional[set] = None

    def setup(self, ctx: PluginContext):
        """One-time init (load indices, read config). Override as needed."""

    @abstractmethod
    def process(self, obs: TrackObservation, person: Optional[Person], ctx: PluginContext):
        """Handle one track observation. `person` is the resolved Person (may be None
        until the identity plugin binds it). Emit events via ctx.emit(...)."""

    def on_tick(self, t: float, ctx: PluginContext):
        """Time-driven checks with no new observation (loiter/absence timers).
        Called once per processed batch with the batch's latest timestamp."""


class PluginHost:
    """Runs the observation stream through the store + ordered plugins."""

    def __init__(self, store: PersonStore, bus: EventBus, plugins: list, config: Optional[dict] = None):
        self.store = store
        self.bus = bus
        self.plugins = list(plugins)          # order matters: identity plugin first
        self.ctx = PluginContext(store, bus, config)
        self._lock = threading.RLock()        # guards plugin list vs runtime toggles
        for p in self.plugins:
            p.setup(self.ctx)

    def has_plugin(self, name: str) -> bool:
        with self._lock:
            return any(p.name == name for p in self.plugins)

    def add_plugin(self, plugin: "Plugin", first: bool = False) -> None:
        """Enable a use-case at runtime. `first` keeps the identity plugin ahead of the
        analytics that depend on the resolved Person. No-op if already present."""
        with self._lock:
            if any(p.name == plugin.name for p in self.plugins):
                return
            plugin.setup(self.ctx)
            if first:
                self.plugins.insert(0, plugin)
            else:
                self.plugins.append(plugin)

    def remove_plugin(self, name: str) -> None:
        with self._lock:
            self.plugins = [p for p in self.plugins if p.name != name]

    def process(self, observations: list) -> None:
        if not observations:
            return
        now = max(o.t for o in observations)
        with self._lock:
            plugins = list(self.plugins)      # snapshot so a toggle mid-batch is safe
        for obs in observations:
            # The Person substrate belongs to tracking, not to the Re-ID use case.
            # Bootstrap every local track before plugins run so face/zones/analytics can
            # operate independently when Re-ID is toggled off. Prefer the authoritative
            # backbone gid; legacy gid-less observations receive a temporary Person.
            gid = self.store.resolve_local(obs.camera, obs.local_id)
            reid_active = any(
                p.name == "reid" and (p.cameras is None or obs.camera in p.cameras)
                for p in plugins
            )
            if gid is None and not reid_active:
                p = (self.store.get_or_create(obs.gid, obs.t)
                     if obs.gid is not None else self.store.mint(obs.t))
                self.store.bind_local(obs.camera, obs.local_id, p.global_id)
            for plugin in plugins:
                # per-camera gate: skip a plugin not enabled for this observation's camera.
                if plugin.cameras is not None and obs.camera not in plugin.cameras:
                    continue
                # Re-resolve before each plugin: Re-ID/mapper work may have merged it.
                gid = self.store.resolve_local(obs.camera, obs.local_id)
                obs.person_id = gid
                person = self.store.get(gid) if gid is not None else None
                plugin.process(obs, person, self.ctx)
            # record the observation into its Person ONCE, after identity is resolved
            # (a first-sight frame binds mid-loop; touching per-plugin would double-log).
            gid = self.store.resolve_local(obs.camera, obs.local_id)
            if gid is not None:
                p = self.store.get(gid)
                if p is not None:
                    p.touch(obs.t, obs.camera, obs.foot_point)
        for plugin in plugins:
            plugin.on_tick(now, self.ctx)
        self.store.prune(now)
