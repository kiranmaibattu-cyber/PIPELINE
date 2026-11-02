"""Bounded Sentry V8 CV-to-delivery-agent package handoff.

The fleet parent owns exactly one scheduler. Camera processes submit complete
logical packages through dedicated multiprocessing pipes; no camera owns an
outbox, retry queue, sequence allocator, credential, or Management uploader.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import heapq
import http.client
import json
import logging
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any
import uuid


logger = logging.getLogger(__name__)

MAX_RETAINED_PACKAGES = 8
MAX_RETAINED_BYTES = 256 * 1024 * 1024
MAX_REPLACEABLE_PACKAGES = 4
MAX_REPLACEABLE_BYTES = 128 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_IN_FLIGHT = 4
REQUEST_TIMEOUT_SECONDS = 30.0
RETRY_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0, 4.0)

PRIORITY_CRITICAL = 0
PRIORITY_DURABLE = 1
PRIORITY_IDENTITY = 2
PRIORITY_REPLACEABLE = 3

CRITICAL_EVENTS = {"fire_smoke_suspected", "fire_smoke_cleared"}
DURABLE_EVENTS = {
    "zone_entry", "zone_exit", "line_cross", "plate_read", "plate_outcome",
    "track_lost", "presence_recovered", "presence_ended_unknown",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def package_digest(manifest: dict[str, Any], evidence: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(manifest))
    for part_name, body in evidence:
        digest.update(b"\0")
        digest.update(part_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(body)
    return digest.hexdigest()


def _observed_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _semantic_key(event: dict[str, Any]) -> tuple[Any, ...] | None:
    event_type = event.get("event_type")
    if event_type == "scene_sample" and event.get("trigger") == "interval":
        return ("scene", event.get("camera_id"), event.get("use_case_id"),
                event.get("scope"), event.get("zone_id"))
    if event_type == "zone_count":
        return ("count", event.get("camera_id"), event.get("use_case_id"),
                event.get("object_type"), event.get("zone_id"))
    if event_type == "object_present" and event.get("reason") == "periodic":
        return ("presence", event.get("camera_id"), event.get("use_case_id"),
                event.get("presence_id"))
    if event_type == "camera_health":
        return ("health", event.get("camera_id"), event.get("state"),
                event.get("reason"))
    return None


class V8HandoffClient:
    """Camera-process endpoint for the one fleet-parent scheduler."""

    def __init__(self, connection, deployment_id: str, camera_id: str) -> None:
        self.connection = connection
        self.deployment_id = deployment_id
        self.camera_id = camera_id
        self._last_health: tuple[str, str] | None = None
        self._last_counts: dict[tuple[Any, ...], int] = {}

    def close(self) -> None:
        try:
            self.connection.close()
        except OSError:
            pass

    @staticmethod
    def metrics_summary() -> dict[str, Any]:
        """Compatibility view used only to suppress optional scene work early."""
        path = Path(os.getenv(
            "SENTINEL_CV_HANDOFF_STATUS_PATH",
            "/tmp/sentinel/generated/v8_handoff_status.json",
        ))
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        return {"outbox_bytes": int(status.get("retained_bytes", 0))}

    def persist(
        self,
        observation: dict[str, Any],
        evidence: list[tuple[dict[str, Any], bytes]],
        embeddings: list[dict[str, Any]] | None = None,
        scenes: list[dict[str, Any]] | None = None,
    ) -> bool:
        del scenes
        if observation.get("deployment_id") != self.deployment_id:
            raise ValueError("observation deployment_id does not match V8 runtime")
        observation_id = str(observation["observation_id"])
        evidence_items: list[dict[str, Any]] = []
        evidence_parts: list[tuple[str, bytes]] = []
        evidence_ids: set[str] = set()
        for index, (metadata, raw_body) in enumerate(evidence):
            body = bytes(raw_body)
            if metadata.get("observation_id") != observation_id:
                raise ValueError("evidence parent does not match observation")
            evidence_id = str(metadata["evidence_id"])
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            if metadata.get("sha256") != digest or metadata.get("size_bytes") != len(body):
                raise ValueError("evidence metadata does not match exact bytes")
            part_name = f"evidence_{index}"
            evidence_ids.add(evidence_id)
            evidence_items.append({"metadata": metadata, "part_name": part_name})
            evidence_parts.append((part_name, body))
        embeddings = list(embeddings or [])
        if set(observation.get("evidence_ids") or []) != evidence_ids:
            raise ValueError("observation evidence_ids do not match package evidence")
        if set(observation.get("embedding_ids") or []) != {
            str(item["embedding_id"]) for item in embeddings
        }:
            raise ValueError("observation embedding_ids do not match package embeddings")
        for embedding in embeddings:
            if embedding.get("observation_id") != observation_id:
                raise ValueError("embedding parent does not match observation")
            if embedding.get("evidence_id") not in evidence_ids:
                raise ValueError("embedding references evidence outside package")

        manifest = {
            "protocol_version": "1.0",
            "package_id": str(uuid.uuid4()),
            "deployment_id": self.deployment_id,
            "observation": observation,
            "evidence": evidence_items,
            "embeddings": embeddings,
        }
        priority, replaceable = self._priority(observation)
        request = {
            "manifest": manifest,
            "evidence_parts": evidence_parts,
            "digest": package_digest(manifest, evidence_parts),
            "size_bytes": len(canonical_json_bytes(manifest))
            + sum(len(name.encode("utf-8")) + len(body) + 2
                  for name, body in evidence_parts),
            "priority": priority,
            "replaceable": replaceable,
            "semantic_key": _semantic_key(observation),
            "observed_timestamp": _observed_timestamp(observation.get("observed_at", "")),
            "camera_id": self.camera_id,
        }
        if request["size_bytes"] > MAX_REQUEST_BYTES:
            logger.error("V8 handoff package exceeds 64 MiB", extra={
                "event": "delivery_handoff_rejected", "reason": "request_too_large",
                "package_id": manifest["package_id"], "size_bytes": request["size_bytes"],
            })
            return False
        try:
            self.connection.send(request)
            response = self.connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            logger.error("V8 handoff scheduler connection failed: %s", exc, extra={
                "event": "delivery_handoff_rejected", "reason": "scheduler_unavailable",
                "package_id": manifest["package_id"],
            })
            return False
        return bool(response.get("accepted"))

    def _priority(self, event: dict[str, Any]) -> tuple[int, bool]:
        event_type = event.get("event_type")
        if event_type in CRITICAL_EVENTS:
            return PRIORITY_CRITICAL, False
        if event_type == "camera_health":
            current = (str(event.get("state")), str(event.get("reason")))
            transition = self._last_health != current
            self._last_health = current
            return (PRIORITY_CRITICAL, False) if transition else (PRIORITY_REPLACEABLE, True)
        if event_type in DURABLE_EVENTS:
            return PRIORITY_DURABLE, False
        if event_type == "object_present":
            if event.get("reason") == "periodic":
                return PRIORITY_REPLACEABLE, True
            return PRIORITY_DURABLE, False
        if event_type == "scene_sample":
            if event.get("trigger") == "interval":
                return PRIORITY_REPLACEABLE, True
            return PRIORITY_DURABLE, False
        if event_type == "zone_count":
            key = _semantic_key(event)
            value = int(event.get("count", 0))
            unchanged = key in self._last_counts and self._last_counts[key] == value
            self._last_counts[key] = value
            return (PRIORITY_REPLACEABLE, True) if unchanged else (PRIORITY_DURABLE, False)
        if event_type == "person_feature_sample":
            return PRIORITY_IDENTITY, False
        return PRIORITY_DURABLE, False


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _multipart(request: dict[str, Any]) -> tuple[bytes, str]:
    boundary = "sentry-v8-" + request["manifest"]["package_id"].replace("-", "")
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"manifest\"\r\n"
        "Content-Type: application/json\r\n\r\n".encode("ascii"),
        canonical_json_bytes(request["manifest"]), b"\r\n",
    ]
    for part_name, body in request["evidence_parts"]:
        chunks.extend([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{part_name}\"; "
            f"filename=\"{part_name}.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("ascii"),
            body, b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


@dataclass
class _Retained:
    request: dict[str, Any]
    cancelled: bool = False
    released: bool = False
    submitted: bool = False


class V8HandoffScheduler:
    """Single bounded scheduler shared by every camera in a deployment."""

    def __init__(self, camera_connections: dict[str, Any], socket_path: str,
                 status_path: str | Path) -> None:
        self.camera_connections = dict(camera_connections)
        self.socket_path = socket_path
        self.status_path = Path(status_path)
        self.stop_requested = threading.Event()
        self.condition = threading.Condition()
        self.heap: list[tuple[int, float, str, _Retained]] = []
        self.pending_by_key: dict[tuple[Any, ...], _Retained] = {}
        self.retained: set[int] = set()
        self.retained_count = 0
        self.retained_bytes = 0
        self.replaceable_count = 0
        self.replaceable_bytes = 0
        self.counters = {
            "admitted": 0, "durable": 0, "duplicate": 0, "backpressure": 0,
            "coalesced": 0, "exhausted": 0, "protocol_errors": 0,
        }
        self.last_error: str | None = None
        self.dispatcher = threading.Thread(target=self._dispatch, daemon=True,
                                           name="v8-handoff-admission")
        self.workers = [
            threading.Thread(target=self._worker, daemon=True, name=f"v8-handoff-{index}")
            for index in range(MAX_IN_FLIGHT)
        ]

    def start(self) -> "V8HandoffScheduler":
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.dispatcher.start()
        for worker in self.workers:
            worker.start()
        self._snapshot()
        return self

    @property
    def alive(self) -> bool:
        return self.dispatcher.is_alive() and all(worker.is_alive() for worker in self.workers)

    def close(self) -> None:
        self.stop_requested.set()
        with self.condition:
            self.condition.notify_all()
        self.dispatcher.join(timeout=2)
        for worker in self.workers:
            worker.join(timeout=2)
        for connection in self.camera_connections.values():
            try:
                connection.close()
            except OSError:
                pass

    def _dispatch(self) -> None:
        from multiprocessing.connection import wait

        connections = list(self.camera_connections.values())
        while not self.stop_requested.is_set():
            try:
                ready = wait(connections, timeout=0.25)
            except (OSError, ValueError):
                if not self.stop_requested.is_set():
                    logger.exception("V8 handoff admission wait failed")
                return
            for connection in ready:
                try:
                    request = connection.recv()
                    accepted, reason = self._admit(request)
                    connection.send({"accepted": accepted, "reason": reason})
                except (EOFError, BrokenPipeError, OSError):
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.exception("V8 handoff admission failed")
                    try:
                        connection.send({"accepted": False, "reason": str(exc)[:120]})
                    except (BrokenPipeError, OSError):
                        pass

    def _admit(self, request: dict[str, Any]) -> tuple[bool, str]:
        size = int(request["size_bytes"])
        replaceable = bool(request["replaceable"])
        with self.condition:
            key = tuple(request["semantic_key"]) if request.get("semantic_key") else None
            if replaceable and key is not None:
                previous = self.pending_by_key.get(key)
                if previous is not None and not previous.submitted and not previous.cancelled:
                    previous.cancelled = True
                    self._release(previous)
                    self.counters["coalesced"] += 1

            while not replaceable and not self._fits(size, False):
                if not self._evict_replaceable():
                    break
            if not self._fits(size, replaceable):
                self.counters["backpressure"] += 1
                self.last_error = "delivery_backpressure"
                self._snapshot_locked()
                return False, "delivery_backpressure"

            retained = _Retained(request)
            self.retained.add(id(retained))
            self.retained_count += 1
            self.retained_bytes += size
            if replaceable:
                self.replaceable_count += 1
                self.replaceable_bytes += size
                if key is not None:
                    self.pending_by_key[key] = retained
            heapq.heappush(
                self.heap,
                (int(request["priority"]), float(request["observed_timestamp"]),
                 str(request["manifest"]["package_id"]), retained),
            )
            self.counters["admitted"] += 1
            self.condition.notify()
            self._snapshot_locked()
            return True, "admitted"

    def _fits(self, size: int, replaceable: bool) -> bool:
        if self.retained_count + 1 > MAX_RETAINED_PACKAGES:
            return False
        if self.retained_bytes + size > MAX_RETAINED_BYTES:
            return False
        if replaceable and self.replaceable_count + 1 > MAX_REPLACEABLE_PACKAGES:
            return False
        if replaceable and self.replaceable_bytes + size > MAX_REPLACEABLE_BYTES:
            return False
        return True

    def _evict_replaceable(self) -> bool:
        candidates = [
            item[3] for item in self.heap
            if item[3].request["replaceable"] and not item[3].submitted
            and not item[3].cancelled
        ]
        if not candidates:
            return False
        victim = max(candidates, key=lambda item: (
            int(item.request["priority"]), float(item.request["observed_timestamp"])
        ))
        victim.cancelled = True
        self._release(victim)
        self.counters["coalesced"] += 1
        return True

    def _take(self) -> _Retained | None:
        with self.condition:
            while not self.stop_requested.is_set():
                while self.heap:
                    _, _, _, retained = heapq.heappop(self.heap)
                    if retained.cancelled:
                        continue
                    retained.submitted = True
                    key = retained.request.get("semantic_key")
                    if key is not None:
                        normalized = tuple(key)
                        if self.pending_by_key.get(normalized) is retained:
                            self.pending_by_key.pop(normalized, None)
                    return retained
                self.condition.wait(timeout=0.25)
        return None

    def _worker(self) -> None:
        while not self.stop_requested.is_set():
            retained = self._take()
            if retained is None:
                continue
            try:
                outcome = self._deliver(retained.request)
                with self.condition:
                    self.counters[outcome] = int(self.counters.get(outcome, 0)) + 1
                    self.last_error = None
            except Exception as exc:  # noqa: BLE001
                logger.error("V8 package handoff failed: %s", exc, extra={
                    "event": "delivery_handoff_exhausted",
                    "package_id": retained.request["manifest"]["package_id"],
                    "camera_id": retained.request["camera_id"],
                })
                with self.condition:
                    self.counters["exhausted"] += 1
                    self.last_error = str(exc)[:256]
            finally:
                with self.condition:
                    self._release(retained)
                    self._snapshot_locked()

    def _deliver(self, request: dict[str, Any]) -> str:
        body, content_type = _multipart(request)
        if len(body) > MAX_REQUEST_BYTES:
            raise RuntimeError("multipart request exceeds 64 MiB")
        last_error = "handoff failed"
        for attempt in range(6):
            if attempt:
                self.stop_requested.wait(RETRY_DELAYS_SECONDS[attempt - 1])
            connection = _UnixHTTPConnection(self.socket_path, REQUEST_TIMEOUT_SECONDS)
            try:
                connection.request("POST", "/v1/packages", body=body, headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                })
                response = connection.getresponse()
                response_body = response.read(MAX_REQUEST_BYTES + 1)
                payload = json.loads(response_body or b"{}")
            except (OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
                last_error = f"{type(exc).__name__}:{exc}"
                continue
            finally:
                connection.close()

            if response.status in (200, 201):
                expected = "duplicate" if response.status == 200 else "durable"
                self._validate_ack(payload, request["manifest"], expected)
                return expected
            error = payload.get("error") if isinstance(payload, dict) else None
            retryable = bool((error or {}).get("retryable"))
            code = str((error or {}).get("code") or f"http_{response.status}")
            last_error = f"{response.status}:{code}"
            if response.status in (429, 503) and retryable:
                continue
            with self.condition:
                self.counters["protocol_errors"] += 1
            raise RuntimeError(last_error)
        raise RuntimeError(last_error)

    @staticmethod
    def _validate_ack(payload: dict[str, Any], manifest: dict[str, Any],
                      expected_status: str) -> None:
        if payload.get("status") != expected_status:
            raise RuntimeError("delivery-agent acknowledgement status mismatch")
        if payload.get("package_id") != manifest["package_id"]:
            raise RuntimeError("delivery-agent package_id mismatch")
        if payload.get("deployment_id") != manifest["deployment_id"]:
            raise RuntimeError("delivery-agent deployment_id mismatch")
        expected = [
            ("observation", manifest["observation"]["observation_id"]),
            *(("evidence", item["metadata"]["evidence_id"])
              for item in manifest["evidence"]),
            *(("embedding", item["embedding_id"]) for item in manifest["embeddings"]),
        ]
        rows = payload.get("sequences") or []
        actual = [(item.get("object_type"), item.get("object_id")) for item in rows]
        sequences = [item.get("sequence") for item in rows]
        if actual != expected or not sequences or sequences != list(
            range(int(sequences[0]), int(sequences[0]) + len(sequences))
        ):
            raise RuntimeError("delivery-agent sequence acknowledgement mismatch")

    def _release(self, retained: _Retained) -> None:
        if retained.released:
            return
        retained.released = True
        request = retained.request
        self.retained.discard(id(retained))
        self.retained_count = max(0, self.retained_count - 1)
        self.retained_bytes = max(0, self.retained_bytes - int(request["size_bytes"]))
        if request["replaceable"]:
            self.replaceable_count = max(0, self.replaceable_count - 1)
            self.replaceable_bytes = max(
                0, self.replaceable_bytes - int(request["size_bytes"])
            )

    def _snapshot(self) -> None:
        with self.condition:
            self._snapshot_locked()

    def _snapshot_locked(self) -> None:
        payload = {
            "schema_version": "1.0",
            "scope": "cv_handoff_scheduler",
            "observed_at": time.time(),
            "socket_path": self.socket_path,
            "retained_packages": self.retained_count,
            "retained_bytes": self.retained_bytes,
            "replaceable_packages": self.replaceable_count,
            "replaceable_bytes": self.replaceable_bytes,
            "maximum_retained_packages": MAX_RETAINED_PACKAGES,
            "maximum_retained_bytes": MAX_RETAINED_BYTES,
            "maximum_in_flight": MAX_IN_FLIGHT,
            "counters": dict(self.counters),
            "last_error": self.last_error,
            "scheduler_running": self.alive if self.dispatcher.ident else True,
        }
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.status_path)


def create_handoff_channels(context, camera_ids: list[str]):
    parent: dict[str, Any] = {}
    children: dict[str, Any] = {}
    for camera_id in camera_ids:
        parent_connection, child_connection = context.Pipe(duplex=True)
        parent[camera_id] = parent_connection
        children[camera_id] = child_connection
    return parent, children
