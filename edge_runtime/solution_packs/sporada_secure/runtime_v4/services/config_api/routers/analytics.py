"""WebSocket bridge: multiplexes two Redis Streams to one browser socket.

  - `traffic:analytics`  ← AsyncAnalyticsDispatcher (camera_payload, events)
  - `traffic:system`     ← SystemMonitor (RAM/CPU/NPU health)

Both payloads are JSON. The browser distinguishes them by `message_type`
(`camera_analytics` vs `system_health`). The forwarder also keeps the
latest system_health payload in memory so `/api/health/system` (see
routers/health.py) can return a fresh snapshot without waiting for a tick.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Set, Tuple

import redis.asyncio as redis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
# Drop a consumer's worker_metrics if it hasn't published within this window
# (it died / was re-routed), so its cameras stop lingering in the merged view.
WORKER_METRICS_STALE_S = float(os.getenv("WORKER_METRICS_STALE_S", "6"))
ANALYTICS_STREAM = os.getenv("ANALYTICS_REDIS_STREAM", "traffic:analytics")
SYSTEM_STREAM = os.getenv("SYSTEM_REDIS_STREAM", "traffic:system")


class _ConnectionHub:
    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        # Latest payload on traffic:system, kept per message_type so system_health
        # and worker_metrics don't clobber each other (HTTP fallbacks below).
        self._latest_by_type: Dict[str, str] = {}
        # Each consumer process publishes its OWN partial worker_metrics (only its
        # hash-routed cameras). Keep the latest per worker_id so we can merge them
        # into one complete per-camera view instead of showing whichever partial
        # (or empty) record arrived last. worker_id -> (recv_monotonic, record).
        self._metrics_by_worker: Dict[str, Tuple[float, dict]] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._fanout_loop())
        # Send the latest system/metrics snapshots immediately so the Monitor page
        # doesn't have to wait for the next tick.
        for payload in list(self._latest_by_type.values()):
            try:
                await ws.send_text(payload)
            except Exception:
                pass

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    @property
    def latest_system(self) -> Optional[str]:
        return self._latest_by_type.get("system_health")

    @property
    def latest_metrics(self) -> Optional[str]:
        return self._latest_by_type.get("worker_metrics")

    def _merge_worker_metrics(self, rec: Optional[dict]) -> str:
        """Combine per-consumer partial worker_metrics into one complete record.
        Each consumer (CONSUMERS>1) reports only its hash-routed cameras; we keep
        the latest record per worker_id, drop stale ones, and union the per-camera
        lists so the Monitor sees ALL cameras at once instead of a flickering subset."""
        now = time.monotonic()
        if rec is not None:
            wid = str(rec.get("worker_id", "default"))
            self._metrics_by_worker[wid] = (now, rec)
        self._metrics_by_worker = {
            w: (t, r) for w, (t, r) in self._metrics_by_worker.items()
            if now - t <= WORKER_METRICS_STALE_S
        }
        per_camera: Dict[str, dict] = {}
        observed_at: Optional[str] = None
        published = dropped = errors = queue_depth = queue_max = 0
        sinks: list = []
        last_error = None
        # oldest -> newest so the most recent reading per camera wins
        for _, r in sorted(self._metrics_by_worker.values(), key=lambda tr: tr[0]):
            tp = r.get("throughput", {})
            for cam in tp.get("per_camera", []):
                per_camera[cam["name"]] = cam
            observed_at = r.get("observed_at", observed_at)
            em = r.get("emission") or {}
            published += em.get("published", 0) or 0
            dropped += em.get("dropped", 0) or 0
            errors += em.get("errors", 0) or 0
            queue_depth += em.get("queue_depth", 0) or 0
            queue_max = max(queue_max, em.get("queue_max", 0) or 0)
            for s in em.get("sinks", []) or []:
                if s not in sinks:
                    sinks.append(s)
            last_error = em.get("last_error") or last_error
        cams = sorted(per_camera.values(), key=lambda c: c["name"])
        merged = {
            "schema_version": "1.0",
            "message_type": "worker_metrics",
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "throughput": {
                "inferences_per_second": round(sum(c.get("fps", 0.0) for c in cams), 1),
                "cameras": len(cams),
                "per_camera": cams,
            },
            "emission": {
                "sinks": sinks, "queue_depth": queue_depth, "queue_max": queue_max,
                "published": published, "dropped": dropped, "errors": errors,
                "last_error": last_error,
            },
        }
        return json.dumps(merged)

    async def _fanout_loop(self) -> None:
        client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            decode_responses=True,
        )
        last_ids = {ANALYTICS_STREAM: "$", SYSTEM_STREAM: "$"}
        try:
            while True:
                async with self._lock:
                    if not self._connections:
                        return
                response = await client.xread(last_ids, block=2000, count=50)
                if not response:
                    continue
                for stream_name, entries in response:
                    for entry_id, fields in entries:
                        last_ids[stream_name] = entry_id
                        payload = fields.get("payload")
                        if not payload:
                            continue
                        if stream_name == SYSTEM_STREAM:
                            try:
                                rec = json.loads(payload)
                                mtype = rec.get("message_type")
                            except Exception:
                                rec, mtype = None, None
                            if mtype == "worker_metrics":
                                # merge this consumer's partial record with the others
                                payload = self._merge_worker_metrics(rec)
                                self._latest_by_type["worker_metrics"] = payload
                            elif mtype:
                                self._latest_by_type[mtype] = payload
                        await self._broadcast(payload)
        except Exception:  # pragma: no cover
            logger.exception("Redis fanout loop crashed")
        finally:
            await client.close()

    async def _broadcast(self, payload: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)


HUB = _ConnectionHub()


@router.websocket("/api/events/ws")
async def events_ws(websocket: WebSocket) -> None:
    await HUB.connect(websocket)
    try:
        while True:
            # keepalive only — UI never sends to us in Phase 1
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await HUB.disconnect(websocket)
