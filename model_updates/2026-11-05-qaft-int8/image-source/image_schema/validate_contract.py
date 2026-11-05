"""Validate the self-contained Sentry V8 CV/delivery-agent contract."""

import copy
import hashlib
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
FORMATS = FormatChecker()


def read_json(name):
    return json.loads((ROOT / name).read_text())


def check(schema, instance):
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FORMATS).validate(instance)


contract = yaml.safe_load((ROOT / "sentry-v8-contract.yaml").read_text())
release = yaml.safe_load((ROOT / "release.yaml").read_text())
schemas = {path.name: read_json(path.name) for path in ROOT.glob("*.schema.json")}

assert contract["name"] == "sentry-v8"
assert contract["schemaVersion"] == "8.0"
assert contract["wireSchemaVersion"] == "7.0"
assert contract["compatibility"]["controlPlaneIngestChanged"] is False
assert contract["imageRegistration"]["contractVersion"] == "8.0"
assert all((ROOT / filename).is_file() for filename in contract["schemas"].values())

desired = read_json("desired-state.example.json")
check(schemas["desired-state.schema.json"], desired)
assert desired["contract"] == "sentry-v8"
assert desired["schema_version"] == "8.0"
assert desired["edge_id"] == desired["deployment_id"]
desired_without_fps = copy.deepcopy(desired)
desired_without_fps["cameras"][0].pop("fps")
check(schemas["desired-state.schema.json"], desired_without_fps)
assert schemas["desired-state.schema.json"]["$defs"]["camera"]["properties"]["fps"]["default"] == 8
configured_fps = contract["retention"]["physicalBox"]["configuredFPS"]
assert configured_fps["defaultPerCamera"] == 8
assert configured_fps["aggregateFormula"] == "sum_of_effective_camera_fps"
assert configured_fps["fixedAggregateMaximum"] is None

events = read_json("observation-examples.json")
for event in events:
    check(schemas["observation.schema.json"], event)
check(schemas["evidence.schema.json"], read_json("evidence.example.json"))
check(schemas["multi-box-delivery.schema.json"], read_json("multi-box-delivery.example.json"))

example = read_json("delivery-agent-package.example.json")
manifest_schema = copy.deepcopy(schemas["delivery-agent-package.schema.json"])
manifest_schema["properties"]["observation"] = {"type": "object"}
manifest_schema["properties"]["evidence"]["items"]["properties"]["metadata"] = {"type": "object"}
manifest_schema["properties"]["embeddings"]["items"] = {"type": "object"}
check(manifest_schema, example["manifest"])
check(schemas["delivery-agent-acknowledgement.schema.json"], example["durable_acknowledgement"])
check(schemas["delivery-agent-status.schema.json"], {
    "deployment_id": "north-yard", "outbox_used_bytes": 1024,
    "outbox_max_bytes": 2147483648, "states": {
        "pending": 2, "acked": 0, "dead_letter": 0, "skip_pending": 0,
        "skipped": 0, "resolved_by_remote_receipt": 0,
    },
    "pending_record_count": 2, "pending_package_count": 1,
    "oldest_pending_sequence": 44, "oldest_pending_age_seconds": 1.5,
    "highest_contiguous_sequence": 43, "highest_received_sequence": 43,
    "last_durable_package_at": 1790848802.0, "last_upload_at": None,
    "connection_state": "offline", "last_error_code": "ConnectError:offline",
    "stream_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "delivery_paused": False,
})
for status, code, retryable in (
    (409, "package_id_conflict", False),
    (429, "outbox_full", True),
    (503, "durability_unavailable", True),
):
    check(schemas["delivery-agent-error.schema.json"], {
        "error": {"code": code, "message": f"HTTP {status}", "retryable": retryable}
    })

manifest = example["manifest"]
observation = manifest["observation"]
check(schemas["observation.schema.json"], observation)
for item in manifest["evidence"]:
    check(schemas["evidence.schema.json"], item["metadata"])
for embedding in manifest["embeddings"]:
    check(schemas["embedding.schema.json"], embedding)
assert manifest["deployment_id"] == observation["deployment_id"]
assert len({item["part_name"] for item in manifest["evidence"]}) == len(manifest["evidence"])
for item in manifest["evidence"]:
    metadata = item["metadata"]
    assert metadata["observation_id"] == observation["observation_id"]
    assert metadata["evidence_id"] in observation.get("evidence_ids", [])
for embedding in manifest["embeddings"]:
    assert embedding["observation_id"] == observation["observation_id"]

ack = example["durable_acknowledgement"]
assert ack["package_id"] == manifest["package_id"]
assert ack["deployment_id"] == manifest["deployment_id"]
assert [row["sequence"] for row in ack["sequences"]] == list(
    range(ack["sequences"][0]["sequence"], ack["sequences"][0]["sequence"] + len(ack["sequences"]))
)
expected_objects = [
    ("observation", observation["observation_id"]),
    *(("evidence", item["metadata"]["evidence_id"]) for item in manifest["evidence"]),
    *(("embedding", item["embedding_id"]) for item in manifest["embeddings"]),
]
assert [(row["object_type"], row["object_id"]) for row in ack["sequences"]] == expected_objects

# Fixed digest vector: canonical manifest, then NUL/name/NUL/raw bytes in
# manifest evidence order. Multipart boundaries never enter this calculation.
canonical = json.dumps(
    manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
).encode()
digest = hashlib.sha256(canonical)
digest.update(b"\0evidence_0\0example-evidence-bytes")
assert digest.hexdigest() == "9c9d07ab8089dfc19348dc729065040734bd1e90e56537e7f57152a72ba4041b"

handoff = (ROOT / "DELIVERY_AGENT_IMAGE_HANDOFF.md").read_text()
normalized_handoff = " ".join(handoff.split())
for phrase in (
    "must never mount", "POST /v1/packages", "one serialized SQLite writer",
    "strict sequence order", "Redis absent", "Control-plane release steps",
    "Never run the same deployment on two boxes concurrently",
    "package digest is exactly SHA-256", "mode `0660`",
    "exactly one deployment-scoped handoff scheduler",
    "Delays before attempts 2–6 are exactly 250 ms",
    "Exact queue limits, event tiers, reserved capacity, and coalescing",
    "retention clock starts at the agent's durable",
):
    assert phrase in normalized_handoff, f"missing handoff requirement: {phrase}"

handoff_contract = contract["delivery"]["cvRuntimeHandoff"]
assert handoff_contract["version"] == "1.0"
assert handoff_contract["transport"] == "HTTP_over_Unix_domain_socket"
assert handoff_contract["socket"] == {
    "env": "SENTINEL_DELIVERY_AGENT_SOCKET",
    "value": "/run/sentinel-delivery/agent.sock",
    "ownerUid": 10001,
    "ownerGid": 10001,
    "mode": "0660",
    "requestTimeoutSeconds": 30,
    "maximumRequestBytes": 67108864,
    "maximumSimultaneousHandoffs": 4,
}
assert handoff_contract["producerScheduler"] == {
    "scope": "exactly_one_shared_scheduler_per_deployment_runtime",
    "perCameraRetryQueues": "forbidden",
    "boundedMemory": True,
    "maximumInFlightHandoffs": 4,
    "priorityPolicy": {
        "authority": "CV_production_policy",
        "requiredGuarantees": [
            "optional_or_replaceable_work_must_not_starve_non_replaceable_facts",
            "coalesce_or_drop_optional_work_before_rejecting_non_replaceable_work",
            "never_cancel_an_in_flight_handoff",
            "rejected_work_is_reported_and_never_claimed_as_produced",
        ],
        "tuningNotInContract": [
            "event_tier_mapping", "queue_package_limit", "queue_byte_limit",
            "reserved_capacity", "coalescing_keys",
        ],
    },
    "queueFullAction": "reject_new_package_and_report_delivery_backpressure_without_claiming_it_was_produced",
    "retryPolicy": {
        "totalAttempts": 6,
        "delaysMilliseconds": [250, 500, 1000, 2000, 4000],
        "retryOn": [
            "network_error", "request_timeout", "HTTP_429_with_retryable_true",
            "HTTP_503_with_retryable_true",
        ],
        "retryPayload": "exact_same_package_id_manifest_and_evidence_bytes",
        "nonRetryable": [
            "HTTP_200_duplicate", "HTTP_201_durable", "HTTP_409", "other_4xx",
            "retryable_false",
        ],
        "exhaustedAction": "report_delivery_handoff_exhausted_release_memory_and_do_not_claim_produced",
    },
}
status_states = schemas["delivery-agent-status.schema.json"]["properties"]["states"]
assert set(status_states["required"]) == {
    "pending", "acked", "dead_letter", "skip_pending", "skipped",
    "resolved_by_remote_receipt",
}
assert "open_or_write_the_delivery_SQLite_database" in handoff_contract["cvRuntimeMustNot"]
assert contract["delivery"]["multiBoxProtocol"]["owner"] == "delivery_agent_sidecar"
assert contract["delivery"]["multiBoxProtocol"]["sending"]["maximumInFlightRequests"] == 1
assert contract["delivery"]["multiBoxProtocol"]["connectsToRedis"] is False
assert contract["resources"]["forbiddenCvRuntimeMounts"] == ["/state"]

expected_capabilities = {
    "hardware_profile": "intel-285h",
    "wire_schema_version": "7.0",
    "multi_box_delivery": "7.0",
    "delivery_agent_protocol": "1.0",
}
assert contract["imageRegistration"]["capabilities"] == expected_capabilities
assert release["capabilities"] == expected_capabilities
assert release["contract"] == "sentry-v8"
assert release["verification"]["static_contract"] == "passed"
edge_retention = contract["retention"]["edgeOutbox"]
assert edge_retention["ageMeasuredFrom"] == "durable_outbox_created_at"
assert edge_retention["observedAtUse"] == (
    "event_timeline_and_control_plane_retention_only_never_edge_outbox_expiration"
)

print(
    f"PASS: {len(schemas)} schemas, {len(events)} V7 wire examples, V8 desired state, "
    "digest/error/status contracts, bounded local handoff, and single-writer ownership"
)
