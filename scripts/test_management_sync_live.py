"""HTTPS management simulator for the real Surveillance container, not a management app."""
import hashlib
import os
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import ssl
import subprocess
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"exact-evidence-management-{int(time.time())}"
NAME = "pipeline-management-sync-test"
IMAGE = os.environ.get("TEST_IMAGE", "localhost/surveillance-edge-runtime:intel-285h-2026.09.10-v4")
API = "http://127.0.0.1:18081"


class Simulator:
    def __init__(self):
        self.lock = threading.RLock()
        self.commands, self.results, self.records, self.artifacts = [], {}, {}, set()
        self.offline = False
        self.token = secrets.token_urlsafe(32)
        self.requests = 0
        self.status = {}

    def handler(self):
        sim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                with sim.lock:
                    sim.requests += 1
                    if sim.offline:
                        return self.respond(503, {"error": "simulated outage"})
                    if self.headers.get("Authorization") != "Bearer " + sim.token:
                        return self.respond(401, {})
                    if self.command == "GET" and self.path.endswith("/commands"):
                        return self.respond(200, {"commands": [c for c in sim.commands if c["command_id"] not in sim.results][:20]})
                    data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    if self.command == "PUT" and "/artifacts/" in self.path:
                        digest = self.path.rsplit("/", 1)[1]
                        assert hashlib.sha256(data).hexdigest() == digest
                        (RUN / "received/artifacts" / digest).write_bytes(data)
                        sim.artifacts.add(digest)
                        return self.respond(200, {"sha256": digest})
                    payload = json.loads(data)
                    if self.path.endswith("/command-results"):
                        sim.results[payload["command_id"]] = payload
                        return self.respond(200, {"command_id": payload["command_id"]})
                    if self.path.endswith("/records"):
                        sim.records[payload["record_id"]] = payload
                        return self.respond(200, {"record_id": payload["record_id"]})
                    if self.path.endswith("/status"):
                        sim.status = payload
                        return self.respond(200, {"ok": True})
                    self.respond(404, {})

            do_GET = do_POST = do_PUT = handle_request

            def respond(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        return Handler

    def issue(self, cid, action, payload):
        with self.lock:
            self.commands.append(dict(command_id=cid, type=action, payload=payload))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            with self.lock:
                result = self.results.get(cid)
            if result:
                assert result["status"] == "applied", result
                return result["result"]
            time.sleep(1)
        raise AssertionError(f"No command result for {cid}")


def api(path):
    with urlopen(API + path, timeout=5) as response:
        return json.load(response)


def main():
    for folder in ("configs", "secrets", "state", "received/artifacts"):
        (RUN / folder).mkdir(parents=True)
    sim = Simulator()
    (RUN / "secrets/token").write_text(sim.token)
    (RUN / "secrets/ch9.rtsp").write_text("rtsp://192.168.1.95:8554/ch9\n")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(RUN / "server.key"), "-out", str(RUN / "secrets/ca.pem"),
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (RUN / "configs/management.json").write_text(json.dumps({
        "url": "https://127.0.0.1:18443", "token_file": "/run/secrets/apexfabric/token",
        "ca_file": "/run/secrets/apexfabric/ca.pem"}))
    (RUN / "configs/desired_state.json").write_text(json.dumps({
        "edge_id": "management-test-edge", "revision": 1, "cameras": [{
            "camera_id": "ch9", "source": "file:/run/secrets/apexfabric/ch9.rtsp",
            "solution_pack": "surveillance", "fps": 8, "apps": ["reid", "face_recognition", "people_counting"]}]}))
    server = ThreadingHTTPServer(("127.0.0.1", 18443), sim.handler())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(RUN / "secrets/ca.pem", RUN / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Artifacts: {RUN}", flush=True)
    try:
        subprocess.run(["podman", "run", "-d", "--name", NAME, "--network=host",
                        "--group-add", "keep-groups", "--security-opt", "label=disable",
                        "--device", "/dev/dri:/dev/dri", "--device", "/dev/accel:/dev/accel",
                        "-e", "APEX_API_PORT=18081", "-e", "MANAGEMENT_SYNC_CONFIG=/configs/management.json",
                        "-v", f"{RUN/'configs'}:/configs:ro", "-v", f"{RUN/'secrets'}:/run/secrets/apexfabric:ro",
                        "-v", f"{RUN/'state'}:/state:U", IMAGE], check=True)
        gallery = sim.issue("status", "gallery.status", {})
        request = Request(API + "/api/management/commands", data=json.dumps({
            "command_id": "unauthenticated", "type": "gallery.status", "payload": {}}).encode())
        try:
            urlopen(request, timeout=5)
            raise AssertionError("Unauthenticated command was accepted")
        except HTTPError as exc:
            assert exc.code == 401
        request = Request(API + "/api/management/commands", data=json.dumps({
            "command_id": "public-api-auth", "type": "gallery.status", "payload": {}}).encode(),
            headers={"Authorization": "Bearer " + sim.token, "Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["status"] == "applied"
        document = {"revision": 1, "embedding_space": gallery["embedding_space"], "people": [{
            "person_id": "synthetic-test", "display_name": "Synthetic Test", "group": "authorised",
            "templates": [[1.0] + [0.0] * 511]}]}
        result = sim.issue("replace", "gallery.replace", document)
        assert result["managed_revision"] == 1
        print("Gallery revision 1 applied through HTTPS management command", flush=True)
        time.sleep(45)
        with sim.lock:
            sim.offline = True
        backlog_ids = []
        for _ in range(30):
            time.sleep(2)
            backlog_ids = json.loads(subprocess.check_output([
                "podman", "exec", NAME, "python", "-c",
                "import sqlite3,json; db=sqlite3.connect('/state/surveillance/management/sync.sqlite'); "
                "print(json.dumps([r[0] for r in db.execute('select id from outbox')]))"], text=True))
            if backlog_ids:
                break
        pending = len(backlog_ids)
        (RUN / "outage-metrics.json").write_text(json.dumps(api("/metrics"), indent=2))
        assert pending > 0, "No records produced during simulated outage"
        subprocess.run(["podman", "restart", "-t", "10", NAME], check=True, capture_output=True)
        with sim.lock:
            sim.offline = False
        after = sim.issue("after-restart", "gallery.status", {})
        assert after["managed_revision"] == 1 and after["people"] == ["synthetic-test"]
        document.update(revision=2, people=[])
        assert sim.issue("delete", "gallery.replace", document)["person_count"] == 0
        time.sleep(25)
        assert set(backlog_ids) <= set(sim.records), "Some outage records were not delivered after restart"
        observations = [r for r in sim.records.values() if r["kind"] == "reid_observation"]
        events = [r for r in sim.records.values() if r["kind"] == "event"]
        assert observations and events and sim.artifacts, "Missing records or evidence at management"
        for record in sim.records.values():
            payload = record["payload"]
            assets = payload.get("payload", {}).get("snapshot_assets", {}) if record["kind"] == "event" else payload.get("artifacts", {})
            assert all(a["artifact_id"] in sim.artifacts for a in assets.values())
        sample = observations[-1]["payload"]
        mapping = {"revision": 1, "mappings": [{"camera_id": sample["camera_id"],
            "stream_session_id": sample["stream_session_id"], "local_identity_id": sample["local_identity_id"],
            "central_person_id": "central-test"}]}
        sim.issue("mapping", "identity.mappings.replace", mapping)
        # Exercise enrollment control, without accepting arbitrary recorded subjects
        # into the active enrolled gallery.
        sim.issue("enroll-start", "enrollment.start", {"session_id": "live-control-test",
                  "person_id": "temporary-test", "camera_id": "ch9"})
        time.sleep(20)
        enrollment = sim.issue("enroll-status", "enrollment.status", {"session_id": "live-control-test"})
        sim.issue("enroll-stop", "enrollment.stop", {"session_id": "live-control-test"})
        sim.issue("enroll-cancel", "enrollment.cancel", {"session_id": "live-control-test"})
        assert sim.issue("gallery-final", "gallery.status", {})["person_count"] == 0
        # The simulator searches received compatible vectors, not an edge index.
        query = sample["embeddings"][0]
        scores = [sum(a*b for a, b in zip(query["vector"], emb["vector"]))
                  for row in observations for emb in row["payload"]["embeddings"]
                  if emb["embedding_space"] == query["embedding_space"]]
        assert max(scores) > .999
        report = {"events": len(events), "observations": len(observations), "artifacts": len(sim.artifacts),
                  "outage_backlog": pending, "gallery_restart_persisted": True, "gallery_delete_applied": True,
                  "backlog_ids_delivered": len(backlog_ids),
                  "mapping_applied": True, "central_search_self_match": max(scores),
                  "enrollment_before_stop": {k: enrollment.get(k) for k in ("state", "captured", "error")},
                  "enrollment_cancelled": True,
                  "modalities": sorted({e["modality"] for r in observations for e in r["payload"]["embeddings"]})}
        (RUN / "report.json").write_text(json.dumps(report, indent=2))
        (RUN / "metrics.json").write_text(json.dumps(api("/metrics"), indent=2))
        print(json.dumps(report), flush=True)
    finally:
        logs = subprocess.run(["podman", "logs", NAME], capture_output=True, text=True)
        (RUN / "container.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["podman", "stop", "-t", "10", NAME], capture_output=True)
        subprocess.run(["podman", "rm", NAME], capture_output=True)
        server.shutdown()
        server.server_close()
        (RUN / "received/records.json").write_text(json.dumps(sim.records))
        (RUN / "received/commands.json").write_text(json.dumps(sim.results, indent=2))


if __name__ == "__main__":
    main()
