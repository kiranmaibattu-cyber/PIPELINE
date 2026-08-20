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
            "source": "ephemeral_runtime_state",
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
    if snapshot_ref and "snapshot_url" not in event:
        enriched = dict(event)
        enriched["snapshot_ref"] = snapshot_ref
        enriched["snapshot_url"] = _snapshot_url(snapshot_ref)
        enriched["snapshot_content_type"] = _snapshot_content_type(snapshot_ref)
        if isinstance(enriched.get("payload"), dict):
            enriched["payload"] = {
                **enriched["payload"],
                "snapshot_ref": snapshot_ref,
                "snapshot_url": enriched["snapshot_url"],
                "snapshot_content_type": enriched["snapshot_content_type"],
            }
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
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {key: value for key, value in event.items() if key not in reserved}
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

    try:
        while not status.stop_requested:
            if status.child is not None:
                exit_code = status.child.poll()
                if exit_code is not None:
                    status.child_exit_code = int(exit_code)
                    return int(exit_code)
            time.sleep(0.5)
        return 0
    finally:
        status.stop_requested = True
        if status.child and status.child.poll() is None:
            status.child.terminate()
            try:
                status.child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                status.child.kill()
                status.child.wait(timeout=5)
        server.stop()


def _load_plan_status(status: RuntimeStatus) -> None:
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
