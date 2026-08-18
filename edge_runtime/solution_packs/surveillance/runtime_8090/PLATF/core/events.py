"""Event + EventBus -- the platform's nervous system.

Every plugin (re-id, intrusion, loitering, counting, absence, ...) EMITS events;
the dashboard, alerts, storage, and the 2D map SUBSCRIBE. Thin in-proc pub/sub so
the backbone stays decoupled from consumers (and can later be swapped for a real
message broker at enterprise scale without touching plugins).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Event:
    type: str                        # "identity", "intrusion", "loiter", "count_in", ...
    t: float
    camera: Optional[str] = None
    person_id: Optional[int] = None
    zone: Optional[str] = None
    payload: dict = field(default_factory=dict)
    snapshot_ref: Optional[str] = None   # path/key into PLATF/cache snapshots

    def as_dict(self) -> dict:
        return {"type": self.type, "t": self.t, "camera": self.camera,
                "person_id": self.person_id, "zone": self.zone,
                "payload": self.payload, "snapshot_ref": self.snapshot_ref}


class EventBus:
    """Topic pub/sub. Subscribe to a specific event type or "*" for all.

    Callbacks run synchronously on publish() by default (cheap, ordered). A slow
    subscriber must offload its own work; a raising subscriber is isolated so it
    cannot break the emitter or sibling subscribers."""

    ALL = "*"

    def __init__(self):
        self._lock = threading.RLock()
        self._subs: dict[str, list[Callable[[Event], None]]] = {}
        self._history: list[Event] = []
        self._history_cap = 1000

    def subscribe(self, topic: str, cb: Callable[[Event], None]):
        with self._lock:
            self._subs.setdefault(topic, []).append(cb)

    def publish(self, event: Event):
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_cap:
                del self._history[0]
            targets = list(self._subs.get(event.type, ())) + list(self._subs.get(self.ALL, ()))
        for cb in targets:
            try:
                cb(event)
            except Exception:
                pass   # a bad subscriber never breaks the emitter

    def recent(self, n: int = 100, type: Optional[str] = None) -> list:
        with self._lock:
            evs = [e for e in self._history if type is None or e.type == type]
            return evs[-n:]
