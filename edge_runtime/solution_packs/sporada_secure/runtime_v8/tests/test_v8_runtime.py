from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_policy
import json
import multiprocessing as mp
from pathlib import Path
import socketserver
import sys
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "worker"))

from pipeline.sentinel_v8_handoff import (  # noqa: E402
    PRIORITY_CRITICAL,
    PRIORITY_DURABLE,
    PRIORITY_IDENTITY,
    PRIORITY_REPLACEABLE,
    V8HandoffClient,
    V8HandoffScheduler,
    canonical_json_bytes,
    create_handoff_channels,
    package_digest,
)
from traffic_pilot_runtime.desired_state import DesiredCamera, DesiredState  # noqa: E402
from traffic_pilot_runtime.graph import compile_runtime_plan  # noqa: E402
from pipeline.runtime_telemetry import CameraTelemetry  # noqa: E402
from detectors.backends.openvino_ocr_async import resolve_ocr_queue_depth  # noqa: E402


def _observation(event_type: str = "object_present", **values):
    payload = {
        "schema_version": "7.0",
        "observation_id": str(uuid.uuid4()),
        "deployment_id": "test-deployment",
        "camera_id": "cam-1",
        "config_revision": 1,
        "stream_session_id": str(uuid.uuid4()),
        "use_case_id": "presence-1",
        "event_type": event_type,
        "observed_at": "2026-11-02T08:00:00Z",
        "source_frame_id": "cam-1:1",
        "evidence_ids": [],
        "embedding_ids": [],
    }
    payload.update(values)
    return payload


def test_ocr_queue_depth_is_safe_and_bounded(monkeypatch):
    monkeypatch.delenv("OCR_QUEUE_DEPTH", raising=False)
    assert resolve_ocr_queue_depth() == 4
    assert resolve_ocr_queue_depth("1") == 1
    assert resolve_ocr_queue_depth("6") == 6
    assert resolve_ocr_queue_depth("0") == 1
    assert resolve_ocr_queue_depth("16") == 8


class _AgentHandler(socketserver.StreamRequestHandler):
    received = []
    next_sequence = 1

    def handle(self):
        request_line = self.rfile.readline().decode("ascii").strip()
        assert request_line == "POST /v1/packages HTTP/1.1"
        headers = {}
        while True:
            line = self.rfile.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            key, value = line.decode("latin-1").split(":", 1)
            headers[key.lower()] = value.strip()
        body = self.rfile.read(int(headers["content-length"]))
        message = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + body
        )
        parts = {part.get_param("name", header="content-disposition"):
                 part.get_payload(decode=True) for part in message.iter_parts()}
        manifest = json.loads(parts.pop("manifest"))
        assert set(parts) == {item["part_name"] for item in manifest["evidence"]}
        self.__class__.received.append((manifest, parts))
        rows = [("observation", manifest["observation"]["observation_id"])]
        rows.extend(("evidence", item["metadata"]["evidence_id"])
                    for item in manifest["evidence"])
        rows.extend(("embedding", item["embedding_id"])
                    for item in manifest["embeddings"])
        first = self.__class__.next_sequence
        self.__class__.next_sequence += len(rows)
        response = {
            "status": "durable",
            "package_id": manifest["package_id"],
            "deployment_id": manifest["deployment_id"],
            "sequences": [
                {"sequence": first + index, "object_type": kind, "object_id": identifier}
                for index, (kind, identifier) in enumerate(rows)
            ],
        }
        encoded = canonical_json_bytes(response)
        self.wfile.write(
            b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode()
            + encoded
        )


def test_digest_is_stable_and_sensitive_to_evidence_order():
    manifest = {"z": 1, "a": "value"}
    expected = package_digest(manifest, [("evidence_0", b"one"), ("evidence_1", b"two")])
    assert expected == package_digest(
        {"a": "value", "z": 1}, [("evidence_0", b"one"), ("evidence_1", b"two")]
    )
    assert expected != package_digest(
        manifest, [("evidence_1", b"two"), ("evidence_0", b"one")]
    )


def test_priority_is_cv_policy_not_desired_state():
    parent, child = mp.Pipe(duplex=True)
    client = V8HandoffClient(child, "test-deployment", "cam-1")
    assert client._priority(_observation("fire_smoke_suspected")) == (PRIORITY_CRITICAL, False)
    assert client._priority(_observation("zone_entry")) == (PRIORITY_DURABLE, False)
    assert client._priority(_observation("person_feature_sample")) == (PRIORITY_IDENTITY, False)
    assert client._priority(_observation("scene_sample", trigger="interval")) == (
        PRIORITY_REPLACEABLE, True
    )
    client.close()
    parent.close()


def test_complete_package_reaches_agent_over_unix_socket(tmp_path):
    socket_path = tmp_path / "agent.sock"
    status_path = tmp_path / "handoff.json"
    _AgentHandler.received = []
    _AgentHandler.next_sequence = 1
    server = socketserver.UnixStreamServer(str(socket_path), _AgentHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    context = mp.get_context("spawn")
    parents, children = create_handoff_channels(context, ["cam-1"])
    scheduler = V8HandoffScheduler(parents, str(socket_path), status_path).start()
    client = V8HandoffClient(children["cam-1"], "test-deployment", "cam-1")
    image = b"jpeg-evidence"
    evidence_id = str(uuid.uuid4())
    observation = _observation()
    observation["evidence_ids"] = [evidence_id]
    evidence = {
        "schema_version": "7.0",
        "evidence_id": evidence_id,
        "observation_id": observation["observation_id"],
        "evidence_type": "frame",
        "captured_at": observation["observed_at"],
        "source_frame_id": observation["source_frame_id"],
        "source_bbox_normalized": None,
        "content_type": "image/jpeg",
        "width": 4,
        "height": 4,
        "size_bytes": len(image),
        "sha256": "sha256:" + __import__("hashlib").sha256(image).hexdigest(),
    }
    assert client.persist(observation, [(evidence, image)])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not _AgentHandler.received:
        time.sleep(0.01)
    assert len(_AgentHandler.received) == 1
    manifest, parts = _AgentHandler.received[0]
    assert manifest["observation"] == observation
    assert parts == {"evidence_0": image}
    deadline = time.monotonic() + 3
    status = {}
    while time.monotonic() < deadline:
        status = json.loads(status_path.read_text())
        if status["counters"]["durable"] == 1:
            break
        time.sleep(0.01)
    assert status["retained_packages"] == 0
    assert status["counters"]["durable"] == 1
    client.close()
    scheduler.close()
    server.shutdown()
    server.server_close()


def test_v8_desired_state_and_plan_have_no_cv_outbox(tmp_path):
    example = json.loads((ROOT / "image_schema" / "desired-state.example.json").read_text())
    source = example["cameras"][0]
    desired = DesiredState(
        edge_id=example["edge_id"], deployment_id=example["deployment_id"],
        revision=example["revision"], content_hash="test",
        cameras=(DesiredCamera(
            camera_id=source["camera_id"], source=source["source"], fps=source["fps"],
            apps=("v8_events", "face_recognition", "person_reid", "scene_embeddings"),
            config={"v8": {
                "deployment_id": example["deployment_id"],
                "config_revision": example["revision"],
                "bindings": source["bindings"], "geometry": source["geometry"],
            }},
        ),),
    )
    plan = compile_runtime_plan(desired).to_dict()
    node_ids = {node for camera in plan["cameras"] for node in camera["node_ids"]}
    assert "v8_handoff_scheduler" in node_ids
    assert "delivery_agent_socket" in node_ids
    assert not any("outbox" in node or "uploader" in node for node in node_ids)


def test_v8_output_telemetry_reports_handoff_without_claiming_durability(tmp_path, monkeypatch):
    monkeypatch.setenv("APEXFABRIC_STATE_ROOT", str(tmp_path))
    telemetry = CameraTelemetry("cam-1", target_fps=8, devices={})
    event = _observation("scene_sample", trigger="interval")
    telemetry.event_handoff_admitted(event, 0.01)
    snapshot = telemetry.flush(force=True)
    assert snapshot["processing"]["totals"]["events_handoff_admitted"] == 1
    assert "events_persisted" not in snapshot["processing"]["totals"]
    assert snapshot["outputs"]["by_event_type"]["scene_sample"]["total"] == 1
    assert snapshot["processing"]["output_events_per_second"] > 0
