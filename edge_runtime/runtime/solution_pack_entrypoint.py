"""ApexFabric solution-pack process wrapper.

The copied 8090 and Traffic Pilot runtimes keep their own internal shape. This
wrapper owns the platform-facing contract: liveness, readiness, metrics, and an
analytics event stream, while running the original pack runtime as a child.
"""
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
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from edge_runtime.model_registry.baked import BakedModelValidator
from edge_runtime.model_registry.registry import ModelRegistry
from edge_runtime.runtime.desired_state_reload import (
    DesiredStateReloadError,
    DesiredStateSnapshot,
    DesiredStateWatcher,
    SubprocessGraphCompiler,
)


@dataclass
class RuntimeStatus:
    solution_pack: str
    plan_path: Path
    state_dir: Path
    started_at: float = field(default_factory=time.time)
    child: subprocess.Popen[str] | None = None
    child_exit_code: int | None = None
    plan_loaded: bool = False
    camera_count: int = 0
    revision: int | None = None
    edge_id: str | None = None
    stop_requested: bool = False
    last_error: str | None = None
    metrics_proxy_url: str | None = None
    runtime_api_url: str | None = None
    models_ready: bool = False
    desired_state_path: Path | None = None
    active_desired_hash: str | None = None
    observed_desired_hash: str | None = None
    pending_revision: int | None = None
    reload_state: str = "disabled"
    reload_attempts: int = 0
    reload_applied: int = 0
    reload_rejected: int = 0
    last_reload_at: float | None = None
    last_reload_error: str | None = None

    def healthy(self) -> bool:
        if self.child_exit_code not in (None, 0):
            return False
        return not self.stop_requested

    def ready(self) -> bool:
        if not self.plan_loaded or not self.models_ready:
            return False
        if self.camera_count == 0:
            return True
        return self.child is not None and self.child.poll() is None

    def as_payload(self) -> dict[str, Any]:
        child_running = self.child is not None and self.child.poll() is None
        return {
            "solution_pack": self.solution_pack,
            "edge_id": self.edge_id,
            "revision": self.revision,
            "plan_loaded": self.plan_loaded,
            "models_ready": self.models_ready,
            "camera_count": self.camera_count,
            "configured_cameras": self.camera_count,
            "child_running": child_running,
            "child_exit_code": self.child_exit_code,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "stop_requested": self.stop_requested,
            "last_error": self.last_error,
            "desired_state": {
                "path": str(self.desired_state_path) if self.desired_state_path else None,
                "active_hash": self.active_desired_hash,
                "observed_hash": self.observed_desired_hash,
                "pending_revision": self.pending_revision,
                "reload_state": self.reload_state,
                "reload_attempts": self.reload_attempts,
                "reload_applied": self.reload_applied,
                "reload_rejected": self.reload_rejected,
                "last_reload_at": self.last_reload_at,
                "last_reload_error": self.last_reload_error,
            },
        }


class SolutionPackServer:
    def __init__(self, status: RuntimeStatus, host: str, port: int) -> None:
        self._status = status
        self._server = ThreadingHTTPServer((host, port), self._handler())

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._server.serve_forever, name="apex-api", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        status = self._status

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib API
                if self.path == "/healthz":
                    code = HTTPStatus.OK if status.healthy() else HTTPStatus.INTERNAL_SERVER_ERROR
                    health = "ok" if status.healthy() else "error"
                    self._json(code, {"status": health, **status.as_payload()})
                    return
                if self.path == "/readyz":
                    code = HTTPStatus.OK if status.ready() else HTTPStatus.SERVICE_UNAVAILABLE
                    self._json(code, {"ready": status.ready(), **status.as_payload()})
                    return
                if self.path == "/metrics":
                    self._json(HTTPStatus.OK, _metrics(status))
                    return
                if self.path == "/events":
                    self._events()
                    return
                if self.path.startswith("/snapshots/"):
                    self._snapshot(self.path.removeprefix("/snapshots/"))
                    return
                if _is_runtime_api_path(status, self.path):
                    self._proxy_runtime("GET")
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802 - stdlib API
                if _is_runtime_api_path(status, self.path):
                    self._proxy_runtime("POST")
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_DELETE(self) -> None:  # noqa: N802 - stdlib API
                if _is_runtime_api_path(status, self.path):
                    self._proxy_runtime("DELETE")
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def log_message(self, fmt: str, *args: Any) -> None:
                print(f"apex-api {self.address_string()} {fmt % args}", flush=True)

            def _json(self, code: HTTPStatus, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(int(code))
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _events(self) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                for event in _tail_events(status):
                    try:
                        if event is None:
                            payload = ": heartbeat\n\n"
                        else:
                            payload = "event: analytics\n" + f"data: {json.dumps(event, sort_keys=True)}\n\n"
                        self.wfile.write(payload.encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return

            def _snapshot(self, raw_ref: str) -> None:
                resolved = _resolve_snapshot_path(status, raw_ref)
                if resolved is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "snapshot_not_found"})
                    return
                content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
                try:
                    body = resolved.read_bytes()
                except OSError as exc:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "snapshot_read_failed", "detail": str(exc)})
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _proxy_runtime(self, method: str) -> None:
                proxied = _proxy_runtime_request(status, method, self.path, self.headers, self.rfile)
                self.send_response(proxied["status"])
                for key, value in proxied["headers"].items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(proxied["body"])

        return Handler


def _metrics(status: RuntimeStatus) -> dict[str, Any]:
    payload = {
        "format": "application/json",
        "runtime": status.as_payload(),
        "events": {
            "protocol": "server-sent-events",
            "path": "/events",
        },
        "snapshots": {
            "path_prefix": "/snapshots/",
            "content_types": ["image/jpeg", "image/png"],
            "source": "persistent_state",
        },
    }
    if status.runtime_api_url:
        payload["management_api"] = {
            "source": "runtime_proxy",
            "base_path": "/api",
            "proxied_paths": sorted(_runtime_api_prefixes(status.solution_pack)),
        }
    child_running = status.child is not None and status.child.poll() is None
    if status.metrics_proxy_url and child_running:
        try:
            with urlopen(status.metrics_proxy_url, timeout=0.5) as response:
                proxied = json.loads(response.read().decode("utf-8"))
            payload["runtime_metrics"] = proxied
        except (OSError, URLError, json.JSONDecodeError) as exc:
            payload["metrics_proxy_error"] = str(exc)
    return payload


def _is_runtime_api_path(status: RuntimeStatus, path: str) -> bool:
    if not status.runtime_api_url:
        return False
    parsed = urlsplit(path)
    return any(parsed.path == prefix or parsed.path.startswith(prefix + "/")
               for prefix in _runtime_api_prefixes(status.solution_pack))


def _runtime_api_prefixes(solution_pack: str) -> tuple[str, ...]:
    if solution_pack != "surveillance":
        return ()
    return (
        "/api/alert_history",
        "/api/alerts",
        "/api/audit",
        "/api/calibration",
        "/api/cameras",
        "/api/churn",
        "/api/counting",
        "/api/crop",
        "/api/enrollment",
        "/api/exemplar_crop",
        "/api/face_chip",
        "/api/face_gallery",
        "/api/face_group",
        "/api/history",
        "/api/history_crop",
        "/api/live",
        "/api/mapper",
        "/api/metrics",
        "/api/person",
        "/api/person_audit",
        "/api/persons",
        "/api/search",
        "/api/summary",
        "/api/usecase",
        "/api/usecases",
        "/api/zones",
    )


def _proxy_runtime_request(status: RuntimeStatus, method: str, path: str, headers: Any, body_stream: Any) -> dict[str, Any]:
    child_running = status.child is not None and status.child.poll() is None
    if not child_running:
        return _proxy_payload(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "runtime_not_running", **status.as_payload()},
        )
    assert status.runtime_api_url is not None
    target = status.runtime_api_url.rstrip("/") + path
    body = None
    request_headers = {"Accept": headers.get("Accept", "*/*")}
    content_type = headers.get("Content-Type")
    if content_type:
        request_headers["Content-Type"] = content_type
    if method in ("POST", "PUT", "PATCH"):
        length = int(headers.get("Content-Length", "0") or "0")
        body = body_stream.read(length) if length else b""
    request = Request(target, data=body, method=method, headers=request_headers)
    try:
        with urlopen(request, timeout=10.0) as response:
            return {
                "status": int(response.status),
                "headers": _proxy_response_headers(response.headers),
                "body": response.read(),
            }
    except HTTPError as exc:
        return {
            "status": int(exc.code),
            "headers": _proxy_response_headers(exc.headers),
            "body": exc.read(),
        }
    except (OSError, URLError) as exc:
        return _proxy_payload(
            HTTPStatus.BAD_GATEWAY,
            {"error": "runtime_proxy_failed", "detail": str(exc), **status.as_payload()},
        )


def _proxy_response_headers(headers: Any) -> dict[str, str]:
    allowed = ("Content-Type", "Cache-Control")
    out = {key: headers[key] for key in allowed if headers.get(key)}
    out.setdefault("Content-Type", "application/json")
    out["Cache-Control"] = "no-store"
    return out


def _proxy_payload(code: HTTPStatus, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    return {
        "status": int(code),
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Content-Length": str(len(body)),
        },
        "body": body,
    }


def _tail_events(status: RuntimeStatus):
    paths = [
        status.state_dir / "management_events.jsonl",
        status.state_dir / "events.jsonl",
    ]
    positions = {path: 0 for path in paths}
    while not status.stop_requested:
        emitted = False
        for path in paths:
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as fh:
                fh.seek(positions[path])
                for line in fh:
                    emitted = True
                    yield _enrich_event(status, _parse_event(line))
                positions[path] = fh.tell()
        if not emitted:
            yield None
            time.sleep(5)


def _parse_event(line: str) -> dict[str, Any]:
    try:
        parsed = json.loads(line)
        return parsed if isinstance(parsed, dict) else {"payload": parsed}
    except json.JSONDecodeError:
        return {"payload": line.rstrip("\n")}


def _enrich_event(status: RuntimeStatus, event: dict[str, Any]) -> dict[str, Any]:
    snapshot_ref = _event_snapshot_ref(event)
    snapshot_assets = _event_snapshot_assets(event)
    if (snapshot_ref and "snapshot_url" not in event) or snapshot_assets:
        enriched = dict(event)
        if snapshot_ref:
            enriched["snapshot_ref"] = snapshot_ref
            enriched["snapshot_url"] = _snapshot_url(snapshot_ref)
            enriched["snapshot_content_type"] = _snapshot_content_type(snapshot_ref)
        if snapshot_assets:
            enriched["snapshot_assets"] = snapshot_assets
        if isinstance(enriched.get("payload"), dict):
            payload_updates = {}
            if snapshot_ref:
                payload_updates.update({
                    "snapshot_ref": snapshot_ref,
                    "snapshot_url": enriched["snapshot_url"],
                    "snapshot_content_type": enriched["snapshot_content_type"],
                })
            if snapshot_assets:
                payload_updates["snapshot_assets"] = snapshot_assets
            enriched["payload"] = {**enriched["payload"], **payload_updates}
        event = enriched
    timestamp = (
        event.get("timestamp")
        or event.get("timestamp_utc")
        or event.get("observed_at")
        or _utc_now()
    )
    nested_payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    raw_event_type = str(
        event.get("event_type")
        or event.get("type")
        or nested_payload.get("event_type")
        or nested_payload.get("type")
        or "analytics"
    )
    raw_application = str(
        event.get("application")
        or event.get("app_id")
        or event.get("use_case")
        or nested_payload.get("application")
        or nested_payload.get("app_id")
        or nested_payload.get("use_case")
        or raw_event_type
    )
    application, event_type = _normalize_event_identity(
        status.solution_pack,
        raw_application,
        raw_event_type,
    )
    reserved = {
        "schema_version", "event_id", "timestamp", "timestamp_utc", "observed_at",
        "camera_id", "camera", "solution_pack", "application", "app_id", "use_case",
        "event_type", "type", "payload",
    }
    top_level_payload = {key: value for key, value in event.items() if key not in reserved}
    nested = event.get("payload")
    payload = {**top_level_payload, **nested} if isinstance(nested, dict) else top_level_payload
    global_id = payload.get("global_id") or payload.get("person_id")
    if status.solution_pack == "surveillance" and global_id is not None:
        payload.setdefault("person_ref", f"{status.edge_id or 'unknown-edge'}:{global_id}")
    return {
        "schema_version": "1.0",
        "event_id": str(event.get("event_id") or uuid.uuid4()),
        "timestamp": _rfc3339_utc(timestamp),
        "camera_id": str(event.get("camera_id") or _camera_id(event) or "unknown"),
        "solution_pack": status.solution_pack,
        "application": application,
        "event_type": event_type,
        "payload": _redact_sensitive(payload),
    }


def _normalize_event_identity(
    solution_pack: str,
    application: str,
    event_type: str,
) -> tuple[str, str]:
    """Map copied runtime terminology to the public ApexFabric V1 contract."""
    app_aliases = {
        "surveillance": {
            "identity": "reid",
            "person_merged": "reid",
            "face_recognized": "face_recognition",
            "face_enrolled": "face_recognition",
            "unauthorised": "face_recognition",
            "unauthorized": "face_recognition",
            "intrusion_alert": "intrusion",
            "count": "people_counting",
        },
        "traffic": {
            "plate_detection": "anpr",
            "plate_read": "anpr",
            "license_plate": "anpr",
            "wrong_way_driving_detection": "wrong_way",
            "wrong_way_driving": "wrong_way",
            "parking_violation_detection": "illegal_parking",
            "parking_violation": "illegal_parking",
            "vehicle_count": "vehicle_counting",
            "pedestrian_count": "pedestrian_counting",
        },
    }
    event_aliases = {
        "surveillance": {
            "identity": "identity_event",
            "person_merged": "cross_camera_identity_event",
            "face_recognized": "face_recognized_event",
            "face_enrolled": "face_enrolled_event",
            "unauthorised": "unauthorised_event",
            "unauthorized": "unauthorised_event",
            "intrusion": "intrusion_event",
            "intrusion_alert": "intrusion_event",
            "count": "people_count_event",
            "people_count": "people_count_event",
        },
        "traffic": {
            "anpr": "plate_read_event",
            "plate_read": "plate_read_event",
            "license_plate": "plate_read_event",
            "wrong_way": "wrong_way_event",
            "wrong_way_driving": "wrong_way_event",
            "vehicle_count": "vehicle_count_event",
            "pedestrian_count": "pedestrian_count_event",
            "parking_violation": "illegal_parking_event",
            "illegal_parking": "illegal_parking_event",
        },
    }
    normalized_app = app_aliases.get(solution_pack, {}).get(application, application)
    normalized_event = event_aliases.get(solution_pack, {}).get(event_type, event_type)
    return normalized_app, normalized_event


def _camera_id(event: dict[str, Any]) -> str | None:
    camera = event.get("camera")
    if isinstance(camera, dict):
        value = camera.get("id") or camera.get("name")
        return str(value) if value else None
    return str(camera) if camera else None


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _rfc3339_utc(value: Any) -> str:
    from datetime import datetime, timezone
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return _utc_now()


def _redact_sensitive(value: Any) -> Any:
    sensitive_keys = {"source", "uri", "rtsp_url", "password", "token", "secret"}
    if isinstance(value, dict):
        return {
            str(key): _redact_sensitive(item)
            for key, item in value.items()
            if str(key).lower() not in sensitive_keys
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str) and "://" in value and "@" in value:
        return "[redacted-url]"
    return value


def _event_snapshot_ref(event: dict[str, Any]) -> str | None:
    for key in ("snapshot_ref", "snapshot_path", "image_path", "image_ref", "crop_path", "crop_ref"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = event.get("payload")
    if isinstance(payload, dict):
        return _event_snapshot_ref(payload)
    return None


def _event_snapshot_assets(event: dict[str, Any]) -> dict[str, dict[str, str]]:
    refs = event.get("snapshot_refs")
    if not isinstance(refs, dict):
        payload = event.get("payload")
        refs = payload.get("snapshot_refs") if isinstance(payload, dict) else None
    if not isinstance(refs, dict):
        return {}
    assets = {}
    for name, ref in refs.items():
        if not isinstance(ref, str) or not ref.strip():
            continue
        clean_ref = ref.strip()
        assets[str(name)] = {
            "ref": clean_ref,
            "url": _snapshot_url(clean_ref),
            "content_type": _snapshot_content_type(clean_ref),
        }
    return assets


def _snapshot_url(snapshot_ref: str) -> str:
    ref = _state_relative_ref(snapshot_ref)
    return "/snapshots/" + ref


def _snapshot_content_type(snapshot_ref: str) -> str:
    return mimetypes.guess_type(snapshot_ref)[0] or "image/jpeg"


def _resolve_snapshot_path(status: RuntimeStatus, raw_ref: str) -> Path | None:
    ref = unquote(raw_ref).lstrip("/")
    if not ref:
        return None
    state_root = status.state_dir.resolve()
    candidates = _snapshot_candidates(state_root, ref)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if _is_relative_to(resolved, state_root) and resolved.is_file():
            return resolved
    return None


def _snapshot_candidates(state_root: Path, ref: str) -> tuple[Path, ...]:
    ref_path = Path(ref)
    if ref_path.is_absolute():
        return (ref_path,)
    normalized = Path(_state_relative_ref(ref))
    return (
        state_root / normalized,
        state_root / "snapshots" / normalized,
        state_root / "crops" / normalized,
    )


def _state_relative_ref(snapshot_ref: str) -> str:
    ref_path = Path(snapshot_ref)
    if ref_path.is_absolute():
        parts = ref_path.parts
        if "state" in parts:
            idx = parts.index("state")
            return "/".join(parts[idx + 2:]) if len(parts) > idx + 2 else ref_path.name
        return ref_path.name
    return str(ref_path).replace("\\", "/").lstrip("/")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a solution pack with the ApexFabric API contract")
    parser.add_argument("--solution-pack", required=True)
    parser.add_argument("--runtime-module", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--generated-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--models-root", default="/models")
    parser.add_argument("--api-host", default=os.getenv("APEX_API_HOST", "0.0.0.0"))
    parser.add_argument("--api-port", type=int, default=int(os.getenv("APEX_API_PORT", "8080")))
    parser.add_argument("--runtime-port", type=int)
    parser.add_argument("--runtime-api-url")
    parser.add_argument("--enable-runtime-api-proxy", action="store_true")
    parser.add_argument("--metrics-proxy-url")
    parser.add_argument("--desired-state")
    parser.add_argument(
        "--reload-interval",
        type=float,
        default=float(os.getenv("DESIRED_STATE_RELOAD_INTERVAL", "2")),
    )
    parser.add_argument(
        "--reload-retry-interval",
        type=float,
        default=float(os.getenv("DESIRED_STATE_RELOAD_RETRY_INTERVAL", "10")),
    )
    parser.add_argument(
        "--reload-startup-grace",
        type=float,
        default=float(os.getenv("DESIRED_STATE_RELOAD_STARTUP_GRACE", "3")),
    )
    parser.add_argument(
        "--reload-compile-timeout",
        type=float,
        default=float(os.getenv("DESIRED_STATE_RELOAD_COMPILE_TIMEOUT", "120")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status = RuntimeStatus(
        solution_pack=args.solution_pack,
        plan_path=Path(args.plan),
        state_dir=Path(args.state_dir),
        metrics_proxy_url=args.metrics_proxy_url,
        runtime_api_url=(
            args.runtime_api_url or _runtime_api_url(args.runtime_port)
            if args.enable_runtime_api_proxy
            else None
        ),
        desired_state_path=Path(args.desired_state) if args.desired_state else None,
    )
    status.state_dir.mkdir(parents=True, exist_ok=True)
    _load_plan_status(status)
    if not status.plan_loaded:
        print(f"runtime startup failed: {status.last_error}", file=sys.stderr, flush=True)
        return 2
    try:
        registry_path = Path(__file__).resolve().parents[1] / "model_registry" / "models.yaml"
        registry = ModelRegistry.from_file(registry_path)
        BakedModelValidator(registry, Path(args.models_root)).validate(args.solution_pack)
        status.models_ready = True
    except (OSError, ValueError) as exc:
        status.last_error = str(exc)
        print(f"runtime startup failed: {exc}", file=sys.stderr, flush=True)
        return 2

    server = SolutionPackServer(status, args.api_host, args.api_port)
    server.start()
    print(f"{args.solution_pack} ApexFabric API on http://{args.api_host}:{args.api_port}", flush=True)

    def stop(_signum: int, _frame: Any) -> None:
        status.stop_requested = True
        if status.child and status.child.poll() is None:
            status.child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if status.plan_loaded and status.camera_count > 0:
        status.child = _start_child(args)

    watcher, compiler = _reload_components(args, status)
    next_reload_check = 0.0
    next_reload_retry = 0.0
    last_attempted_hash = None

    try:
        while not status.stop_requested:
            if status.child is not None:
                exit_code = status.child.poll()
                if exit_code is not None:
                    status.child_exit_code = int(exit_code)
                    return int(exit_code)
            now = time.monotonic()
            if watcher is not None and compiler is not None and now >= next_reload_check:
                next_reload_check = now + max(0.25, args.reload_interval)
                try:
                    snapshot = watcher.snapshot()
                    status.observed_desired_hash = snapshot.digest
                    status.pending_revision = snapshot.revision
                    if snapshot.digest == status.active_desired_hash:
                        status.pending_revision = None
                        if status.reload_state != "applying":
                            status.reload_state = "idle"
                    elif snapshot.digest != last_attempted_hash or now >= next_reload_retry:
                        last_attempted_hash = snapshot.digest
                        applied = _apply_desired_state(args, status, snapshot, compiler)
                        if not applied:
                            next_reload_retry = time.monotonic() + max(
                                args.reload_retry_interval, args.reload_interval
                            )
                except DesiredStateReloadError as exc:
                    status.reload_state = "rejected"
                    status.last_reload_error = str(exc)
            time.sleep(0.5)
        return 0
    finally:
        status.stop_requested = True
        _stop_child(status)
        server.stop()


def _load_plan_status(status: RuntimeStatus) -> None:
    status.plan_loaded = False
    try:
        plan = json.loads(status.plan_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        status.last_error = f"plan not found: {status.plan_path}"
        return
    except json.JSONDecodeError as exc:
        status.last_error = f"invalid plan json: {exc}"
        return
    status.plan_loaded = True
    status.camera_count = len(plan.get("cameras") or [])
    status.revision = plan.get("revision")
    status.edge_id = plan.get("edge_id")


def _reload_components(args, status: RuntimeStatus):
    if status.desired_state_path is None or args.reload_interval <= 0:
        return None, None
    watcher = DesiredStateWatcher(status.desired_state_path)
    compiler = SubprocessGraphCompiler(
        solution_pack=status.solution_pack,
        models_root=Path(args.models_root),
        work_root=Path(args.generated_dir).parent / "reloads",
        timeout_seconds=args.reload_compile_timeout,
    )
    try:
        snapshot = watcher.snapshot()
        status.active_desired_hash = snapshot.digest
        status.observed_desired_hash = snapshot.digest
        status.reload_state = "idle"
    except DesiredStateReloadError as exc:
        status.reload_state = "rejected"
        status.last_reload_error = str(exc)
    return watcher, compiler


def _apply_desired_state(
    args,
    status: RuntimeStatus,
    snapshot: DesiredStateSnapshot,
    compiler: SubprocessGraphCompiler,
) -> bool:
    """Compile before disruption, then switch the child with rollback protection."""
    status.reload_attempts += 1
    status.reload_state = "compiling"
    status.last_reload_error = None
    try:
        candidate = compiler.compile(snapshot)
        candidate_revision = int(candidate.payload.get("revision"))
        if status.revision is not None and candidate_revision < status.revision:
            raise DesiredStateReloadError(
                f"desired-state revision {candidate_revision} is older than active revision {status.revision}"
            )
        if candidate.payload.get("solution_pack") != status.solution_pack:
            raise DesiredStateReloadError("compiled plan solution pack does not match the running image")
        previous_plan = status.plan_path.read_bytes()
    except (DesiredStateReloadError, OSError, TypeError, ValueError) as exc:
        _reject_reload(status, snapshot, exc)
        return False

    status.reload_state = "applying"
    previous_hash = status.active_desired_hash
    try:
        _stop_child(status)
        _write_plan_atomically(status.plan_path, candidate.content)
        _load_plan_status(status)
        if not status.plan_loaded:
            raise DesiredStateReloadError(status.last_error or "candidate plan could not be loaded")
        status.child_exit_code = None
        status.child = _start_child(args) if status.camera_count > 0 else None
        if not _child_survived_startup(status.child, args.reload_startup_grace):
            raise DesiredStateReloadError("candidate runtime exited during startup grace period")
    except (DesiredStateReloadError, OSError, subprocess.SubprocessError) as exc:
        _stop_child(status)
        try:
            _write_plan_atomically(status.plan_path, previous_plan)
            _load_plan_status(status)
            status.child_exit_code = None
            status.child = _start_child(args) if status.camera_count > 0 else None
        except (OSError, subprocess.SubprocessError) as rollback_exc:
            status.last_error = f"runtime rollback failed: {rollback_exc}"
        status.active_desired_hash = previous_hash
        _reject_reload(status, snapshot, exc)
        return False

    status.active_desired_hash = snapshot.digest
    status.observed_desired_hash = snapshot.digest
    status.pending_revision = None
    status.reload_state = "idle"
    status.reload_applied += 1
    status.last_reload_at = time.time()
    status.last_reload_error = None
    print(
        f"desired state revision {status.revision} applied without container restart",
        flush=True,
    )
    return True


def _reject_reload(status: RuntimeStatus, snapshot: DesiredStateSnapshot, exc: Exception) -> None:
    status.observed_desired_hash = snapshot.digest
    status.pending_revision = snapshot.revision
    status.reload_state = "rejected"
    status.reload_rejected += 1
    status.last_reload_error = str(exc)
    print(f"desired state revision {snapshot.revision} rejected: {exc}", file=sys.stderr, flush=True)


def _write_plan_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_suffix(path.suffix + ".next")
    candidate.write_bytes(content)
    candidate.replace(path)


def _child_survived_startup(child, grace_seconds: float) -> bool:
    if child is None:
        return True
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        time.sleep(0.1)
    return child.poll() is None


def _stop_child(status: RuntimeStatus, timeout_seconds: float = 20.0) -> None:
    child = status.child
    if child is None:
        return
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    status.child = None


def _start_child(args: argparse.Namespace) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        "-m",
        args.runtime_module,
        "--plan",
        args.plan,
        "--generated-dir",
        args.generated_dir,
        "--state-dir",
        args.state_dir,
        "--models-dir",
        args.models_dir,
    ]
    if args.runtime_port is not None:
        command.extend(["--port", str(args.runtime_port)])
    print("starting runtime child: " + " ".join(command), flush=True)
    return subprocess.Popen(command, text=True)


def _runtime_api_url(runtime_port: int | None) -> str | None:
    if runtime_port is None:
        return None
    return f"http://127.0.0.1:{runtime_port}"


if __name__ == "__main__":
    raise SystemExit(main())
