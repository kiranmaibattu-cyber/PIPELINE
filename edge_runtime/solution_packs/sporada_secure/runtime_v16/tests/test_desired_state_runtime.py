from __future__ import annotations

import json
from pathlib import Path

import pytest

from traffic_pilot_runtime.adapter import write_worker_config
from traffic_pilot_runtime.desired_state import DesiredStateValidator
from traffic_pilot_runtime.graph import compile_runtime_plan
from traffic_pilot_runtime.solution_image_entrypoint import _hot_reload_signature


def _zone(name="zone-main"):
    return {"id": name, "name": name, "poly": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]}


def _config_for(apps):
    zones = {}
    for app in apps:
        if app == "vehicle_entry_exit_counts":
            continue
        key = "anpr" if app == "plate_detection" else app
        zones[key] = [_zone(f"{key}-zone")]
    config = {
        "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
        "emission": {
            "minimum_quality": 0.3,
            "cooldown_seconds": 5,
            "material_change_threshold": 0.15,
        },
        "zones": zones,
    }
    if "vehicle_entry_exit_counts" in apps:
        config["counting_lines"] = {
            "vehicle_entry_exit_counts": [{
                "id": "entrance-line",
                "name": "Entrance line",
                "a": [0.1, 0.5],
                "b": [0.9, 0.5],
                "direction_mapping": {"right_to_left": "in", "left_to_right": "out"},
                "vehicle_classes": ["car", "truck", "bus", "motorcycle"],
            }]
        }
    return config


def _desired(tmp_path: Path, apps, config=None):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    source = secrets / "cam1.url"
    source.write_text("rtsp://camera.test/stream\n", encoding="utf-8")
    schema_apps = ["anpr" if app == "plate_detection" else app for app in apps]
    desired = tmp_path / "desired_state.json"
    desired.write_text(json.dumps({
        "edge_id": "edge-test",
        "revision": 1,
        "cameras": [{
            "camera_id": "cam1",
            "source": f"file:{source}",
            "solution_pack": "sporada-secure",
            "fps": 8,
            "apps": schema_apps,
            "config": config if config is not None else _config_for(apps),
        }],
    }), encoding="utf-8")
    return desired, secrets


def test_desired_state_requires_secret_source(tmp_path):
    desired = tmp_path / "desired_state.json"
    desired.write_text(json.dumps({
        "edge_id": "edge-test",
        "revision": 1,
        "cameras": [{
            "camera_id": "cam1",
            "source": "rtsp://camera",
            "solution_pack": "sporada-secure",
            "apps": ["vehicle_counting"],
            "config": _config_for(["vehicle_counting"]),
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="Secret"):
        DesiredStateValidator(tmp_path).load(desired)


@pytest.mark.parametrize("source", [
    "/data/local.mp4",
    "file:///data/local.mp4",
    "rtmp://camera.test/live",
    "ftp://camera.test/video",
])
def test_v14_secret_rejects_non_contract_source_schemes(tmp_path, source):
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"])
    (secrets / "cam1.url").write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match="rtsp, rtsps, http, or https"):
        DesiredStateValidator(secrets).load(desired_path)


@pytest.mark.parametrize("source", [
    "rtsp://camera.test/live",
    "rtsps://camera.test/live",
    "http://camera.test/live.mjpg",
    "https://camera.test/index.m3u8",
])
def test_v14_secret_accepts_contract_stream_schemes(tmp_path, source):
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"])
    (secrets / "cam1.url").write_text(source, encoding="utf-8")

    state = DesiredStateValidator(secrets).load(desired_path)

    assert state.cameras[0].source.endswith("cam1.url")


def test_dynamic_graph_only_contains_active_counting_nodes(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"])
    desired = DesiredStateValidator(secrets).load(desired_path)

    graph = compile_runtime_plan(desired).cameras[0]

    assert graph.config_mode == "zone"
    assert "vehicle_detector" in graph.node_ids
    assert "vehicle_tracker" in graph.node_ids
    assert "vehicle_counting" in graph.node_ids
    assert "plate_detector" not in graph.node_ids
    assert "ocr_service" not in graph.node_ids
    assert "smoke_fire_detector" not in graph.node_ids


def test_dynamic_graph_adds_plate_and_smoke_only_when_active(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["plate_detection", "fire_smoke_detection"])
    desired = DesiredStateValidator(secrets).load(desired_path)

    graph = compile_runtime_plan(desired).cameras[0]

    assert "plate_detector" in graph.node_ids
    assert "ocr_service" in graph.node_ids
    assert "smoke_fire_detector" in graph.node_ids
    assert "snapshot_storage" in graph.node_ids
    assert graph.devices["smoke_fire_detector"] == "GPU"
    assert {"source": "fire_smoke_detection", "target": "snapshot_storage"} in graph.edges


def test_dynamic_graph_adds_face_branch_without_removing_traffic_nodes(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["plate_detection", "vehicle_counting", "face_recognition"])
    desired = DesiredStateValidator(secrets).load(desired_path)

    graph = compile_runtime_plan(desired).cameras[0]

    assert {"plate_detector", "ocr_service", "vehicle_counting"} <= set(graph.node_ids)
    assert {"face_detector", "face_alignment", "face_embedder", "face_sample_outbox",
            "face_management_uploader"} <= set(graph.node_ids)
    assert graph.devices["face_detector"] == "GPU"
    assert graph.devices["face_embedder"] == "NPU"
    assert ("vehicle_tracker", "face_detector") in graph.edge_ids
    assert ("face_embedder", "face_recognition") in graph.edge_ids


def test_adapter_writes_worker_config_from_secret_and_zone(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"])
    desired = DesiredStateValidator(secrets).load(desired_path)

    payload = write_worker_config(desired, tmp_path / "generated" / "cameras.json")

    camera = payload["cameras"][0]
    assert camera["source"]["uri"] == "rtsp://camera.test/stream"
    assert camera["processing"]["fps"] == 8
    zone_config = camera["analytics"]["vehicle_counting"]["zones"][0]
    assert zone_config["type"] == "vehicle_counting"
    assert zone_config["normalized"] is True
    assert camera["analytics"]["vehicle_counting"]["lines"] == []


def test_hot_reload_signature_ignores_zone_only_changes(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"])
    first = DesiredStateValidator(secrets).load(desired_path)
    data = json.loads(desired_path.read_text(encoding="utf-8"))
    data["revision"] = 2
    data["cameras"][0]["config"]["zones"]["vehicle_counting"][0]["poly"] = [
        [0.0, 0.0],
        [0.5, 0.0],
        [0.5, 0.5],
        [0.0, 0.5],
    ]
    desired_path.write_text(json.dumps(data), encoding="utf-8")
    second = DesiredStateValidator(secrets).load(desired_path)

    assert first.content_hash != second.content_hash
    assert _hot_reload_signature(first) == _hot_reload_signature(second)


def test_hot_reload_signature_changes_for_runtime_topology(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"])
    first = DesiredStateValidator(secrets).load(desired_path)
    data = json.loads(desired_path.read_text(encoding="utf-8"))
    data["revision"] = 2
    data["cameras"][0]["apps"] = ["vehicle_counting", "anpr"]
    data["cameras"][0]["config"]["zones"]["anpr"] = [_zone("anpr-zone")]
    desired_path.write_text(json.dumps(data), encoding="utf-8")
    second = DesiredStateValidator(secrets).load(desired_path)

    assert _hot_reload_signature(first) != _hot_reload_signature(second)


def test_validator_rejects_legacy_line_field(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"], {"line": {"a": [0.1, 0.5], "b": [0.9, 0.5]}})

    with pytest.raises(ValueError, match="unknown fields"):
        DesiredStateValidator(secrets).load(desired_path)


def test_entry_exit_graph_is_durable_and_not_connected_to_sse(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["vehicle_entry_exit_counts"])
    state = DesiredStateValidator(secrets).load(desired_path)

    graph = compile_runtime_plan(state).cameras[0]

    assert graph.config_mode == "line"
    assert ("vehicle_tracker", "vehicle_crossing_evaluator") in graph.edge_ids
    assert ("vehicle_crossing_outbox", "vehicle_crossing_uploader") in graph.edge_ids
    assert ("vehicle_entry_exit_counts", "event_sink") not in graph.edge_ids
    assert "event_sink" not in graph.node_ids


def test_occupancy_and_entry_exit_share_detector_and_tracker(tmp_path):
    desired_path, secrets = _desired(
        tmp_path, ["vehicle_counting", "vehicle_entry_exit_counts"]
    )
    state = DesiredStateValidator(secrets).load(desired_path)
    graph = compile_runtime_plan(state).cameras[0]

    assert graph.node_ids.count("vehicle_detector") == 1
    assert graph.node_ids.count("vehicle_tracker") == 1
    assert graph.config_mode == "mixed"
    assert ("vehicle_counting", "event_sink") in graph.edge_ids
    assert ("vehicle_entry_exit_counts", "event_sink") not in graph.edge_ids


def test_adapter_writes_ordered_entry_exit_line(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["vehicle_entry_exit_counts"])
    state = DesiredStateValidator(secrets).load(desired_path)
    payload = write_worker_config(state, tmp_path / "generated" / "cameras.json")

    line = payload["cameras"][0]["analytics"]["vehicle_entry_exit_counts"]["lines"][0]
    assert line["points"] == [{"x": 0.1, "y": 0.5}, {"x": 0.9, "y": 0.5}]
    assert line["vehicle_classes"] == ["car", "truck", "bus", "motorcycle"]
    assert line["minimum_track_age_frames"] == 3
    assert line["minimum_crossing_displacement"] == 0.02


def test_entry_exit_rejects_model_unsupported_vehicle_class(tmp_path):
    config = _config_for(["vehicle_entry_exit_counts"])
    config["counting_lines"]["vehicle_entry_exit_counts"][0]["vehicle_classes"] = ["van"]
    desired_path, secrets = _desired(tmp_path, ["vehicle_entry_exit_counts"], config)

    with pytest.raises(ValueError, match="vehicle_classes"):
        DesiredStateValidator(secrets).load(desired_path)


def test_validator_accepts_missing_counting_zone_as_full_frame(tmp_path):
    config = _config_for([])
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"], config)

    state = DesiredStateValidator(secrets).load(desired_path)
    payload = write_worker_config(state, tmp_path / "generated" / "cameras.json")

    assert state.cameras[0].config["zones"] == {}
    assert payload["cameras"][0]["analytics"]["vehicle_counting"]["zones"] == []


def test_validator_rejects_omitted_config(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting", "pedestrian_counting"])
    document = json.loads(desired_path.read_text(encoding="utf-8"))
    document["cameras"][0].pop("config")
    desired_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="missing fields"):
        DesiredStateValidator(secrets).load(desired_path)


def test_validator_rejects_bad_polygon(tmp_path):
    config = _config_for([])
    config["zones"] = {"vehicle_counting": [{"id": "bad", "name": "bad", "poly": [[0, 0], [1, 1], [0, 1], [1, 0]]}]}
    desired_path, secrets = _desired(tmp_path, ["vehicle_counting"], config)

    with pytest.raises(ValueError, match="zero area|self-intersecting"):
        DesiredStateValidator(secrets).load(desired_path)


def test_desired_state_accepts_schema_app_names(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["plate_detection", "vehicle_counting", "pedestrian_counting", "fire_smoke_detection"])
    state = DesiredStateValidator(secrets).load(desired_path)
    payload = write_worker_config(state, tmp_path / "generated" / "cameras.json")

    assert state.cameras[0].apps == (
        "plate_detection",
        "vehicle_counting",
        "pedestrian_counting",
        "fire_smoke_detection",
    )
    analytics = payload["cameras"][0]["analytics"]
    assert analytics["plate_detection"]["zones"][0]["type"] == "plate_roi"
    assert analytics["vehicle_counting"]["zones"][0]["type"] == "vehicle_counting"
    assert analytics["pedestrian_counting"]["zones"][0]["type"] == "pedestrian_counting"
    assert analytics["fire_smoke_detection"]["zones"][0]["type"] == "fire_smoke"


def test_desired_state_rejects_apps_not_in_limited_image(tmp_path):
    desired_path, secrets = _desired(tmp_path, ["wrong_way"], {"zones": {"wrong_way": [_zone("wrong")]} })

    with pytest.raises(ValueError, match="unsupported apps"):
        DesiredStateValidator(secrets).load(desired_path)
