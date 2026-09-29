from __future__ import annotations

from services.worker.pipeline.output_sinks import JsonlFileSink, json_payload
from traffic_pilot_runtime.solution_image_entrypoint import RuntimeState, _tail_events


def _observation(event_id: str) -> dict:
    return {
        "message_id": "message-" + event_id,
        "observed_at": "2026-09-18T12:00:00Z",
        "camera": {"id": "cam1"},
        "events": [{
            "id": event_id,
            "type": "smoke_detected",
            "use_case": "fire_smoke_detection",
            "timestamp": "2026-09-18T12:00:00Z",
        }],
    }


def _follower(monkeypatch, path):
    del monkeypatch
    return _tail_events(RuntimeState(), path, heartbeat_seconds=0.01, poll_seconds=0.001)


def test_connection_starts_at_eof_and_does_not_replay_history(monkeypatch, tmp_path):
    path = tmp_path / "analytics.jsonl"
    sink = JsonlFileSink(str(path), max_bytes=4096)
    sink.publish(_observation("historical"))

    events = _follower(monkeypatch, path)
    assert next(events) is None
    sink.publish(_observation("live"))
    assert next(events)["event_id"] == "live"
    events.close()


def test_reconnect_is_at_most_once_without_replay(monkeypatch, tmp_path):
    path = tmp_path / "analytics.jsonl"
    sink = JsonlFileSink(str(path), max_bytes=4096)
    first = _follower(monkeypatch, path)
    assert next(first) is None
    sink.publish(_observation("delivered-once"))
    assert next(first)["event_id"] == "delivered-once"
    first.close()

    reconnected = _follower(monkeypatch, path)
    assert next(reconnected) is None
    reconnected.close()


def test_connected_follower_continues_across_rotation(monkeypatch, tmp_path):
    path = tmp_path / "analytics.jsonl"
    first_payload = _observation("before-rotation")
    line_size = len((json_payload(first_payload) + "\n").encode("utf-8"))
    sink = JsonlFileSink(str(path), max_bytes=line_size + 1)
    events = _follower(monkeypatch, path)
    assert next(events) is None

    sink.publish(first_payload)
    assert next(events)["event_id"] == "before-rotation"
    sink.publish(_observation("after-rotation"))
    assert next(events)["event_id"] == "after-rotation"

    sink.publish(_observation("second-rotation"))
    assert path.is_file()
    assert (tmp_path / "analytics.jsonl.1").is_file()
    assert len(list(tmp_path.glob("analytics.jsonl.[0-9]*"))) == 1
    assert path.stat().st_size + (tmp_path / "analytics.jsonl.1").stat().st_size < 2 * sink.max_bytes
    events.close()


def test_journal_rejects_single_event_larger_than_rotation_limit(tmp_path):
    path = tmp_path / "analytics.jsonl"
    sink = JsonlFileSink(str(path), max_bytes=8)

    try:
        sink.publish({"larger": "than-eight-bytes"})
    except ValueError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("oversized journal event was accepted")
    assert not path.exists()


def test_preexisting_oversized_journals_are_not_retained(tmp_path):
    path = tmp_path / "analytics.jsonl"
    backup = tmp_path / "analytics.jsonl.1"
    path.write_bytes(b"x" * 32)
    backup.write_bytes(b"y" * 32)
    sink = JsonlFileSink(str(path), max_bytes=16)

    sink.publish({"a": 1})

    assert path.stat().st_size < 16
    assert not backup.exists()
