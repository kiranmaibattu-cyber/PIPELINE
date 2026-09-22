"""Health endpoints.

`/api/health/system` returns the most recent system_health payload the
WebSocket bridge has seen. Lets the UI render values on first paint
without waiting for the next 5 s Redis tick.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .analytics import HUB

router = APIRouter()


@router.get("/api/health/system")
def system_health():
    payload = HUB.latest_system
    if not payload:
        raise HTTPException(503, "no system_health record yet")
    return json.loads(payload)


@router.get("/api/metrics")
def worker_metrics():
    payload = HUB.latest_metrics
    if not payload:
        raise HTTPException(503, "no worker_metrics record yet")
    return json.loads(payload)


@router.get("/api/health/live")
def live():
    return {"ok": True}
