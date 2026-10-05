"""Validate the V5.2 draft package and representative boundary decisions."""

import copy
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
CHECK_FORMATS = FormatChecker()


def read_json(name):
    return json.loads((ROOT / name).read_text())


def validator(schema):
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=CHECK_FORMATS)


def rejected(checker, instance):
    assert not checker.is_valid(instance), f"Unexpectedly accepted: {instance}"


contract = yaml.safe_load((ROOT / "sentry-v5-contract.yaml").read_text())
assert contract["schemaVersion"] == "5.2"
assert all((ROOT / name).is_file() for name in contract["schemas"].values())
schemas = {path.name: read_json(path.name) for path in ROOT.glob("*.schema.json")}
checks = {name: validator(schema) for name, schema in schemas.items()}

desired = read_json("desired-state.example.json")
checks["desired-state.schema.json"].validate(desired)
evidence = read_json("evidence.example.json")
checks["evidence.schema.json"].validate(evidence)
events = read_json("observation-examples.json")
for event in events:
    checks["observation.schema.json"].validate(event)

event_types = {
    branch["properties"]["event_type"]["const"]
    for branch in schemas["observation.schema.json"]["oneOf"]
}
assert event_types == set(contract["outputSemantics"])
assert event_types == {event["event_type"] for event in events}
assert set(desired["cameras"][0]["bindings"][0]) >= {
    "use_case_id", "app", "scope", "geometry_ids"
}
old_apps = read_json("../sentry-v5-desired-state.schema.json")["$defs"]["camera"]["properties"]["apps"]["items"]["enum"]
assert set(old_apps) == set(schemas["desired-state.schema.json"]["$defs"]["app"]["enum"])

camera = desired["cameras"][0]
geometry = {
    **{item["id"]: "polygon" for item in camera["geometry"]["zones"]},
    **{item["id"]: "line" for item in camera["geometry"]["counting_lines"]},
}
binding_ids = [item["use_case_id"] for item in camera["bindings"]]
assert len(binding_ids) == len(set(binding_ids))
for binding in camera["bindings"]:
    assert all(geometry[item] == ("line" if binding["app"] in {
        "vehicle_entry_exit_counts", "people_entry_exit_counts", "line_crossing"
    } else "polygon") for item in binding["geometry_ids"])
    assert all(item in binding_ids and item != binding["use_case_id"]
               for item in binding.get("scene_trigger_use_case_ids", []))

owner_policy = contract["subjectEventOwnership"]
assert owner_policy["tieBreak"] == "lexicographically_smallest_use_case_id"
owner_candidates = [item for item in camera["bindings"]
                    if item["app"] in {"vehicle_presence", "anpr"}
                    and "parking-zone" in item["geometry_ids"]]
owner = min(owner_candidates,
            key=lambda item: (0 if item["app"] == "vehicle_presence" else 1,
                              item["use_case_id"]))
assert next(item for item in events if item["event_type"] == "object_present")["use_case_id"] == owner["use_case_id"]
assert min(["z-presence", "a-presence"]) == "a-presence"

plate = next(event for event in events if event["event_type"] == "plate_read")
assert evidence["evidence_type"] == "frame"
assert evidence["evidence_id"] in plate["evidence_ids"]
assert evidence["observation_id"] == plate["observation_id"]
assert evidence["source_frame_id"] == plate["source_frame_id"]
assert evidence["captured_at"] == plate["observed_at"]

full_frame = copy.deepcopy(desired)
full_frame["cameras"][0].pop("geometry")
full_frame["cameras"][0]["bindings"] = [
    {"use_case_id": "all-vehicle-count", "app": "vehicle_counting",
     "scope": "full_frame", "geometry_ids": [], "object_types": ["vehicle"]},
    {"use_case_id": "body-only", "app": "person_identity",
     "scope": "full_frame", "geometry_ids": [], "object_types": ["person"],
     "embedding_profile_ids": ["transreid-ssl-v18.1"]},
]
checks["desired-state.schema.json"].validate(full_frame)

polygon = next(item for item in camera["geometry"]["zones"]
               if item["id"] == "parking-zone")["poly"]
xs, ys = zip(*polygon)
rect = {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}
assert contract["sceneRegionCrop"]["outsidePolygonMask"] == "none"
assert contract["sceneRegionCrop"]["padding"] == "none"
region_state = copy.deepcopy(desired)
region_binding = copy.deepcopy(next(item for item in camera["bindings"]
                                    if item["app"] == "scene_search"))
region_binding.update(use_case_id="parking-scene", scope="geometry",
                      geometry_ids=["parking-zone"])
region_state["cameras"][0]["bindings"].append(region_binding)
checks["desired-state.schema.json"].validate(region_state)
region_event = copy.deepcopy(next(item for item in events
                                  if item["event_type"] == "scene_sample"))
region_event.update(observation_id="35353535-3535-4535-8535-353535353535",
                    use_case_id="parking-scene", scope="geometry",
                    zone_id="parking-zone",
                    evidence_ids=["36363636-3636-4636-8636-363636363636"])
checks["observation.schema.json"].validate(region_event)
region_evidence = copy.deepcopy(evidence)
region_evidence.update(evidence_id=region_event["evidence_ids"][0],
                       observation_id=region_event["observation_id"],
                       evidence_type="scene_region_crop",
                       captured_at=region_event["observed_at"],
                       source_frame_id=region_event["source_frame_id"],
                       source_bbox_normalized=rect)
checks["evidence.schema.json"].validate(region_evidence)

bad = copy.deepcopy(desired)
bad["cameras"][0]["apps"] = ["anpr"]
rejected(checks["desired-state.schema.json"], bad)
bad = copy.deepcopy(desired)
bad["cameras"][0]["bindings"][4]["embedding_profile_ids"] = ["siglip2-base-v1"]
rejected(checks["desired-state.schema.json"], bad)
bad = copy.deepcopy(desired)
bad["cameras"][0]["bindings"][0]["sample_interval_seconds"] = 2
rejected(checks["desired-state.schema.json"], bad)

bad = copy.deepcopy(evidence)
bad["evidence_type"] = "plate_crop"
rejected(checks["evidence.schema.json"], bad)
bad["evidence_type"] = "aligned_face_chip"
rejected(checks["evidence.schema.json"], bad)

bad = copy.deepcopy(events[0])
bad["recheck_id"] = "12121212-1212-4212-8212-121212121212"
rejected(checks["observation.schema.json"], bad)
bad = copy.deepcopy(events[0])
bad["plate_text"] = "KA01AB1234"
rejected(checks["observation.schema.json"], bad)
bad = copy.deepcopy(next(x for x in events if x["event_type"] == "plate_read"))
bad["evidence_ids"] = []
rejected(checks["observation.schema.json"], bad)

embedding = {
    "schema_version": "5.2",
    "embedding_id": "14141414-1414-4414-8414-141414141414",
    "observation_id": "88888888-8888-4888-8888-888888888888",
    "use_case_id": "person-search",
    "evidence_id": "13131313-1313-4313-8313-131313131313",
    "profile_id": "adaface-ir101-v18.1",
    "kind": "face",
    "vector": [1.0] + [0.0] * 511,
    "quality": 0.82,
    "captured_at": "2026-10-01T10:03:00Z",
    "source_frame_id": "entrance-camera:3840",
    "presence_id": "12121212-1212-4212-8212-121212121212",
    "model_id": "adaface-ir101",
    "model_version": "qualified-revision",
    "preprocessing_id": "alignment-v1",
    "normalization": "l2",
}
checks["embedding.schema.json"].validate(embedding)
bad = copy.deepcopy(embedding)
del bad["quality"]
rejected(checks["embedding.schema.json"], bad)
scene_embedding = copy.deepcopy(embedding)
scene_embedding.update(profile_id="siglip2-base-v1", kind="semantic",
                       vector=[1.0] + [0.0] * 767, presence_id=None,
                       observation_id="55555555-5555-4555-8555-555555555555",
                       evidence_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
                       use_case_id="scene-history")
del scene_embedding["quality"]
checks["embedding.schema.json"].validate(scene_embedding)
scene_embedding["quality"] = 1.0
rejected(checks["embedding.schema.json"], scene_embedding)
subject_semantic = copy.deepcopy(scene_embedding)
del subject_semantic["quality"]
subject_semantic.update(presence_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        observation_id="11111111-1111-4111-8111-111111111111",
                        evidence_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                        use_case_id="parking-presence")
checks["embedding.schema.json"].validate(subject_semantic)
subject_event = copy.deepcopy(next(item for item in events if item["event_type"] == "object_present"))
subject_event["embedding_ids"] = [subject_semantic["embedding_id"]]
checks["observation.schema.json"].validate(subject_event)

print(f"PASS: {len(schemas)} schemas, {len(events)} event examples, V5 app catalog, "
      "cross-references, and stale-field checks")
