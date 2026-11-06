from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traffic_pilot_runtime import solution_image_entrypoint as runtime  # noqa: E402


NOW = 1_800_000_000.0


def utc(seconds_ago=0):
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).isoformat()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("APEXFABRIC_STATE_DIR", raising=False)
    monkeypatch.setenv("ANALYTICS_EVENT_LOG_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("SENTINEL_CV_HANDOFF_STATUS_PATH", str(tmp_path / "handoff.json"))
    monkeypatch.setattr(runtime.time, "time", lambda: NOW)
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    state = runtime.RuntimeState()
    state.plan = {"cameras": [{"camera_id": name, "fps": 8} for name in ("a", "b")]}
    supervisor = runtime.WorkerSupervisor(tmp_path, tmp_path, state)
    supervisor.process = SimpleNamespace(pid=100, poll=lambda: None)
    supervisor.ready_dir = tmp_path / "ready"
    supervisor.ready_dir.mkdir()
    supervisor.expected_camera_ids = ("a", "b")
    (supervisor.ready_dir / "a.ready").touch()
    supervisor.poll_worker()
    assert state.ready  # Historical initialization alone must not admit traffic.
    return state, metrics


def write_status(metrics, kind, camera="a", **fields):
    payload = {"camera_id": camera, "observed_at": utc(), **fields}
    (metrics / f"{kind}-{camera}.json").write_text(json.dumps(payload))


def healthy_decoder(metrics, camera="a", **fields):
    write_status(metrics, "decoder", camera, **{
        "state": "healthy", "last_frame_at": utc(1), "pid": 200,
        "stream_session_id": f"current-{camera}", **fields,
    })


def get_json(state, path):
    handler = object.__new__(runtime.RuntimeHandler)
    handler.runtime_state = state
    handler.path = path
    handler._json = Mock()
    handler.do_GET()
    args = handler._json.call_args.args
    return args[0], args[1] if len(args) > 1 else 200


def assert_readiness(state, expected):
    ready, status = get_json(state, "/readyz")
    assert status == (200 if expected else 503)
    assert set(ready) == {"ready", "worker_running", "per_camera_state"}
    assert ready["ready"] is expected
    assert ready["worker_running"] is state.worker_running
    health, status = get_json(state, "/healthz")
    assert status == 200
    assert health["ok"] is True
    assert health["liveness"] == "alive"
    assert health["ready"] is expected
    assert state.metrics()["models_ready"] is expected
    assert f"apexfabric_runtime_ready {int(expected)}\n" in runtime._metrics(state)
    return ready, health


@pytest.mark.parametrize("progress", ["none", "zero", "stale", "missing_frame", "failed"])
def test_initialized_fleet_requires_current_progress(fleet, progress):
    state, metrics = fleet
    if progress != "none":
        write_status(metrics, "runtime-camera", processing={
            "decoded_fps": 0, "inference_fps": 0,
            "totals": {"decoded_frames": 100, "inference_frames": 100},
        })
    if progress == "stale":
        healthy_decoder(metrics, last_frame_at=utc(60))
        write_status(metrics, "camera-health", state="healthy", last_reliable_at=utc(60))
    elif progress == "missing_frame":
        healthy_decoder(metrics, last_frame_at=None)
        write_status(metrics, "camera-health", state="healthy", last_reliable_at=utc(1))
    elif progress == "failed":
        healthy_decoder(metrics, state="unavailable", reason="stream_disconnected")
        write_status(metrics, "camera-health", state="healthy", last_reliable_at=utc(1))
    ready, health = assert_readiness(state, False)
    assert len(ready["per_camera_state"]) == 2
    if progress != "none":
        # The reporter is alive even when frame processing has stopped.
        assert health["telemetry"]["cameras"][0]["fresh"] is True


def test_empty_fleet_is_not_ready(fleet):
    state, metrics = fleet
    healthy_decoder(metrics)
    state.plan = {"cameras": []}
    ready, _ = assert_readiness(state, False)
    assert ready["per_camera_state"] == []


def test_fresh_decoder_progress_does_not_require_a_new_analytic_heartbeat(fleet):
    state, metrics = fleet
    healthy_decoder(metrics)
    write_status(metrics, "camera-health", state="healthy", last_reliable_at=utc(25),
                 stream_session_id="current-a", observed_at=utc(25))
    assert_readiness(state, True)


@pytest.mark.parametrize("failed_peer", ["starting", "stale", "process_exited"])
def test_one_healthy_peer_keeps_shared_readiness(fleet, failed_peer):
    state, metrics = fleet
    if failed_peer == "stale":
        healthy_decoder(metrics, last_frame_at=utc(60))
    else:
        write_status(metrics, "camera-supervisor", state=failed_peer, camera_process_pid=200)
    healthy_decoder(metrics, "b")
    ready, _ = assert_readiness(state, True)
    assert ready["per_camera_state"][0]["state"] != "healthy"
    assert ready["per_camera_state"][1]["state"] == "healthy"
    healthy_decoder(metrics, "b", last_frame_at=utc(60))
    assert_readiness(state, False)
    healthy_decoder(metrics, "b")
    assert_readiness(state, True)


@pytest.mark.parametrize("supervisor_state", ["starting", "process_exited"])
@pytest.mark.parametrize("republished", [False, True])
def test_supervisor_invalidates_old_decoder_generation(fleet, supervisor_state, republished):
    state, metrics = fleet
    healthy_decoder(metrics, observed_at=utc(0 if republished else 4), last_frame_at=utc(4))
    write_status(metrics, "camera-health", state="healthy", last_reliable_at=utc(1))
    write_status(metrics, "camera-supervisor", state=supervisor_state,
                 camera_process_pid=200, observed_at=utc(2))
    ready, _ = assert_readiness(state, False)
    assert ready["per_camera_state"][0]["reason"] == (
        "worker_starting" if supervisor_state == "starting" else "camera_process_exited"
    )


def test_wrong_process_cannot_claim_progress_after_new_process_start(fleet):
    state, metrics = fleet
    healthy_decoder(metrics)
    write_status(metrics, "camera-supervisor", state="starting",
                 camera_process_pid=201, observed_at=utc(2))
    assert_readiness(state, False)
    healthy_decoder(metrics, pid=201)
    assert_readiness(state, True)


def test_new_stream_does_not_inherit_old_analytic_failure(fleet):
    state, metrics = fleet
    write_status(metrics, "camera-health", state="unavailable", reason="old_failure",
                 last_reliable_at=utc(2), stream_session_id="old-session")
    write_status(metrics, "camera-supervisor", state="starting",
                 camera_process_pid=200, observed_at=utc(2))
    healthy_decoder(metrics)
    assert_readiness(state, True)
    write_status(metrics, "camera-health", state="unavailable", reason="current_failure",
                 last_reliable_at=utc(1), stream_session_id="current-a")
    assert_readiness(state, False)


@pytest.mark.parametrize("last_reliable_at,expected", [(utc(1), True), (utc(60), False), (None, False)])
def test_analytic_health_fallback_requires_recent_reliable_progress(fleet, last_reliable_at, expected):
    state, metrics = fleet
    write_status(metrics, "camera-health", state="healthy", last_reliable_at=last_reliable_at)
    assert_readiness(state, expected)


@pytest.mark.parametrize("ready,running", [(False, True), (True, False)])
def test_fresh_camera_does_not_override_worker_prerequisites(fleet, ready, running):
    state, metrics = fleet
    healthy_decoder(metrics)
    state.ready, state.worker_running = ready, running
    assert_readiness(state, False)


def test_health_reads_operational_status_once_per_camera(fleet, monkeypatch):
    state, metrics = fleet
    healthy_decoder(metrics)
    status = Mock(wraps=runtime._camera_operational_status)
    monkeypatch.setattr(runtime, "_camera_operational_status", status)
    assert runtime._health_payload(state)["ready"] is True
    assert status.call_count == 2


@pytest.mark.parametrize("ready,running", [(False, True), (False, False), (True, True)])
def test_hot_reload_preserves_worker_prerequisites_and_effective_readiness(fleet, ready, running):
    state, _ = fleet
    state.ready, state.worker_running = ready, running
    reloader = runtime.DesiredStateReloader(SimpleNamespace(secrets_root="/unused"), state, Mock())
    reloader._apply_hot_reload(
        SimpleNamespace(revision=2, content_hash="new-hash"),
        SimpleNamespace(to_dict=lambda: state.plan),
    )
    assert state.ready is ready
    assert state.worker_running is running
    assert state.active_revision == 2
    assert_readiness(state, False)


class FakeSocket:
    """Drive the real HTTP handler without opening a listening socket."""

    def __init__(self, path, error=None, fail_on=1):
        self.request = io.BytesIO(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        self.error = error
        self.fail_on = fail_on
        self.writes = 0
        self.response = bytearray()

    def makefile(self, *args, **kwargs):
        return self.request

    def sendall(self, data):
        self.writes += 1
        if self.error and self.writes == self.fail_on:
            raise self.error("sensitive client detail must not be logged")
        self.response.extend(data)


@pytest.mark.parametrize("path", ["/healthz", "/metrics", "/snapshots/a/frame.jpg"])
@pytest.mark.parametrize("error", [ConnectionResetError, BrokenPipeError])
@pytest.mark.parametrize("fail_on", [1, 2])
def test_http_client_disconnect_is_benign(fleet, tmp_path, caplog, path, error, fail_on):
    state, _ = fleet
    snapshot = tmp_path / "snapshots" / "a" / "frame.jpg"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"jpeg payload")
    connection = FakeSocket(path + "?token=must-not-log", error, fail_on)
    handler_type = type("TestHandler", (runtime.RuntimeHandler,), {"runtime_state": state})
    with caplog.at_level(logging.DEBUG, logger=runtime.logger.name):
        handler = handler_type(connection, ("127.0.0.1", 12345), SimpleNamespace())
    assert handler.close_connection
    assert connection.writes == fail_on
    records = [record for record in caplog.records if getattr(record, "event", "") == "http_client_disconnected"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].reason == error.__name__
    assert records[0].exc_info is None
    assert "must-not-log" not in caplog.text
    assert "sensitive client detail" not in caplog.text


def test_snapshot_response_preserves_headers_and_bytes(fleet, tmp_path):
    state, _ = fleet
    snapshot = tmp_path / "snapshots" / "frame.jpg"
    snapshot.parent.mkdir()
    snapshot.write_bytes(b"jpeg payload")
    connection = FakeSocket("/snapshots/frame.jpg")
    handler_type = type("TestHandler", (runtime.RuntimeHandler,), {"runtime_state": state})
    handler_type(connection, ("127.0.0.1", 12345), SimpleNamespace())
    headers, body = bytes(connection.response).split(b"\r\n\r\n", 1)
    assert b"200 OK" in headers
    assert b"Content-Type: image/jpeg" in headers
    assert b"Content-Length: 12" in headers
    assert b"Cache-Control: no-store" in headers
    assert body == b"jpeg payload"


def test_unexpected_response_errors_remain_visible(fleet):
    state, _ = fleet
    connection = FakeSocket("/readyz", OSError)
    handler_type = type("TestHandler", (runtime.RuntimeHandler,), {"runtime_state": state})
    with pytest.raises(OSError):
        handler_type(connection, ("127.0.0.1", 12345), SimpleNamespace())
