"""Exercise the v14 artifact-first face transaction across a container restart."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import urlopen

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"traffic-v14-face-delivery-{int(time.time())}"
NAME = "pipeline-traffic-v14-face-delivery"
IMAGE = "localhost/traffic-pilot-runtime:intel-285h-2026.09.21-v14"
API = "http://127.0.0.1:18080"
TOKEN = "v14-live-test-token"


class Receiver:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.allow_samples = False
        self.artifacts: dict[str, dict] = {}
        self.artifact_requests: dict[str, int] = {}
        self.samples: dict[str, dict] = {}
        self.sample_attempts: dict[str, int] = {}

    def handler(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self.respond(401, {})
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path.startswith("/internal/face-artifacts/"):
                    sample_id = self.path.rsplit("/", 1)[1]
                    digest = "sha256:" + hashlib.sha256(body).hexdigest()
                    assert self.headers["X-ApexFabric-Artifact-SHA256"] == digest
                    with receiver.lock:
                        receiver.artifact_requests[sample_id] = receiver.artifact_requests.get(sample_id, 0) + 1
                        previous = receiver.artifacts.get(sample_id)
                        if previous:
                            assert previous["body"] == body and previous["sha256"] == digest
                        receiver.artifacts[sample_id] = {"body": body, "sha256": digest}
                    return self.respond(200 if previous else 201, {
                        "artifact_id": "artifact-sha256-" + digest[7:],
                        "sample_id": sample_id,
                        "sha256": digest,
                        "content_type": self.headers["Content-Type"],
                        "size_bytes": len(body),
                        "status": "existing" if previous else "created",
                    })
                if self.path == "/internal/face-samples":
                    sample = json.loads(body)
                    sample_id = sample["sample_id"]
                    with receiver.lock:
                        receiver.sample_attempts[sample_id] = receiver.sample_attempts.get(sample_id, 0) + 1
                        allowed = receiver.allow_samples
                        if allowed:
                            receiver.samples[sample_id] = sample
                    if not allowed:
                        return self.respond(503, {"error": "simulated outage"})
                    return self.respond(201, {"sample_id": sample_id, "person_id": "management-owned"})
                self.respond(404, {})

            def respond(self, status: int, payload: dict):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def desired_state() -> dict:
    return {
        "edge_id": "traffic-v14-live-test",
        "revision": 1,
        "cameras": [{
            "camera_id": "camera7",
            "source": "file:/run/secrets/apexfabric/camera7.url",
            "solution_pack": "sporada-secure",
            "fps": 5,
            "apps": ["face_recognition"],
            "config": {
                "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
                "emission": {"minimum_quality": 0.2, "cooldown_seconds": 5,
                             "material_change_threshold": 0.15},
                "zones": {},
            },
        }],
    }


def wait_until(predicate, message: str, timeout: int = 180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1)
    raise AssertionError(message)


def ready():
    try:
        with urlopen(API + "/readyz", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def main() -> None:
    for folder in ("configs", "secrets", "state"):
        (RUN / folder).mkdir(parents=True, exist_ok=True)
    (RUN / "configs/desired_state.json").write_text(json.dumps(desired_state()))
    (RUN / "secrets/camera7.url").write_text(
        "rtsp://admin:Admin123_@192.168.1.7:554/video/live?channel=1&subtype=0\n"
    )
    receiver = Receiver()
    server = ThreadingHTTPServer(("127.0.0.1", 18444), receiver.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
    command = [
        "podman", "run", "-d", "--name", NAME, "--network=host",
        "--group-add", "keep-groups", "--security-opt", "label=disable",
        "--device", "/dev/dri:/dev/dri", "--device", "/dev/accel:/dev/accel",
        "-e", "APEX_API_PORT=18080", "-e", "APEXFABRIC_FACE_IDENTITY_TOKEN=" + TOKEN,
        "-e", "APEXFABRIC_FACE_ARTIFACT_URL_TEMPLATE=http://127.0.0.1:18444/internal/face-artifacts/<sample-id>",
        "-e", "APEXFABRIC_FACE_IDENTITY_URL=http://127.0.0.1:18444/internal/face-samples",
        "-e", "FACE_PROCESS_INTERVAL=1",
        "-v", f"{RUN / 'configs'}:/configs:ro", "-v", f"{RUN / 'secrets'}:/run/secrets/apexfabric:ro",
        "-v", f"{RUN / 'state'}:/state:U", IMAGE,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        wait_until(ready, "v14 image did not become ready")

        sample_id = wait_until(
            lambda: next(iter(receiver.sample_attempts), None),
            "no face artifact/sample transaction reached management",
        )
        wait_until(
            lambda: next((path for path in (RUN / "state/face_samples/outbox/camera7").glob("*.json")
                          if json.loads(path.read_text())["sample"]["sample_id"] == sample_id), None),
            "artifact acknowledgement was not persisted",
        )
        with receiver.lock:
            initial_artifact_requests = receiver.artifact_requests[sample_id]
            artifact = dict(receiver.artifacts[sample_id])

        subprocess.run(["podman", "restart", "-t", "10", NAME], check=True, stdout=subprocess.DEVNULL)
        wait_until(ready, "v14 image did not recover after restart")
        with receiver.lock:
            receiver.allow_samples = True
        sample = wait_until(lambda: receiver.samples.get(sample_id), "pending embedding was not resumed")

        schema = json.loads((ROOT / "delivery/apexfabric-v1/intel-285h/traffic-v14/face-sample.schema.json").read_text())
        jsonschema.validate(sample, schema)
        assert receiver.artifact_requests[sample_id] == initial_artifact_requests == 1
        assert sample["artifact_sha256"] == artifact["sha256"]
        assert sample["artifact_id"] == "artifact-sha256-" + artifact["sha256"][7:]
        assert len(sample["embedding"]) == 512
        assert not list((RUN / "state/face_samples/outbox/camera7").glob(f"{sample_id}.json"))
        journal = RUN / "state/events/analytics.jsonl"
        assert "\"embedding\"" not in journal.read_text(errors="replace")

        report = {
            "image": IMAGE,
            "sample_id": sample_id,
            "artifact_bytes": len(artifact["body"]),
            "artifact_requests_for_resumed_sample": receiver.artifact_requests[sample_id],
            "sample_attempts": receiver.sample_attempts[sample_id],
            "restart_resumed_embedding_only": True,
            "sse_client_connected": False,
            "embedding_dimensions": len(sample["embedding"]),
        }
        (RUN / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
        print(f"Artifacts: {RUN}", flush=True)
    finally:
        logs = subprocess.run(["podman", "logs", NAME], capture_output=True, text=True)
        (RUN / "container.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
