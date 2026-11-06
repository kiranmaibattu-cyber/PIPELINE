"""Native-worker recovery signals read by the separate fleet supervisor.

No native API, file IO or worker-owned lock is required by the parent check.
Raw shared scalars avoid a dead process stranding a multiprocessing lock.
"""
from __future__ import annotations

import math
import time


def create_watchdog_channel(context):
    return {
        "heartbeat": context.RawValue("d", time.monotonic()),
        "restart_requested": context.RawValue("i", 0),
        "recovering": context.RawValue("i", 0),
    }


def watchdog_failure(channel, now=None, timeout_seconds=60.0):
    if not channel:
        return None
    if channel["restart_requested"].value:
        return "native_graph_rebuild_requested"
    age = (time.monotonic() if now is None else now) - channel["heartbeat"].value
    if not math.isfinite(age) or age < -1.0 or age > timeout_seconds:
        return "native_worker_watchdog_unresponsive"
    return None
