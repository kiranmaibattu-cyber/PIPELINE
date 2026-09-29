from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from services.worker.pipeline.analytics import TrafficAnalyticsStage
from services.worker.pipeline.output_sinks import JsonlFileSink, build_analytics_sink, simple_event, sink_kinds
from services.worker.pipeline.processor_config import normalize_runtime_analytics
from services.worker.pipeline.types import Detection, FramePacket


def _det(cls="car", bbox=None, track_id=1, hits=2):
    return Detection(
        bbox=bbox or [40, 40, 60, 60],
        class_id=2 if cls != "pedestrian" else 0,
        class_name=cls,
        confidence=0.9,
        model_name="vehicle",
        metadata={"track_id": track_id, "track_hits": hits},
    )


def _packet(index, det):
    return FramePacket(index=index, name="cam1", frame=np.zeros((100, 100, 3), dtype=np.uint8), detections=[det])


def _run_two(stage, first, second):
    p1 = _packet(1, first)
    p2 = _packet(2, second)
    stage.process([p1])
    stage.process([p2])
    return p1, p2


def test_vehicle_counting_line_mode_counts_crossing_once():
    line = {"id": "line-main", "shape": "line", "points": [{"x": 0.1, "y": 0.5}, {"x": 0.9, "y": 0.5}]}
    cam = {"runtime_analytics": {"vehicle_counting": {"lines": [line], "zones": []}}}
    stage = TrafficAnalyticsStage({"cam1": cam})

    _, p2 = _run_two(stage, _det(bbox=[40, 20, 60, 40]), _det(bbox=[40, 60, 60, 80]))

    cumulative = [e for e in p2.analytics_events if not e["type"].endswith(("_per_frame", "_snapshot"))]
    per_frame = [e for e in p2.analytics_events if e["type"].endswith(("_per_frame", "_snapshot"))]
    assert len(cumulative) == 1
    assert cumulative[0]["type"] == "vehicle_count"
    assert cumulative[0]["geometry"]["id"] == "line-main"
    assert cumulative[0]["value"] == 1
    assert len(per_frame) == 1
    assert per_frame[0]["type"] == "vehicle_count_per_frame"
    assert per_frame[0]["value"] == 1


def test_line_mode_has_priority_over_zone_when_both_exist():
    line = {"id": "line-main", "shape": "line", "points": [{"x": 0.1, "y": 0.5}, {"x": 0.9, "y": 0.5}]}
    zone = {"id": "zone-main", "shape": "polygon", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]}
    cam = {"runtime_analytics": {"vehicle_counting": {"lines": [line], "zones": [zone]}}}
    stage = TrafficAnalyticsStage({"cam1": cam})

    _, p2 = _run_two(stage, _det(bbox=[40, 40, 60, 60]), _det(bbox=[42, 40, 62, 60]))

    cumulative = [e for e in p2.analytics_events if not e["type"].endswith(("_per_frame", "_snapshot"))]
    per_frame = [e for e in p2.analytics_events if e["type"].endswith(("_per_frame", "_snapshot"))]
    assert cumulative == []
    assert len(per_frame) == 1
    assert per_frame[0]["value"] == 1  # per-frame counts whole frame inside zone (1 vehicle)
    state_geometry = p2.analytics_state["use_cases"]["vehicle_counting"]["geometry"]
    assert [item["geometry"]["type"] for item in state_geometry] == ["line", "zone"]
    assert state_geometry[0]["count"] == 0


def test_vehicle_counting_zone_mode_counts_stable_track_once():
    zone = {"id": "zone-main", "shape": "polygon", "points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]}
    cam = {"runtime_analytics": {"vehicle_counting": {"lines": [], "zones": [zone]}}}
    stage = TrafficAnalyticsStage({"cam1": cam})

    _, p2 = _run_two(stage, _det(bbox=[40, 40, 60, 60]), _det(bbox=[42, 40, 62, 60]))
    p3 = _packet(3, _det(bbox=[44, 40, 64, 60]))
    stage.process([p3])

    cum_p2 = [e for e in p2.analytics_events if not e["type"].endswith(("_per_frame", "_snapshot"))]
    assert len(cum_p2) == 1
    assert cum_p2[0]["geometry"]["id"] == "zone-main"
    assert cum_p2[0]["value"] == 1
    # per-frame still present every frame
    assert any(e["type"] == "vehicle_count_per_frame" and e["value"] == 1 for e in p2.analytics_events)
    cum_p3 = [e for e in p3.analytics_events if not e["type"].endswith(("_per_frame", "_snapshot"))]
    assert cum_p3 == []
    assert any(e["type"] == "vehicle_count_per_frame" for e in p3.analytics_events)


def test_pedestrian_counting_whole_frame_default_counts_once():
    cam = {"runtime_analytics": {"pedestrian_counting": {"lines": [], "zones": []}}}
    stage = TrafficAnalyticsStage({"cam1": cam})

    _, p2 = _run_two(stage, _det(cls="pedestrian", bbox=[10, 10, 30, 40]), _det(cls="pedestrian", bbox=[12, 10, 32, 40]))

    cum = [e for e in p2.analytics_events if not e["type"].endswith(("_per_frame", "_snapshot"))]
    assert len(cum) == 1
    assert cum[0]["type"] == "pedestrian_count"
    assert cum[0]["geometry"]["id"] == "zone:whole_frame"
    assert any(e["type"] == "pedestrian_count_per_frame" and e["value"] == 1 for e in p2.analytics_events)
    state_geometry = p2.analytics_state["use_cases"]["pedestrian_counting"]["geometry"]
    assert state_geometry[0]["geometry"]["name"] == "whole_frame"
    assert state_geometry[0]["count"] == 1


def test_vehicle_counting_whole_frame_default_counts_once():
    cam = {"runtime_analytics": {"vehicle_counting": {"lines": [], "zones": []}}}
    stage = TrafficAnalyticsStage({"cam1": cam})

    _, p2 = _run_two(stage, _det(bbox=[10, 10, 30, 40]), _det(bbox=[12, 10, 32, 40]))

    cumulative = [e for e in p2.analytics_events if not e["type"].endswith(("_per_frame", "_snapshot"))]
    assert len(cumulative) == 1
    assert cumulative[0]["type"] == "vehicle_count"
    assert cumulative[0]["geometry"]["id"] == "zone:whole_frame"
    assert any(e["type"] == "vehicle_count_per_frame" and e["value"] == 1 for e in p2.analytics_events)


def test_processor_config_accepts_plain_counting_line_or_zone():
    line = {"shape": "line", "points": [{"x": 0.1, "y": 0.5}, {"x": 0.9, "y": 0.5}]}
    zone = {"shape": "polygon", "points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]}

    normalized_line = normalize_runtime_analytics({"analytics": {"vehicle_counting": {"enabled": True, "lines": [line], "zones": [zone]}}})
    normalized_zone = normalize_runtime_analytics({"analytics": {"pedestrian_counting": {"enabled": True, "lines": [], "zones": [zone]}}})

    assert normalized_line["vehicle_counting"]["lines"] == [line]
    assert normalized_line["vehicle_counting"]["zones"] == [zone]
    assert normalized_zone["pedestrian_counting"]["zones"] == [zone]


def test_smoke_fire_alert_writes_snapshot_and_output_payload(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    stage = TrafficAnalyticsStage({"cam1": {"runtime_analytics": {"fire_smoke_detection": {"enabled": True}}}})
    frame = np.full((80, 120, 3), 127, dtype=np.uint8)
    detection = Detection(
        bbox=[10, 12, 50, 60],
        class_id=1,
        class_name="smoke",
        confidence=0.82,
        model_name="smoke_fire",
    )
    packet = FramePacket(index=7, name="cam1", frame=frame, detections=[detection])

    stage.process([packet])

    assert len(packet.analytics_events) == 1
    event = packet.analytics_events[0]
    snapshot = event["snapshot"]
    assert event["type"] == "smoke_detected"
    assert snapshot["format"] == "jpg"
    assert snapshot["frame_index"] == 7
    assert snapshot["bbox"] == [10, 12, 50, 60]
    assert snapshot["ref"].endswith("smoke_detected-7.jpg")
    assert snapshot["url"] == "/snapshots/" + snapshot["ref"].removeprefix("snapshots/")
    assert snapshot["content_type"] == "image/jpeg"
    payload = simple_event(event)
    assert payload["snapshot_ref"] == snapshot["ref"]
    assert payload["snapshot_url"] == snapshot["url"]
    assert payload["snapshot_assets"]["event_frame"]["url"] == snapshot["url"]
    assert "/snapshots/snapshots/" not in payload["snapshot_url"]
    assert "snapshot" not in payload

def test_analytics_sink_has_no_redis_default(monkeypatch):
    monkeypatch.delenv("ANALYTICS_REDIS_TAP", raising=False)
    sink = build_analytics_sink(
        {"json_streaming": {"enabled": True, "outputs": []}},
        redis_host="redis",
        redis_port=6379,
        stream_key="traffic:analytics",
    )

    assert sink is None

def test_analytics_sink_writes_local_event_log_when_configured(tmp_path, monkeypatch):
    event_log = tmp_path / "events" / "analytics.jsonl"
    monkeypatch.setenv("ANALYTICS_EVENT_LOG_PATH", str(event_log))
    sink = build_analytics_sink(
        {"json_streaming": {"enabled": True, "outputs": []}},
        redis_host="redis",
        redis_port=6379,
    )

    assert isinstance(sink, JsonlFileSink)
    assert sink_kinds(sink) == ["jsonl"]
    sink.publish({"message_type": "camera_observation", "events": [{"type": "smoke_detected"}]})
    assert '"smoke_detected"' in event_log.read_text(encoding="utf-8")


def test_count_snapshot_throttled_to_interval_not_every_frame(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    cam = {"runtime_analytics": {"vehicle_counting": {"lines": [], "zones": []}}}
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    stage = TrafficAnalyticsStage({"cam1": cam})

    p1 = _packet(1, _det(bbox=[20, 20, 60, 60]))
    p2 = _packet(2, _det(bbox=[22, 20, 62, 60]))
    stage.process([p1])
    stage.process([p2])

    snapshots = [e for e in p1.analytics_events + p2.analytics_events if e["type"] == "vehicle_count_snapshot"]
    assert len(snapshots) == 1  # throttled: only the first packet in the interval
    event = snapshots[0]
    assert event["value"] == 1
    assert event["snapshot"]["format"] == "jpg"
    assert event["snapshot"]["frame_index"] == 1
    assert event["snapshot"]["bbox"] == [0, 0, 100, 100]  # whole frame
    files = list((tmp_path / "snapshots" / "cam1").glob("*vehicle_count_snapshot*.jpg"))
    assert len(files) == 1
    payload = simple_event(event)
    assert payload["snapshot_ref"] == event["snapshot"]["ref"]
    assert payload["snapshot_assets"]["event_frame"]["ref"] == event["snapshot"]["ref"]


def test_count_snapshot_refires_after_interval(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    cam = {"runtime_analytics": {"pedestrian_counting": {"lines": [], "zones": []}}}
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    monkeypatch.setenv("COUNT_SNAPSHOT_INTERVAL_S", "0")
    stage = TrafficAnalyticsStage({"cam1": cam})

    p1 = _packet(1, _det(cls="pedestrian", bbox=[20, 20, 60, 80]))
    p2 = _packet(2, _det(cls="pedestrian", bbox=[22, 20, 62, 80]))
    stage.process([p1])
    stage.process([p2])

    snapshots = [e for e in p1.analytics_events + p2.analytics_events if e["type"] == "pedestrian_count_snapshot"]
    assert len(snapshots) == 2


def test_plate_read_writes_full_frame_snapshot_only(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    cam = {"runtime_analytics": {"plate_detection": {"enabled": True, "zones": []}}}
    stage = TrafficAnalyticsStage({"cam1": cam})

    frame = np.full((80, 120, 3), 127, dtype=np.uint8)
    vehicle = Detection(
        bbox=[10, 20, 110, 70],
        class_id=2,
        class_name="car",
        confidence=0.9,
        model_name="vehicle",
        metadata={"track_id": 7},
    )
    plate = Detection(
        bbox=[40, 40, 60, 52],
        class_id=0,
        class_name="license_plate",
        confidence=0.85,
        model_name="license_plate",
        parent_id=7,
        metadata={"ocr_text": "TS09EA1234"},
    )
    packet = FramePacket(index=9, name="cam1", frame=frame, detections=[vehicle, plate])

    stage.process([packet])

    plate_events = [e for e in packet.analytics_events if e["type"] == "plate_read"]
    assert len(plate_events) == 1
    event = plate_events[0]
    whole = event["snapshot"]
    assert whole["bbox"] == [40, 40, 60, 52]  # plate bbox on the whole frame
    assert "snapshots" not in event

    files = sorted(p.name for p in (tmp_path / "snapshots" / "cam1").glob("*.jpg"))
    assert len(files) == 1
    assert files[0].endswith("-plate_read-9.jpg")

    payload = simple_event(event)
    assert set(payload["snapshot_assets"].keys()) == {"event_frame"}
    assert payload["snapshot_assets"]["event_frame"]["url"] == payload["snapshot_url"]
    assert "/snapshots/snapshots/" not in payload["snapshot_url"]


def _plate_packet(idx, track_id):
    vehicle = Detection(bbox=[10, 20, 110, 70], class_id=2, class_name="car", confidence=0.9,
                        model_name="vehicle", metadata={"track_id": track_id})
    plate = Detection(bbox=[40, 40, 60, 52], class_id=0, class_name="license_plate", confidence=0.85,
                      model_name="license_plate", parent_id=track_id,
                      metadata={"ocr_text": "KA01AB1234"})
    return FramePacket(index=idx, name="cam1", frame=np.full((80, 120, 3), 127, dtype=np.uint8),
                       detections=[vehicle, plate])


def test_plate_read_fires_once_per_deduped_plate(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    cam = {"runtime_analytics": {"plate_detection": {"enabled": True, "zones": []}}}
    stage = TrafficAnalyticsStage({"cam1": cam})

    p1 = _plate_packet(1, 7)
    p2 = _plate_packet(2, 7)
    stage.process([p1])
    stage.process([p2])

    events = [
        e
        for e in p1.analytics_events + p2.analytics_events
        if e["type"] == "plate_read"
    ]
    assert len(events) == 1
    files = list((tmp_path / "snapshots" / "cam1").glob("*.jpg"))
    assert len(files) == 1  # event_frame only, once per deduped plate
