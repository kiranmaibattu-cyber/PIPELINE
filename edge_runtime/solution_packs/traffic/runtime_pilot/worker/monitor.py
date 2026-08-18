"""Background system monitor.

Publishes one record per `interval` seconds to Redis stream `traffic:system`
(separate stream from `traffic:analytics`). The UI bridge in
services/config-api/routers/analytics.py multiplexes both streams over the
same WebSocket so the Overview page can render RAM/CPU/NPU live without
adding a second connection.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import psutil

from hardware_check import Topology, detect_topology
from pipeline.output_sinks import json_payload

logger = logging.getLogger(__name__)

SYSTEM_STREAM = os.getenv("SYSTEM_REDIS_STREAM", "traffic:system")
SYSTEM_MAXLEN = int(os.getenv("SYSTEM_REDIS_MAXLEN", "1000"))


class SystemMonitor:
    def __init__(
        self,
        redis_client=None,
        interval: float = 5.0,
        stream: str = SYSTEM_STREAM,
        maxlen: int = SYSTEM_MAXLEN,
    ):
        self.redis = redis_client
        self.interval = max(1.0, float(interval))
        self.stream = stream
        self.maxlen = maxlen
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc = psutil.Process()
        self._topology = detect_topology()
        psutil.cpu_percent(interval=None)  # prime; first sample is always 0

    @classmethod
    def from_env(cls, interval: float = 5.0) -> "SystemMonitor":
        try:
            import redis
        except ImportError:
            logger.warning("redis package unavailable; SystemMonitor will log to stdout only")
            return cls(redis_client=None, interval=interval)

        try:
            client = redis.Redis(
                host=os.getenv("REDIS_HOST", "redis"),
                port=int(os.getenv("REDIS_PORT", "6379")),
                password=os.getenv("REDIS_PASSWORD") or None,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
                decode_responses=True,
            )
            client.ping()
            return cls(redis_client=client, interval=interval)
        except Exception as exc:
            logger.warning("SystemMonitor cannot reach Redis (%s); logging to stdout only", exc)
            return cls(redis_client=None, interval=interval)

    def start(self) -> "SystemMonitor":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="system-monitor", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                payload = self._sample(self._topology)
                self._publish(payload)
            except Exception:
                logger.exception("system monitor tick failed")

    def _sample(self, topology: Topology) -> dict:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        with self._proc.oneshot():
            rss_mb = self._proc.memory_info().rss / (1024 * 1024)
            threads = self._proc.num_threads()
            try:
                open_fds = self._proc.num_fds()
            except AttributeError:
                open_fds = -1  # Windows
        cpu_pct = psutil.cpu_percent(interval=None)
        return {
            "schema_version": "1.0",
            "message_type": "system_health",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "host": {
                "ram_used_mb": round((virtual.total - virtual.available) / 1024 / 1024, 1),
                "ram_total_mb": round(virtual.total / 1024 / 1024, 1),
                "ram_pct": virtual.percent,
                "cpu_pct": cpu_pct,
                "swap_pct": swap.percent,
            },
            "worker": {
                "rss_mb": round(rss_mb, 1),
                "threads": threads,
                "open_fds": open_fds,
            },
            "decode": {
                "backend": topology.decoder_backend,
                "device": topology.decode_device,
                "available": topology.decode_available,
            },
            "npu": {
                "backend": topology.detector_backend,
                "device": topology.npu_device,
                "available": topology.npu_available,
                "models_loaded": topology.models_resident,
            },
        }

    def _publish(self, payload: dict) -> None:
        if self.redis is None:
            logger.info(
                "system: ram=%s%% cpu=%s%% rss=%sMB threads=%s",
                payload["host"]["ram_pct"],
                payload["host"]["cpu_pct"],
                payload["worker"]["rss_mb"],
                payload["worker"]["threads"],
            )
            return
        self.redis.xadd(
            self.stream,
            {"payload": json_payload(payload)},
            maxlen=self.maxlen,
            approximate=True,
        )


class WorkerMetricsMonitor:
    """Publishes a `worker_metrics` heartbeat to `traffic:system` so the operator
    UI can show inference throughput and emission health. Reuses the cumulative
    counters the AsyncAnalyticsDispatcher already tracks; per-camera fps is the
    delta of each camera's `sequence` between ticks. Shares the stream (and the
    UI WebSocket bridge) with SystemMonitor — the browser routes by message_type.
    """

    def __init__(
        self,
        dispatcher,
        redis_client=None,
        interval: float = 2.0,
        stream: str = SYSTEM_STREAM,
        maxlen: int = SYSTEM_MAXLEN,
    ):
        self.dispatcher = dispatcher
        self.redis = redis_client
        self.interval = max(0.5, float(interval))
        self.stream = stream
        self.maxlen = maxlen
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last: dict[str, tuple[int, float]] = {}  # camera -> (sequence, monotonic_t)

    @classmethod
    def from_env(cls, dispatcher, interval: float = 2.0) -> "WorkerMetricsMonitor":
        try:
            import redis
        except ImportError:
            logger.warning("redis package unavailable; WorkerMetricsMonitor will log to stdout only")
            return cls(dispatcher, redis_client=None, interval=interval)
        try:
            client = redis.Redis(
                host=os.getenv("REDIS_HOST", "redis"),
                port=int(os.getenv("REDIS_PORT", "6379")),
                password=os.getenv("REDIS_PASSWORD") or None,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
                decode_responses=True,
            )
            client.ping()
            return cls(dispatcher, redis_client=client, interval=interval)
        except Exception as exc:
            logger.warning("WorkerMetricsMonitor cannot reach Redis (%s); logging to stdout only", exc)
            return cls(dispatcher, redis_client=None, interval=interval)

    def start(self) -> "WorkerMetricsMonitor":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="worker-metrics", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._publish(self._sample())
            except Exception:
                logger.exception("worker metrics tick failed")

    def _sample(self) -> dict:
        stats = self.dispatcher.stats()
        now = time.monotonic()
        per_camera = []
        total_fps = 0.0
        live = set()
        for name, sequence in stats["sequence_by_camera"].items():
            live.add(name)
            fps = 0.0
            prev = self._last.get(name)
            if prev is not None:
                dt = now - prev[1]
                if dt > 0:
                    fps = max(0.0, (sequence - prev[0]) / dt)
            self._last[name] = (sequence, now)
            per_camera.append({"name": name, "fps": round(fps, 2), "sequence": sequence})
            total_fps += fps
        # forget cameras that stopped reporting so stale fps doesn't linger
        for name in list(self._last):
            if name not in live:
                self._last.pop(name, None)
        per_camera.sort(key=lambda item: item["name"])
        return {
            "schema_version": "1.0",
            "message_type": "worker_metrics",
            # With CONSUMERS>1 each consumer process publishes its OWN partial
            # worker_metrics (only its hash-routed cameras). Stamp the publisher so
            # the UI bridge merges per-camera across consumers instead of showing
            # whichever partial record (or empty consumer) arrived last.
            "worker_id": str(os.getpid()),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "throughput": {
                "inferences_per_second": round(total_fps, 1),
                "cameras": len(per_camera),
                "per_camera": per_camera,
            },
            "emission": {
                "sinks": stats["sinks"],
                "queue_depth": stats["queue_depth"],
                "queue_max": stats["queue_max"],
                "published": stats["published"],
                "dropped": stats["dropped"],
                "errors": stats["errors"],
                "last_error": stats["last_error"],
            },
        }

    def _publish(self, payload: dict) -> None:
        if self.redis is None:
            tp = payload["throughput"]
            em = payload["emission"]
            logger.info(
                "metrics: %.1f inf/s x%d cams | sinks=%s published=%d dropped=%d errors=%d",
                tp["inferences_per_second"], tp["cameras"], ",".join(em["sinks"]) or "-",
                em["published"], em["dropped"], em["errors"],
            )
            return
        self.redis.xadd(
            self.stream,
            {"payload": json_payload(payload)},
            maxlen=self.maxlen,
            approximate=True,
        )
