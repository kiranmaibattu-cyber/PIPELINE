#!/usr/bin/env python3
from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import shutil
import ssl
import subprocess
import threading
import time
from urllib.request import urlopen

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"sporada-v17-crossing-{int(time.time())}"
IMAGE = "localhost/traffic-pilot-runtime:intel-285h-2026.09.22-v17"
NAME = "sporada-v17-crossing-test"
API = "http://127.0.0.1:18087"
TOKEN = "crossing-live-test-token"


class Receiver:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records: dict[str, dict] = {}

    def handler(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                if self.path != "/internal/vehicle-crossings":
                    return self.respond(404, {"error": "not found"})
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self.respond(401, {"error": "unauthorized"})
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                message = BytesParser(policy=default).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() +
                    b"\r\nMIME-Version: 1.0\r\n\r\n" + body
                )
                parts = {
                    part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                    for part in message.iter_parts()
                }
                event = json.loads(parts["event"])
                image = parts["snapshot"]
                digest = hashlib.sha256(image).hexdigest()
                with receiver.lock:
                    existed = event["event_id"] in receiver.records
                    receiver.records[event["event_id"]] = {
                        "event": event,
                        "sha256": digest,
                        "size_bytes": len(image),
                    }
                self.respond(200 if existed else 201, {
                    "event_id": event["event_id"],
                    "status": "existing" if existed else "created",
                    "artifact_id": "artifact-sha256-" + digest,
                    "artifact_sha256": "sha256:" + digest,
                })

            def respond(self, status, payload):
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


def api(path: str) -> str:
    with urlopen(API + path, timeout=5) as response:
        return response.read().decode()


def metric_value(metrics: str, name: str, camera_id: str) -> int | None:
    match = re.search(rf'^{re.escape(name)}\{{camera_id="{re.escape(camera_id)}"\}} (\d+)$', metrics, re.MULTILINE)
    return int(match.group(1)) if match else None


def main() -> None:
    shutil.rmtree(RUN, ignore_errors=True)
    for folder in ("configs", "secrets", "state", "received"):
        (RUN / folder).mkdir(parents=True, exist_ok=True)
    (RUN / "secrets/traffic.url").write_text("rtsp://192.168.1.95:8554/traffic1\n")
    (RUN / "configs/desired_state.json").write_text(json.dumps({
        "edge_id": "v17-crossing-test",
        "revision": 1,
        "cameras": [{
            "camera_id": "traffic-live",
            "source": "file:/run/secrets/apexfabric/traffic.url",
            "solution_pack": "sporada-secure",
            "fps": 8,
            "apps": ["vehicle_entry_exit_counts"],
            "config": {
                "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
                "emission": {"minimum_quality": 0.55, "cooldown_seconds": 30,
                             "material_change_threshold": 0.15},
                "zones": {},
                "counting_lines": {"vehicle_entry_exit_counts": [
                    {"id": "horizontal", "name": "Horizontal", "a": [0.0, 0.5], "b": [1.0, 0.5],
                     "direction_mapping": {"right_to_left": "in", "left_to_right": "out"},
                     "minimum_track_age_frames": 3, "minimum_crossing_displacement": 0.01,
                     "crossing_cooldown_seconds": 10},
                    {"id": "vertical", "name": "Vertical", "a": [0.5, 0.0], "b": [0.5, 1.0],
                     "direction_mapping": {"right_to_left": "in", "left_to_right": "out"},
                     "minimum_track_age_frames": 3, "minimum_crossing_displacement": 0.01,
                     "crossing_cooldown_seconds": 10}
                ]}
            }
        }]
    }, indent=2))
    receiver = Receiver()
    server = ThreadingHTTPServer(("127.0.0.1", 19017), receiver.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
    command = [
        "podman", "run", "-d", "--name", NAME, "--network=host", "--group-add", "keep-groups",
        "--security-opt", "label=disable", "-e", "APEX_API_PORT=18087",
        "-e", "APEXFABRIC_FACE_IDENTITY_TOKEN=" + TOKEN,
        "-e", "APEXFABRIC_VEHICLE_CROSSING_URL=http://127.0.0.1:19017/internal/vehicle-crossings",
        "-v", f"{RUN / 'configs'}:/configs:ro", "-v", f"{RUN / 'secrets'}:/run/secrets/apexfabric:ro",
        "-v", f"{RUN / 'state'}:/state:U",
    ]
    for device in ("/dev/dri", "/dev/accel"):
        if Path(device).exists():
            command.extend(["--device", f"{device}:{device}"])
    command.append(IMAGE)
    try:
        subprocess.run(command, check=True, capture_output=True)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                if json.loads(api("/readyz")).get("ready"):
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise AssertionError("runtime did not become ready")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with receiver.lock:
                if receiver.records:
                    break
            time.sleep(2)
        with receiver.lock:
            records = dict(receiver.records)
        metrics = ""
        for _ in range(20):
            metrics = api("/metrics")
            acknowledged = metric_value(metrics, "vehicle_crossing_acknowledgements_total", "traffic-live")
            outbox_records = metric_value(metrics, "vehicle_crossing_outbox_records", "traffic-live")
            if acknowledged is not None and acknowledged >= len(records) and outbox_records == 0:
                break
            time.sleep(0.25)
        journal = RUN / "state/events/analytics.jsonl"
        journal_text = journal.read_text(errors="replace") if journal.exists() else ""
        assert records, "no vehicle crossed either test line during the live window"
        assert "vehicle_entry_exit_crossed" not in journal_text
        assert acknowledged is not None and acknowledged >= len(records)
        assert outbox_records == 0
        assert all(row["event"]["vehicle"]["class"] for row in records.values())
        validator = Draft202012Validator(json.loads(
            (ROOT / "delivery/apexfabric-v1/intel-285h/traffic-v17/vehicle-crossing.schema.json").read_text()
        ))
        for row in records.values():
            validator.validate(row["event"])
        report = {
            "crossings_received": len(records),
            "directions": sorted({row["event"]["direction"] for row in records.values()}),
            "classes": sorted({row["event"]["vehicle"]["class"] for row in records.values()}),
            "sse_duplicate": False,
            "ack_metric_present": True,
            "outbox_drained": True,
            "schema_validated": True,
        }
        (RUN / "received/records.json").write_text(json.dumps(records, indent=2))
        (RUN / "metrics.prom").write_text(metrics)
        (RUN / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        logs = subprocess.run(["podman", "logs", NAME], capture_output=True, text=True)
        (RUN / "container.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
        server.shutdown()
        server.server_close()
        print("Artifacts:", RUN)


if __name__ == "__main__":
    main()
