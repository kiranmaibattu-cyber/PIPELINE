from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .adapter import write_worker_config
from .desired_state import SOLUTION_PACK, DesiredState, DesiredStateValidator, file_hash
from .graph import RuntimePlan, compile_runtime_plan

CONTRACT_EVENT_TYPES = frozenset({
    "plate_read",
    "vehicle_count_per_frame",
    "pedestrian_count_per_frame",
    "smoke_detected",
    "fire_detected",
    "face_seen",
})
CONTRACT_APPLICATIONS = frozenset({
    "anpr",
    "vehicle_counting",
    "pedestrian_counting",
    "fire_smoke_detection",
    "face_recognition",
})
IDENTITY_MAP = {
    "plate_detection": ("anpr", "plate_read"),
    "anpr": ("anpr", "plate_read"),
    "plate_read": ("anpr", "plate_read"),
    "vehicle_count_per_frame": ("vehicle_counting", "vehicle_count_per_frame"),
    "pedestrian_count_per_frame": ("pedestrian_counting", "pedestrian_count_per_frame"),
    "smoke_detected": ("fire_smoke_detection", "smoke_detected"),
    "fire_detected": ("fire_smoke_detection", "fire_detected"),
    "face_recognition": ("face_recognition", "face_seen"),
    "face_seen": ("face_recognition", "face_seen"),
}


class RuntimeState:
    def __init__(self, desired_state_path: str = "/configs/desired_state.json") -> None:
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.desired_state_path = desired_state_path
        self.ready = False
        self.active_revision = 0
        self.active_hash = ""
        self.observed_hash = ""
        self.pending_revision: int | None = None
        self.reload_state = "idle"
        self.reload_attempts = 0
        self.applied_count = 0
        self.rejected_count = 0
        self.last_reload_at: float | None = None
        self.latest_error = ""
        self.plan: dict[str, Any] | None = None
        self.worker_pid: int | None = None
        self.worker_running = False
        self.events: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "ready": self.ready,
                "active_revision": self.active_revision,
                "active_hash": self.active_hash,
                "observed_hash": self.observed_hash,
                "pending_revision": self.pending_revision,
                "reload_state": self.reload_state,
                "reload_attempts": self.reload_attempts,
                "applied_count": self.applied_count,
                "rejected_count": self.rejected_count,
                "last_reload_at": self.last_reload_at,
                "latest_error": self.latest_error,
                "worker_pid": self.worker_pid,
                "worker_running": self.worker_running,
                "plan": self.plan,
            }

    def metrics(self, stop_requested: bool = False) -> dict[str, Any]:
        with self.lock:
            plan = self.plan or {}
            camera_count = len(plan.get("cameras") or [])
            return {
                "solution_pack": SOLUTION_PACK,
                "edge_id": plan.get("edge_id"),
                "revision": self.active_revision or None,
                "plan_loaded": self.plan is not None,
                "models_ready": self.ready,
                "camera_count": camera_count,
                "configured_cameras": camera_count,
                "child_running": self.worker_running,
                "child_exit_code": None,
                "uptime_seconds": max(0.0, time.time() - self.started_at),
                "stop_requested": stop_requested,
                "last_error": self.latest_error or None,
                "desired_state": {
                    "path": self.desired_state_path,
                    "active_hash": self.active_hash or None,
                    "observed_hash": self.observed_hash or None,
                    "pending_revision": self.pending_revision,
                    "reload_state": self.reload_state,
                    "reload_attempts": self.reload_attempts,
                    "reload_applied": self.applied_count,
                    "reload_rejected": self.rejected_count,
                    "last_reload_at": self.last_reload_at,
                    "last_reload_error": self.latest_error or None,
                },
            }

    def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {"observed_at": time.time(), "type": event_type, "payload": payload}
        with self.lock:
            self.events.append(event)
            self.events = self.events[-200:]


class WorkerSupervisor:
    def __init__(self, repo_root: Path, generated_dir: Path, state: RuntimeState) -> None:
        self.repo_root = repo_root
        self.generated_dir = generated_dir
        self.state = state
        self.process: subprocess.Popen | None = None

    def start_or_replace(self, desired: DesiredState, plan: RuntimePlan, cameras_file: Path) -> None:
        env = os.environ.copy()
        env.update({
            "VIDEO_DIR": str(self.generated_dir),
            "OPENVINO_MODELS_DIR": env.get("OPENVINO_MODELS_DIR", str(self.repo_root / "models" / "openvino")),
            "WORKER_CONFIG_PATH": env.get("WORKER_CONFIG_PATH", str(self.repo_root / "config" / "worker.json")),
            "CAMERAS_FILE": str(cameras_file),
            "PYTHONPATH": str(self.repo_root / "services" / "worker"),
            "INFER_FPS": str(max(1, int(min(camera.fps for camera in desired.cameras) if desired.cameras else 1))),
            "ANALYTICS_EVENT_LOG_PATH": env.get("ANALYTICS_EVENT_LOG_PATH", "/state/events/analytics.jsonl"),
            "EDGE_ID": desired.edge_id,
        })
        command = [sys.executable, "-u", str(self.repo_root / "services" / "worker" / "stream_fleet_openvino.py")]
        new_process = subprocess.Popen(command, cwd=str(self.repo_root), env=env)
        time.sleep(float(os.getenv("WORKER_SWAP_GRACE_SECONDS", "2")))
        if new_process.poll() is not None:
            raise RuntimeError(f"new worker exited during startup with code {new_process.returncode}")
        old_process = self.process
        self.process = new_process
        self._stop_process(old_process)
        with self.state.lock:
            self.state.worker_pid = new_process.pid
            self.state.worker_running = True
            self.state.ready = True
            self.state.active_revision = desired.revision
            self.state.active_hash = desired.content_hash
            self.state.plan = plan.to_dict()
            self.state.applied_count += 1
            self.state.latest_error = ""
        self.state.record_event("desired_state_applied", {"revision": desired.revision, "worker_pid": new_process.pid})

    def poll_worker(self) -> None:
        proc = self.process
        running = bool(proc and proc.poll() is None)
        with self.state.lock:
            self.state.worker_running = running
            self.state.worker_pid = proc.pid if running and proc else None
            self.state.ready = running and bool(self.state.plan)

    def stop(self) -> None:
        self._stop_process(self.process)
        self.process = None
        self.poll_worker()

    @staticmethod
    def _stop_process(proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


class DesiredStateReloader(threading.Thread):
    def __init__(self, args, state: RuntimeState, supervisor: WorkerSupervisor) -> None:
        super().__init__(daemon=True)
        self.args = args
        self.state = state
        self.supervisor = supervisor
        self.validator = DesiredStateValidator(Path(args.secrets_root))
        self.stop_requested = threading.Event()
        self.active_desired: DesiredState | None = None

    def run(self) -> None:
        while not self.stop_requested.is_set():
            self._try_reload()
            self.stop_requested.wait(float(self.args.poll_seconds))

    def _try_reload(self) -> None:
        path = Path(self.args.desired_state)
        try:
            observed_hash = file_hash(path)
        except OSError as exc:
            self._reject(f"desired-state file is unreadable: {exc}")
            return
        with self.state.lock:
            self.state.observed_hash = observed_hash
            already_active = observed_hash == self.state.active_hash
        if already_active:
            self.supervisor.poll_worker()
            return
        try:
            desired = self.validator.load(path)
            with self.state.lock:
                self.state.pending_revision = desired.revision
                self.state.reload_state = "compiling"
                self.state.reload_attempts += 1
                if desired.revision < self.state.active_revision:
                    raise ValueError(
                        f"revision {desired.revision} is older than active revision {self.state.active_revision}"
                    )
            plan = compile_runtime_plan(desired)
            plan_path = Path(self.args.plan_dir) / "traffic.runtime_plan.json"
            cameras_path = Path(self.args.generated_dir) / "cameras.generated.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
            write_worker_config(desired, cameras_path)
            with self.state.lock:
                self.state.reload_state = "applying"
            if self._can_hot_reload(desired):
                self._apply_hot_reload(desired, plan)
            else:
                self.supervisor.start_or_replace(desired, plan, cameras_path)
            self.active_desired = desired
            with self.state.lock:
                self.state.pending_revision = None
                self.state.reload_state = "idle"
                self.state.last_reload_at = time.time()
        except Exception as exc:  # noqa: BLE001
            self._reject(str(exc))

    def _can_hot_reload(self, desired: DesiredState) -> bool:
        if self.active_desired is None:
            return False
        snap = self.state.snapshot()
        if not snap["worker_running"]:
            return False
        return _hot_reload_signature(self.active_desired) == _hot_reload_signature(desired)

    def _apply_hot_reload(self, desired: DesiredState, plan: RuntimePlan) -> None:
        with self.state.lock:
            self.state.worker_running = True
            self.state.ready = True
            self.state.active_revision = desired.revision
            self.state.active_hash = desired.content_hash
            self.state.plan = plan.to_dict()
            self.state.applied_count += 1
            self.state.latest_error = ""
        self.state.record_event("desired_state_hot_reloaded", {"revision": desired.revision})

    def _reject(self, reason: str) -> None:
        with self.state.lock:
            if self.state.latest_error == reason:
                return
            self.state.latest_error = reason
            self.state.reload_state = "rejected"
            self.state.rejected_count += 1
            self.state.last_reload_at = time.time()
        self.state.record_event("desired_state_rejected", {"reason": reason})


class RuntimeHandler(BaseHTTPRequestHandler):
    runtime_state: RuntimeState
    _sse_slots: threading.BoundedSemaphore = threading.BoundedSemaphore(
        max(1, int(os.getenv("SSE_MAX_CONNECTIONS", "16")))
    )

    def do_GET(self):
        parsed_path = urlsplit(self.path).path
        if parsed_path == "/healthz":
            self._json({"ok": True})
        elif parsed_path == "/readyz":
            snap = self.runtime_state.snapshot()
            self._json({"ready": bool(snap["ready"]), "worker_running": bool(snap["worker_running"])}, 200 if snap["ready"] else 503)
        elif parsed_path == "/metrics":
            self._json(_metrics(self.runtime_state))
        elif parsed_path == "/events":
            self._events()
        elif parsed_path.startswith("/snapshots/"):
            self._snapshot(parsed_path.removeprefix("/snapshots/"))
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        return

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _events(self) -> None:
        if not self._acquire_sse_slot():
            self._json({"error": "too_many_connections"}, 503)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            for event in _tail_events(self.runtime_state, Path(os.getenv("ANALYTICS_EVENT_LOG_PATH", "/state/events/analytics.jsonl"))):
                try:
                    if event is None:
                        self.wfile.write(b": heartbeat\n\n")
                    else:
                        self.wfile.write((f"id: {event['event_id']}\n").encode("utf-8"))
                        self.wfile.write(b"event: analytics\n")
                        self.wfile.write(("data: " + json.dumps(event, sort_keys=True) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        finally:
            self._release_sse_slot()

    def _acquire_sse_slot(self) -> bool:
        return RuntimeHandler._sse_slots.acquire(blocking=False)

    def _release_sse_slot(self) -> None:
        RuntimeHandler._sse_slots.release()

    def _snapshot(self, raw_ref: str) -> None:
        resolved = _resolve_snapshot_path(raw_ref)
        if resolved is None:
            self._json({"error": "snapshot_not_found"}, 404)
            return
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        try:
            body = resolved.read_bytes()
        except OSError as exc:
            self._json({"error": "snapshot_read_failed", "detail": str(exc)}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    payloads = []
    for line in lines:
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return payloads


def _metrics(state: RuntimeState) -> dict[str, Any]:
    return {
        "format": "application/json",
        "runtime": state.metrics(),
        "events": {
            "protocol": "server-sent-events",
            "path": "/events",
            "delivery": "at-most-once",
            "start_position": "eof",
            "historical_replay": False,
            "heartbeat_seconds": 15.0,
        },
        "snapshots": {
            "path_prefix": "/snapshots/",
            "content_types": ["image/jpeg", "image/png"],
            "source": "persistent_state",
        },
    }


def _hot_reload_signature(desired: DesiredState) -> tuple[Any, ...]:
    """Fields that require a worker restart when changed.

    Per-camera geometry/config is intentionally excluded so zone-only updates
    can be applied by the running worker without creating a second OpenVINO
    process.
    """
    return (
        desired.edge_id,
        tuple(
            (
                camera.camera_id,
                camera.name,
                camera.source,
                camera.fps,
                camera.apps,
            )
            for camera in desired.cameras
        ),
    )


def _tail_events(
    state: RuntimeState,
    path: Path,
    heartbeat_seconds: float = 15.0,
    poll_seconds: float = 0.25,
):
    del state  # Kept in the signature for compatibility with existing callers.
    heartbeat_seconds = max(0.1, heartbeat_seconds)
    poll_seconds = max(0.01, poll_seconds)
    next_heartbeat = time.monotonic() + heartbeat_seconds
    stream = None
    initial_open = True
    try:
        while True:
            emitted = False
            if stream is None:
                try:
                    stream = path.open("r", encoding="utf-8")
                    stream.seek(0, os.SEEK_END if initial_open else os.SEEK_SET)
                except FileNotFoundError:
                    stream = None
                initial_open = False

            if stream is not None:
                while True:
                    line_start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        stream.seek(line_start)
                        break
                    for event in _events_from_jsonl_line(line):
                        emitted = True
                        yield event

                try:
                    path_stat = path.stat()
                    stream_stat = os.fstat(stream.fileno())
                    if path_stat.st_ino != stream_stat.st_ino:
                        stream.close()
                        stream = None
                    elif path_stat.st_size < stream.tell():
                        stream.seek(0)
                except FileNotFoundError:
                    # A rename-based rotation briefly removes the active path.
                    # Keep the old descriptor open so its final records are drained.
                    pass

            now = time.monotonic()
            if now >= next_heartbeat:
                next_heartbeat = now + heartbeat_seconds
                yield None
            elif not emitted:
                time.sleep(min(poll_seconds, next_heartbeat - now))
    finally:
        if stream is not None:
            stream.close()

def _events_from_jsonl_line(line: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, dict):
        return []
    if isinstance(raw.get("events"), list):
        return [
            event for event in (
                _normalize_worker_event(raw, item) for item in raw["events"] if isinstance(item, dict)
            )
            if event is not None
        ]
    event = _normalize_worker_event(raw, raw)
    return [] if event is None else [event]


def _normalize_management_event(event: dict[str, Any]) -> dict[str, Any]:
    timestamp = _utc_timestamp(float(event.get("observed_at") or time.time()))
    event_type = str(event.get("type") or "runtime_event")
    return {
        "schema_version": "1.0",
        "event_id": f"runtime:{timestamp}:{event_type}",
        "timestamp": timestamp,
        "camera_id": "runtime",
        "solution_pack": SOLUTION_PACK,
        "application": "runtime",
        "event_type": event_type,
        "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
    }


def _normalize_worker_event(envelope: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    observed_at = event.get("timestamp") or event.get("observed_at") or envelope.get("observed_at")
    camera = envelope.get("camera") if isinstance(envelope.get("camera"), dict) else {}
    camera_id = str(envelope.get("camera_id") or camera.get("id") or camera.get("name") or "unknown")
    raw_application = str(event.get("use_case") or event.get("application") or event.get("app_id") or event.get("event_type") or event.get("type") or "analytics")
    raw_event_type = str(event.get("event_type") or event.get("type") or raw_application)
    identity = IDENTITY_MAP.get(raw_application) or IDENTITY_MAP.get(raw_event_type)
    if not identity:
        return None
    application, normalized_type = identity
    if application not in CONTRACT_APPLICATIONS or normalized_type not in CONTRACT_EVENT_TYPES:
        return None
    payload = dict(event)
    for key in ("id", "timestamp", "observed_at", "use_case", "application", "app_id", "event_type", "type"):
        payload.pop(key, None)
    _attach_snapshot_refs(payload)
    _fix_snapshot_urls(payload)
    objects = payload.get("objects")
    if isinstance(objects, list):
        payload["count"] = {"total": len(objects)}
    if normalized_type in {"vehicle_count_per_frame", "pedestrian_count_per_frame"}:
        payload.pop("subject", None)
    _normalize_contract_bboxes(payload)
    _strip_none(payload)
    return {
        "schema_version": "1.0",
        "event_id": str(event.get("id") or envelope.get("message_id") or f"{camera_id}:{application}:{observed_at}"),
        "timestamp": str(observed_at or _utc_timestamp(time.time())),
        "camera_id": camera_id,
        "solution_pack": SOLUTION_PACK,
        "application": application,
        "event_type": normalized_type,
        "payload": payload,
    }


def _attach_snapshot_refs(payload: dict[str, Any]) -> None:
    snapshot = payload.pop("snapshot", None)
    if not isinstance(snapshot, dict):
        details = payload.get("details")
        if isinstance(details, dict):
            details.pop("snapshot_path", None)
        return
    ref = snapshot.get("ref") or _snapshot_ref_from_path(snapshot.get("path"))
    if not ref:
        return
    payload["snapshot_ref"] = ref
    payload["snapshot_url"] = _snapshot_url(ref)
    payload["snapshot_content_type"] = snapshot.get("content_type") or "image/jpeg"
    payload["snapshot_assets"] = {
        "event_frame": {
            "ref": ref,
            "url": _snapshot_url(ref),
            "content_type": snapshot.get("content_type") or "image/jpeg",
        }
    }
    details = payload.get("details")
    if isinstance(details, dict):
        details.pop("snapshot_path", None)


def _fix_snapshot_urls(payload: dict[str, Any]) -> None:
    """Rewrite every snapshot URL to the canonical single-prefix
    '/snapshots/<camera-id>/<filename>' form required by the contract
    (forbidden form: '/snapshots/snapshots/...')."""
    ref = payload.get("snapshot_ref")
    if isinstance(ref, str):
        payload["snapshot_url"] = _snapshot_url(ref)
    assets = payload.get("snapshot_assets")
    if isinstance(assets, dict):
        for asset in assets.values():
            if not isinstance(asset, dict):
                continue
            asset_ref = asset.get("ref")
            if isinstance(asset_ref, str):
                asset["url"] = _snapshot_url(asset_ref)


def _normalize_contract_bboxes(value: Any) -> Any:
    if isinstance(value, dict):
        bbox = value.get("bbox")
        if isinstance(bbox, dict):
            value["bbox"] = {key: bbox[key] for key in ("x1", "y1", "x2", "y2") if key in bbox}
        for item in value.values():
            _normalize_contract_bboxes(item)
    elif isinstance(value, list):
        for item in value:
            _normalize_contract_bboxes(item)
    return value


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        for key in list(value):
            item = _strip_none(value[key])
            if item is None:
                value.pop(key, None)
            else:
                value[key] = item
    elif isinstance(value, list):
        value[:] = [item for item in (_strip_none(item) for item in value) if item is not None]
    return value


def _snapshot_ref_from_path(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    state_root = Path(os.getenv("APEXFABRIC_STATE_DIR") or os.getenv("APEXFABRIC_STATE_ROOT", "/state")).resolve()
    try:
        resolved = Path(path).resolve()
        rel = resolved.relative_to(state_root)
        return str(rel).replace(os.sep, "/")
    except (OSError, ValueError):
        return None


def _snapshot_url(ref: str) -> str:
    cleaned = ref.lstrip("/")
    if cleaned.startswith("snapshots/"):
        cleaned = cleaned[len("snapshots/"):]
    return "/snapshots/" + cleaned


def _resolve_snapshot_path(raw_ref: str) -> Path | None:
    state_root = Path(os.getenv("APEXFABRIC_STATE_DIR") or os.getenv("APEXFABRIC_STATE_ROOT", "/state")).resolve()
    ref = unquote(raw_ref).lstrip("/")
    candidates = []
    if ref.startswith("snapshots/"):
        candidates.append(state_root / ref)
        candidates.append(state_root / ref[len("snapshots/"):])
    else:
        candidates.append(state_root / "snapshots" / ref)
        candidates.append(state_root / ref)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if candidate != state_root and state_root not in resolved.parents:
            continue
        if resolved.is_file():
            return resolved
    return None


def _utc_timestamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run traffic-pilot as an ApexFabric-style solution image")
    parser.add_argument("--repo-root", default=os.getenv("TRAFFIC_PILOT_ROOT", "/opt/traffic-pilot"))
    parser.add_argument("--desired-state", default=os.getenv("DESIRED_STATE_PATH", "/configs/desired_state.json"))
    parser.add_argument("--secrets-root", default=os.getenv("APEXFABRIC_SECRETS_ROOT", "/run/secrets/apexfabric"))
    parser.add_argument("--generated-dir", default=os.getenv("APEXFABRIC_GENERATED_DIR", "/tmp/apexfabric/generated/traffic-pilot"))
    parser.add_argument("--plan-dir", default=os.getenv("APEXFABRIC_PLAN_DIR", "/plans"))
    parser.add_argument("--host", default=os.getenv("APEX_API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("APEX_API_PORT", "8080")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("DESIRED_STATE_RELOAD_INTERVAL", "2")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = RuntimeState(args.desired_state)
    supervisor = WorkerSupervisor(Path(args.repo_root), Path(args.generated_dir), state)
    reloader = DesiredStateReloader(args, state, supervisor)

    def _shutdown(signum, frame):
        reloader.stop_requested.set()
        supervisor.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    reloader.start()
    RuntimeHandler.runtime_state = state
    server = ThreadingHTTPServer((args.host, args.port), RuntimeHandler)
    print(f"traffic-pilot solution API listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        reloader.stop_requested.set()
        supervisor.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
