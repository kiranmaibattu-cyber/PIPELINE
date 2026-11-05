from pathlib import Path
from types import SimpleNamespace
import os,sys,json,time
import numpy as np
import pytest
sys.path.insert(0,'/opt/traffic-pilot')
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline import v8_events as events_module
from pipeline.v8_events import V8EventPipeline as ActualPipeline
from pipeline.ocr_stabilizer import OcrStabilizer
CAPTURES={}
class CaptureStore:
    def __init__(self):
        self.root=Path(os.environ['APEXFABRIC_STATE_ROOT']);self.rows=CAPTURES.setdefault(str(self.root),[])
    def persist(self,event,evidence,embeddings):
        rows=[]
        for metadata,blob in evidence:
            p=self.root/(metadata['evidence_id']+'.jpg');p.write_bytes(blob)
            rows.append({'metadata':metadata,'path':str(p)})
        self.rows.append({'observation':event,'evidence':rows,'embeddings':embeddings})
        return True
    def metrics_summary(self):return {'outbox_bytes':0}
    def close(self):pass

def create_pipeline(*args,**kwargs):
    kwargs['handoff_client']=CaptureStore()
    return ActualPipeline(*args,**kwargs)
def events(tmp_path):return [r['observation'] for r in CAPTURES.get(str(tmp_path),[])]
def package_record(tmp_path,observation_id):return next(r for r in CAPTURES[str(tmp_path)] if r['observation']['observation_id']==observation_id)
class Gait:
    def __init__(self):self.buffers={};self.buffer_times={}
    def collect(self,frame,people,camera_id,keys,captured_at=None,**kwargs):
        out={}
        for person in people:
            track=int(person.metadata['track_id']);key=keys[track]
            self.buffers[key]=[np.ones((64,44),np.uint8) for _ in range(24)]
            self.buffer_times[key]=[captured_at-1,captured_at]
            accept=kwargs.get('accept_sequence')
            if accept is None or accept(track,24):
                vector=np.zeros(4096,np.float32);vector[0]=1;out[track]=vector
        return out


class Scene:
    def embed(self, image):
        vector = np.zeros(768, np.float32)
        vector[0] = 1
        return vector

class Body:
    def embed(self, crops):
        vector = np.zeros(384, np.float32)
        vector[0] = 1
        return np.stack([vector for _ in crops])

def packet(index, boxes=(), plate=None, *, width=100, height=100):
    detections = [SimpleNamespace(model_name="vehicle", class_name=kind,
                                  bbox=box, confidence=.9,
                                  metadata={"track_id": track})
                  for kind, track, box in boxes]
    frame = np.full((height, width, 3), 128, np.uint8)
    if plate is not None:
        plate_box = (30, 50, 60, 62)
        pattern = (np.indices((12, 30)).sum(axis=0) % 2 * 255).astype(np.uint8)
        frame[50:62, 30:60] = np.repeat(pattern[:, :, None], 3, axis=2)
        detections.append(SimpleNamespace(model_name="license_plate", bbox=plate_box,
                                          confidence=.9, metadata={"ocr_text": plate}, parent_id=1))
    return SimpleNamespace(index=index, frame=frame,
                           detections=detections)

def add_quality_plate(sample, text, confidence, *, parent_id=1, provisional=None):
    box = (30, 48, 62, 60)
    pattern = (np.indices((12, 32)).sum(axis=0) % 2 * 255).astype(np.uint8)
    sample.frame[48:60, 30:62] = np.repeat(pattern[:, :, None], 3, axis=2)
    metadata = {"ocr_text": text, "ocr_confidence": confidence}
    if provisional is not None:
        metadata["ocr_provisional"] = provisional
    sample.detections.append(SimpleNamespace(
        model_name="license_plate", bbox=box, confidence=.9,
        metadata=metadata, parent_id=parent_id,
    ))
    return sample

def binding(use_case_id, app, scope="full_frame", ids=(), kind="vehicle", **extra):
    return {"use_case_id": use_case_id, "app": app, "scope": scope,
            "geometry_ids": list(ids), "object_types": [] if kind is None else [kind], **extra}

def config(bindings, zones=(), lines=()):
    return {"analytics": {"v8_events": {"v8": {
        "deployment_id": "test", "config_revision": 1, "bindings": bindings,
        "geometry": {"zones": list(zones), "counting_lines": list(lines)},
    }}}}

def test_count_only_binding_does_not_emit_presence_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline(
        "cam", config([binding("count", "vehicle_counting")]),
    )
    for index in range(5):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .2
    clock.value += 20
    pipeline.process(packet(6))
    output = events(tmp_path)
    assert any(item["event_type"] == "zone_count" for item in output)
    assert not any(item["event_type"] in {
        "object_present", "track_lost", "presence_recovered",
        "presence_ended_unknown",
    } for item in output)

def test_quiet_frame_preserves_lifecycle_without_negative_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = create_pipeline("cam", config([
        binding("presence", "vehicle_presence", "geometry", ["parking"]),
        binding("count", "vehicle_counting", "geometry", ["parking"]),
        binding("hazard", "fire_smoke", "geometry", ["parking"], None),
    ], [zone]))
    for index in range(3):
        item = packet(index, [("car", 1, (20, 20, 70, 75))])
        item.frame_received_at = clock.value
        pipeline.process(item)
        clock.value += .1
    before = events(tmp_path)
    zero_counts_before = len([item for item in before
                              if item["event_type"] == "zone_count" and item["count"] == 0])
    clock.value += 5.1
    pipeline.process_quiet(clock.value, 4)
    clock.value += 13
    pipeline.process_quiet(clock.value, 5)
    output = events(tmp_path)
    assert not any(item["event_type"] == "track_lost" for item in output)
    assert not any(item["event_type"] == "presence_ended_unknown" for item in output)
    assert not any(item["event_type"] == "zone_exit" for item in output)
    assert not any(item["event_type"] == "fire_smoke_cleared" for item in output)
    zero_counts_after = len([item for item in output
                             if item["event_type"] == "zone_count" and item["count"] == 0])
    assert zero_counts_after == zero_counts_before

def test_presence_count_scene_and_anpr(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    bindings = [binding("presence", "vehicle_presence", "geometry", ["parking"],
                        embedding_profile_ids=["siglip2-base-v1"]),
                binding("count", "vehicle_counting", "geometry", ["parking"]),
                binding("plate", "anpr"),
                binding("scene", "scene_search", "geometry", ["parking"], None,
                        embedding_profile_ids=["siglip2-base-v1"], sample_interval_seconds=2)]
    pipeline = create_pipeline("cam", config(bindings, [zone]), scene=Scene())
    box = (20, 20, 70, 75)
    for index in range(1, 6):
        pipeline.process(packet(index, [("car", 1, box)], "KA01AB1234" if index >= 4 else None))
        clock.value += 1
    observations = events(tmp_path)
    assert len([item for item in observations if item["event_type"] == "object_present"]) == 1
    assert not any(item["event_type"] == "zone_entry" for item in observations)
    assert any(item["event_type"] == "zone_count" and item["count"] == 0 for item in observations)
    assert any(item["event_type"] == "zone_count" and item["count"] == 1 for item in observations)
    assert any(item["event_type"] == "plate_read" and item["plate_bbox_normalized"]["x1"] == .3 for item in observations)
    scene = next(item for item in observations if item["event_type"] == "scene_sample")
    assert scene["zone_id"] == "parking" and scene["scope"] == "geometry"
    record = package_record(tmp_path, scene["observation_id"])
    assert record["evidence"][0]["metadata"]["evidence_type"] == "scene_region_crop"
    assert record["evidence"][0]["metadata"]["source_bbox_normalized"] == {"x1": .1, "y1": .1, "x2": .9, "y2": .9}
    assert record["embeddings"][0]["evidence_id"] == record["evidence"][0]["metadata"]["evidence_id"]
    assert record["observation"]["stream_session_id"] == next(item["stream_session_id"] for item in observations if item["event_type"] == "object_present")

def test_loss_is_unknown_and_positive_exit_is_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = create_pipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .1
    for index in range(3, 6):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 95))]))
        clock.value += .3
    assert len([item for item in events(tmp_path) if item["event_type"] == "zone_exit"]) == 1
    clock.value += 5.1
    pipeline.process(packet(6))
    clock.value += 13
    pipeline.process(packet(7))
    output = events(tmp_path)
    assert len([item for item in output if item["event_type"] == "track_lost"]) == 1
    assert len([item for item in output if item["event_type"] == "presence_ended_unknown"]) == 1
    assert len([item for item in output if item["event_type"] == "zone_exit"]) == 1

def test_zone_boundary_jitter_does_not_emit_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = create_pipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    for index, bottom in enumerate((89, 91, 89, 91, 89), start=3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, bottom))]))
    assert not any(item["event_type"] == "zone_exit" for item in events(tmp_path))

def test_candidate_confirmation_requires_bounded_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline("cam", config([binding("presence", "vehicle_presence")]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += 2
    assert not any(item["event_type"] == "object_present" for item in events(tmp_path))

def test_frame_receive_time_is_used_for_observations(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    pipeline = create_pipeline("cam", config([binding("presence", "vehicle_presence")]))
    for index in range(3):
        item = packet(index, [("car", 1, (20, 20, 70, 75))])
        item.frame_received_at = 100.0 + index * .1
        pipeline.process(item)
    present = next(item for item in events(tmp_path) if item["event_type"] == "object_present")
    assert present["observed_at"] == "1970-01-01T00:01:40.200000Z"
    assert pipeline.timestamp_source == "edge_received_at"

def test_frame_observed_time_precedes_receive_time_when_available(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    pipeline = create_pipeline("cam", config([binding("presence", "vehicle_presence")]))
    for index in range(3):
        item = packet(index, [("car", 1, (20, 20, 70, 75))])
        item.frame_observed_at = 90.0 + index * .1
        item.frame_received_at = 100.0 + index * .1
        pipeline.process(item)
    present = next(item for item in events(tmp_path) if item["event_type"] == "object_present")
    assert present["observed_at"] == "1970-01-01T00:01:30.200000Z"
    assert pipeline.timestamp_source == "source_pts"

def test_same_track_recovery_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = create_pipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .1
    clock.value += 5.1
    pipeline.process(packet(3))
    clock.value += 5.1
    pipeline.process(packet(4))
    pipeline.process(packet(5, [("car", 1, (20, 20, 70, 75))]))
    output = events(tmp_path)
    recovered = [item for item in output if item["event_type"] == "presence_recovered"]
    assert len(recovered) == 1
    assert recovered[0]["previous_track_id"] == recovered[0]["track_id"] == "1"
    assert recovered[0]["zone_episodes"][0]["observation_state"] == "observed_inside"

def test_motion_quiet_frames_do_not_age_presence_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline(
        "cam", config([binding("presence", "person_presence", kind="person")])
    )
    for index in range(3):
        pipeline.process(packet(index, [("pedestrian", 1, (20, 10, 80, 90))]))
        clock.value += .1
    presence_id = next(
        item["presence_id"] for item in events(tmp_path)
        if item["event_type"] == "object_present"
    )

    for index in range(3, 100):
        clock.value += .25
        pipeline.process_quiet(clock.value, index)

    output = events(tmp_path)
    assert not any(
        item["event_type"] in {
            "track_lost", "presence_recovered", "presence_ended_unknown",
        }
        for item in output
    )
    assert presence_id in pipeline.presences

def test_tracker_retained_prediction_suppresses_external_track_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline(
        "cam", config([binding("presence", "person_presence", kind="person")])
    )
    for index in range(3):
        pipeline.process(packet(index, [("pedestrian", 1, (20, 10, 80, 90))]))
        clock.value += .1

    for index in range(3, 8):
        clock.value += 2
        sample = packet(index, [("pedestrian", 1, (20, 10, 80, 90))])
        sample.detections[0].metadata["predicted"] = True
        pipeline.process(sample)

    output = events(tmp_path)
    assert not any(
        item["event_type"] in {"track_lost", "presence_recovered"}
        for item in output
    )

def test_small_subject_gets_extended_recovery_hysteresis(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline(
        "cam", config([binding("presence", "person_presence", kind="person")])
    )
    # 32x102 in a 1280x720 source represents the distant people observed in
    # the live qualification view.
    for index in range(3):
        pipeline.process(packet(
            index, [("pedestrian", 1, (256, 55, 288, 157))],
            width=1280, height=720,
        ))
        clock.value += .1

    clock.value += .1
    pipeline.process(packet(3, width=1280, height=720))
    clock.value += 8.1
    pipeline.process(packet(4, width=1280, height=720))
    assert not any(item["event_type"] == "track_lost" for item in events(tmp_path))

    clock.value += 2.1
    pipeline.process(packet(5, width=1280, height=720))
    assert any(item["event_type"] == "track_lost" for item in events(tmp_path))

def test_stream_disconnect_has_unknown_not_exit_and_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = create_pipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .1
    first_session = pipeline.session_id
    clock.value += 2
    pipeline.camera_status("unavailable", "stream_disconnected")
    clock.value += 1
    pipeline.camera_status("healthy", "none")
    assert pipeline.session_id != first_session
    output = events(tmp_path)
    assert not any(item["event_type"] == "zone_exit" for item in output)
    assert any(item["event_type"] == "camera_health" and item["state"] == "unavailable" for item in output)
    assert any(item["event_type"] == "track_lost" and item["reason"] == "stream_interrupted" for item in output)
    assert any(item["event_type"] == "presence_ended_unknown" and item["stream_session_id"] == first_session for item in output)
    assert any(item["event_type"] == "camera_health" and item["state"] == "healthy" and item["stream_session_id"] == pipeline.session_id for item in output)

def test_binding_revision_keeps_session_and_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    original = config([binding("a", "vehicle_presence")])
    pipeline = create_pipeline("cam", original)
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    first = next(item for item in events(tmp_path) if item["event_type"] == "object_present")
    session = pipeline.session_id
    updated = config([binding("b", "vehicle_presence")])
    updated["analytics"]["v8_events"]["v8"]["config_revision"] = 2
    pipeline.reconfigure(updated)
    assert pipeline.session_id == session
    clock.value += 61
    pipeline.process(packet(3, [("car", 1, (20, 20, 70, 75))]))
    present = [item for item in events(tmp_path) if item["event_type"] == "object_present"]
    assert len(present) == 2
    assert {item["presence_id"] for item in present} == {first["presence_id"]}
    latest = next(item for item in present if item["config_revision"] == 2)
    assert latest["reason"] == "periodic" and latest["use_case_id"] == "b"

def test_geometry_revision_preserves_presence_without_false_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    old_zone = {
        "id": "parking", "name": "Parking",
        "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]],
    }
    presence = binding(
        "presence", "vehicle_presence", "geometry", ["parking"]
    )
    pipeline = create_pipeline("cam", config([presence], [old_zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .1
    first = next(
        item for item in events(tmp_path)
        if item["event_type"] == "object_present"
    )
    session = pipeline.session_id

    new_zone = {
        "id": "parking", "name": "Parking",
        "poly": [[.0, .0], [.4, .0], [.4, .4], [.0, .4]],
    }
    updated = config([presence], [new_zone])
    updated["analytics"]["v8_events"]["v8"]["config_revision"] = 2
    pipeline.reconfigure(updated)

    output = events(tmp_path)
    material = next(
        item for item in output
        if item["event_type"] == "object_present"
        and item["reason"] == "material_change"
    )
    assert pipeline.session_id == session
    assert material["presence_id"] == first["presence_id"]
    assert material["zone_episodes"][0]["observation_state"] == "ended_unknown"
    assert not any(item["event_type"] == "zone_exit" for item in output)
    assert any(
        item["event_type"] == "camera_health"
        and item["reason"] == "view_changed"
        for item in output
    )

    for index in range(3, 7):
        clock.value += .1
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    output = events(tmp_path)
    assert not any(
        item["event_type"] in {"zone_entry", "zone_exit"}
        and item["config_revision"] == 2
        for item in output
    )

def test_shared_polygon_fans_out_with_one_episode(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    bindings = [binding("a", "vehicle_presence", "geometry", ["parking"]),
                binding("b", "vehicle_presence", "geometry", ["parking"])]
    pipeline = create_pipeline("cam", config(bindings, [zone]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 95))]))
        clock.value += .1
    for index in range(3, 6):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
        clock.value += .3
    output = events(tmp_path)
    entrances = [item for item in output if item["event_type"] == "zone_entry"]
    assert {item["use_case_id"] for item in entrances} == {"a", "b"}
    assert len({item["zone_episode_id"] for item in entrances}) == 1
    first = [item for item in output if item["event_type"] == "object_present"]
    assert len(first) == 1 and first[0]["use_case_id"] == "a"

def test_dwell_episode_updates_inside_time_and_completes_only_on_positive_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    zone = {"id": "parking", "name": "Parking", "poly": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}
    pipeline = create_pipeline("cam", config([binding("presence", "vehicle_presence", "geometry", ["parking"])], [zone]))
    outside = (20, 20, 70, 95)
    inside = (20, 20, 70, 75)
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, outside)]))
        clock.value += 1
    pipeline.process(packet(3, [("car", 1, inside)]))
    clock.value += 1
    pipeline.process(packet(4, [("car", 1, inside)]))
    clock.value += 5
    pipeline.process(packet(5, [("car", 1, inside)]))
    output = events(tmp_path)
    entry = next(item for item in output if item["event_type"] == "zone_entry")
    clock.value += 61
    pipeline.process(packet(6, [("car", 1, inside)]))
    present = next(item for item in events(tmp_path)
                   if item["event_type"] == "object_present" and item["reason"] == "periodic")
    episode = next(item for item in present["zone_episodes"] if item["zone_id"] == "parking")
    assert episode["zone_episode_id"] == entry["zone_episode_id"]
    assert episode["last_confirmed_inside_at"] == "1970-01-01T00:02:50Z"
    clock.value += 5
    pipeline.process(packet(7))
    assert not any(item["event_type"] == "zone_exit" for item in events(tmp_path))
    pipeline.process(packet(8, [("car", 1, outside)]))
    clock.value += 1
    pipeline.process(packet(9, [("car", 1, outside)]))
    exits = [item for item in events(tmp_path) if item["event_type"] == "zone_exit"]
    assert len(exits) == 1 and exits[0]["zone_episode_id"] == entry["zone_episode_id"]

def test_face_and_body_are_independent_samples(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["adaface-ir101-v18.1", "transreid-ssl-v18.1"])
    pipeline = create_pipeline("cam", config([identity]), body=Body())
    face_vector = np.zeros(512, np.float32)
    face_vector[0] = 1
    face = SimpleNamespace(track_id=2, embedding=face_vector, quality=.8, bbox=(40, 30, 60, 55))
    for index in range(1, 4):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]), [face] if index == 3 else [])
        clock.value += .6
    clock.value += 4
    pipeline.process(packet(4))
    output = events(tmp_path)
    assert len([item for item in output if item["event_type"] == "object_present"]) == 1
    features = [item for item in output if item["event_type"] == "person_feature_sample"]
    assert {item["kind"] for item in features} == {"face", "body"}
    assert len({item["presence_id"] for item in features}) == 1

def test_gait_sample_emits_sequence_metadata_and_embedding(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["gaitbase-v18.1"])
    pipeline = create_pipeline("cam", config([identity]), gait=Gait())
    for index in range(4):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]))
        clock.value += .5
    output = events(tmp_path)
    gait = next(item for item in output if item.get("kind") == "gait")
    assert gait["profile_id"] == "gaitbase-v18.1"
    assert gait["source_frame_id"] == "cam:3"
    assert gait["quality"] == pytest.approx(0.8)
    record = package_record(tmp_path, gait["observation_id"])
    embedding = record["embeddings"][0]
    assert embedding["kind"] == "gait"
    assert len(embedding["vector"]) == 4096
    assert embedding["usable_silhouette_count"] == 24
    assert embedding["sequence_started_at"] == "1970-01-01T00:01:40.500000Z"
    assert embedding["sequence_ended_at"] == "1970-01-01T00:01:41.500000Z"

def test_face_window_selects_best_original_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["adaface-ir101-v18.1"])
    pipeline = create_pipeline("cam", config([identity]))
    vector = np.zeros(512, np.float32)
    vector[0] = 1
    for index in range(3):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]))
    first = SimpleNamespace(track_id=2, embedding=vector, quality=.5, bbox=(40, 30, 60, 55))
    pipeline.process(packet(3, [("pedestrian", 2, (20, 10, 80, 90))]), [first])
    clock.value += 1
    better = SimpleNamespace(track_id=2, embedding=vector, quality=.8, bbox=(42, 30, 62, 55))
    pipeline.process(packet(4, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    assert not any(item["event_type"] == "person_feature_sample" for item in events(tmp_path))
    clock.value += 3.1
    pipeline.process(packet(5))
    selected = [item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]
    assert len(selected) == 1 and selected[0]["quality"] == .8
    assert selected[0]["source_frame_id"] == "cam:4"

def test_face_improvement_respects_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["adaface-ir101-v18.1"])
    pipeline = create_pipeline("cam", config([identity]))
    vector = np.zeros(512, np.float32)
    vector[0] = 1
    first = SimpleNamespace(track_id=2, embedding=vector, quality=.4, bbox=(40, 30, 60, 55))
    better = SimpleNamespace(track_id=2, embedding=vector, quality=.8, bbox=(40, 30, 60, 55))
    for index in range(3):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]),
                         [first] if index == 2 else [])
    clock.value += 4.1
    pipeline.process(packet(3, [("pedestrian", 2, (20, 10, 80, 90))]))
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 1
    pipeline.process(packet(4, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 1
    clock.value += 1.1
    pipeline.process(packet(5, [("pedestrian", 2, (20, 10, 80, 90))]), [better])
    clock.value += 4.1
    pipeline.process(packet(6))
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 2

def test_disconnect_flushes_pending_face_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    identity = binding("identity", "person_identity", kind="person",
                       embedding_profile_ids=["adaface-ir101-v18.1"])
    pipeline = create_pipeline("cam", config([identity]))
    vector = np.zeros(512, np.float32)
    vector[0] = 1
    face = SimpleNamespace(track_id=2, embedding=vector, quality=.8, bbox=(40, 30, 60, 55))
    for index in range(3):
        pipeline.process(packet(index, [("pedestrian", 2, (20, 10, 80, 90))]),
                         [face] if index == 2 else [])
    assert not any(item["event_type"] == "person_feature_sample" for item in events(tmp_path))
    pipeline.camera_status("unavailable", "stream_disconnected")
    assert len([item for item in events(tmp_path) if item["event_type"] == "person_feature_sample"]) == 1

def test_line_crossing_requires_actual_segment_intersection(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    line = {"id": "gate", "name": "Gate", "a": [.5, .1], "b": [.5, .9],
            "direction_mapping": {"left_to_right": "in", "right_to_left": "out"}}
    crossing = binding("cross", "vehicle_entry_exit_counts", "geometry", ["gate"])
    pipeline = create_pipeline("cam", config([crossing], lines=[line]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (10, 20, 50, 70))]))
    pipeline.process(packet(3, [("car", 1, (50, 20, 90, 70))]))
    crosses = [item for item in events(tmp_path) if item["event_type"] == "line_cross"]
    assert len(crosses) == 1 and crosses[0]["line_id"] == "gate"

def test_fire_episode_has_clear_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline("cam", config([binding("hazard", "fire_smoke", kind=None)]))
    for index in range(5):
        sample = packet(index)
        sample.detections.append(SimpleNamespace(model_name="smoke_fire", class_name="fire",
                                                 bbox=(20, 20, 60, 60), confidence=.9))
        pipeline.process(sample)
        clock.value += .5
    first_negative = packet(5)
    pipeline.process(first_negative)
    clock.value += 9
    skipped = packet(6)
    skipped.analytics_state = {"fire_smoke_evaluated": False}
    pipeline.process(skipped)
    assert not any(item["event_type"] == "fire_smoke_cleared" for item in events(tmp_path))
    clock.value += 2
    pipeline.process(packet(7))
    assert not any(item["event_type"] == "fire_smoke_cleared" for item in events(tmp_path))
    for index in range(8, 18):
        clock.value += 1
        pipeline.process(packet(index))
    output = events(tmp_path)
    suspected = next(item for item in output if item["event_type"] == "fire_smoke_suspected")
    cleared = next(item for item in output if item["event_type"] == "fire_smoke_cleared")
    assert cleared["hazard_episode_id"] == suspected["hazard_episode_id"]

def test_low_score_hazard_does_not_become_event(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    pipeline = create_pipeline("cam", config([binding("hazard", "fire_smoke", kind=None)]))
    for index in range(4):
        sample = packet(index)
        sample.detections.append(SimpleNamespace(model_name="smoke_fire", class_name="smoke",
                                                 bbox=(20, 20, 60, 60), confidence=.4))
        pipeline.process(sample)
    assert not any(item["event_type"] == "fire_smoke_suspected" for item in events(tmp_path))

def test_plate_attempt_has_one_failure_outcome_and_later_correction(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline("cam", config([binding("plate-a", "anpr"), binding("plate-b", "anpr")]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    attempted = packet(3, [("car", 1, (20, 20, 70, 75))])
    attempted.detections.append(SimpleNamespace(model_name="license_plate", bbox=(30, 50, 50, 60),
                                                 confidence=.8, metadata={"ocr_provisional": "KA01"}, parent_id=1))
    pipeline.process(attempted)
    clock.value += 13
    pipeline.process(packet(4, [("car", 1, (20, 20, 70, 75))]))
    outcomes = [item for item in events(tmp_path) if item["event_type"] == "plate_outcome"]
    assert len(outcomes) == 2 and all(item["outcome"] == "unreadable" for item in outcomes)
    for outcome in outcomes:
        record = package_record(tmp_path, outcome["observation_id"])
        assert record["evidence"][0]["metadata"]["evidence_type"] == "frame"
    first_read = add_quality_plate(
        packet(5, [("car", 1, (20, 20, 70, 75))]), "KA01AB1234", .35,
    )
    pipeline.process(first_read)
    clock.value += .1
    selected_read = add_quality_plate(
        packet(6, [("car", 1, (20, 20, 70, 75))]), "KA01AB1234", .80,
    )
    pipeline.process(selected_read)
    clock.value += 2.6
    pipeline.process(packet(7, [("car", 1, (20, 20, 70, 75))]))
    clock.value += 5.1
    pipeline.process(add_quality_plate(
        packet(8, [("car", 1, (20, 20, 70, 75))]), "KA01AB1235", .95,
    ))
    clock.value += .1
    pipeline.process(add_quality_plate(
        packet(9, [("car", 1, (20, 20, 70, 75))]), "KA01AB1235", .95,
    ))
    reads = [item for item in events(tmp_path) if item["event_type"] == "plate_read"]
    assert len(reads) == 4
    by_binding = {key: [item for item in reads if item["use_case_id"] == key]
                  for key in ("plate-a", "plate-b")}
    for items in by_binding.values():
        first = next(item for item in items if item["supersedes_observation_id"] is None)
        correction = next(item for item in items if item["supersedes_observation_id"] is not None)
        assert correction["supersedes_observation_id"] == first["observation_id"]

def test_anpr_accepts_single_non_indian_in_range_read():
    stabilizer = OcrStabilizer()
    stabilizer.observe("cam", 7, "GARAGE-12", 0.26, 1, plate_width=80)
    assert stabilizer.confirmed_text("cam", 7) == "GARAGE12"
    assert stabilizer.confirmed_confidence("cam", 7) == pytest.approx(0.26)

def test_anpr_rejects_below_range_read_but_keeps_provisional():
    stabilizer = OcrStabilizer()
    stabilizer.observe("cam", 7, "GARAGE-12", 0.12, 1, plate_width=80)
    assert stabilizer.confirmed_text("cam", 7) is None
    assert stabilizer.text_for("cam", 7) == "GARAGE12"

def test_anpr_improved_confidence_supersedes_same_plate(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline("cam", config([binding("plate", "anpr")]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 1, (20, 20, 70, 75))]))
    pipeline.process(add_quality_plate(
        packet(3, [("car", 1, (20, 20, 70, 75))]), "KA01AB1234", .50,
    ))
    clock.value += .1
    pipeline.process(add_quality_plate(
        packet(4, [("car", 1, (20, 20, 70, 75))]), "KA01AB1234", .50,
    ))
    clock.value += 3.1
    pipeline.process(packet(5, [("car", 1, (20, 20, 70, 75))]))
    clock.value += 5.1
    pipeline.process(add_quality_plate(
        packet(6, [("car", 1, (20, 20, 70, 75))]), "KA01AB1234", .75,
    ))
    clock.value += .1
    pipeline.process(add_quality_plate(
        packet(7, [("car", 1, (20, 20, 70, 75))]), "KA01AB1234", .75,
    ))
    clock.value += 2.6
    pipeline.process(packet(8, [("car", 1, (20, 20, 70, 75))]))
    reads = sorted((item for item in events(tmp_path) if item["event_type"] == "plate_read"),
                   key=lambda item: item["confidence"])
    assert [item["confidence"] for item in reads] == [.50, .75]
    assert reads[1]["supersedes_observation_id"] == reads[0]["observation_id"]

def test_anpr_suppresses_noisy_alternate_plate_for_same_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    clock = SimpleNamespace(value=100.0, time=lambda: clock.value)
    monkeypatch.setattr(events_module, "time", clock)
    pipeline = create_pipeline("cam", config([binding("plate", "anpr")]))
    for index in range(3):
        pipeline.process(packet(index, [("car", 9, (20, 20, 70, 75))]))
        clock.value += .1
    good = add_quality_plate(
        packet(3, [("car", 9, (20, 20, 70, 75))]),
        "KA03NP4277", .95, parent_id=9,
    )
    pipeline.process(good)
    for index, (text, confidence) in enumerate([
        ("KA03BP4277", .91),
        ("KA1TBB4377", .73),
        ("AP1BBD4137", .51),
        ("AP22A8777", .47),
    ], start=4):
        clock.value += 6
        noisy = add_quality_plate(
            packet(index, [("car", 9, (20, 20, 70, 75))]),
            text, confidence, parent_id=9,
        )
        pipeline.process(noisy)
    reads = [item for item in events(tmp_path) if item["event_type"] == "plate_read"]
    assert [item["plate_text"] for item in reads] == ["KA03NP4277"]
