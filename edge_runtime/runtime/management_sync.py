"""Optional management transport. Configuration and credentials are mounted inputs."""
import hashlib
import json
import os
from pathlib import Path
import ssl
import threading
import time
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler, HTTPSHandler

from edge_runtime.runtime.sync_store import SyncStore


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ManagementSync:
    def __init__(self, status, config):
        self.status = status
        self.config = config
        self.base = config["url"].rstrip("/")
        parsed = urlsplit(self.base)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("management URL must not contain credentials, query or fragment")
        if parsed.scheme != "https" and not (
            config.get("allow_insecure_loopback") and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("management synchronization requires HTTPS")
        self.token = Path(config["token_file"]).read_text().strip()
        if not self.token or "\n" in self.token or "\r" in self.token:
            raise ValueError("invalid management token")
        self.opener = build_opener(NoRedirect(), HTTPSHandler(
            context=ssl.create_default_context(cafile=config.get("ca_file"))))
        self.store = SyncStore(status.state_dir / "management")
        self.store.bind_edge(str(status.edge_id))
        self.edge_id = str(status.edge_id)
        self.stop_event = threading.Event()
        self.last_error = None
        self.delivered = 0
        self.thread = threading.Thread(target=self.run, name="management-sync", daemon=True)

    def request(self, path, payload=None, method=None, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else (
            json.dumps(payload, allow_nan=False).encode() if payload is not None else None)
        req = Request(self.base + path, data=body, method=method,
                      headers={"Authorization": "Bearer " + self.token, "Content-Type": content_type})
        with self.opener.open(req, timeout=5) as response:
            return json.loads(response.read(4 * 1024 * 1024))

    def cycle(self):
        from edge_runtime.runtime.solution_pack_entrypoint import _enrich_event, _resolve_snapshot_path, _event_snapshot_assets
        prefix = "/v1/edges/" + quote(self.edge_id, safe="")
        commands = self.request(prefix + "/commands").get("commands", [])
        if not isinstance(commands, list) or len(commands) > 20:
            raise ValueError("management must return at most 20 commands per poll")
        for command in commands:
            if self.stop_event.is_set():
                return
            req = Request(self.status.runtime_api_url + "/api/management/commands",
                          data=json.dumps(command).encode(), headers={
                              "Content-Type": "application/json", "Authorization": "Bearer " + self.token})
            try:
                with urlopen(req, timeout=15) as response:
                    result = json.load(response)
            except HTTPError as exc:
                if exc.code != 400:
                    raise
                result = {"command_id": command.get("command_id"), "status": "rejected",
                          "error": "invalid command or reused command ID"}
            ack = self.request(prefix + "/command-results", result)
            if ack.get("command_id") != command["command_id"]:
                raise ValueError("command result acknowledgement mismatch")
        for record_id, kind, raw in self.store.pending():
            if self.stop_event.is_set():
                return
            payload = _enrich_event(self.status, raw) if kind == "event" else dict(raw)
            assets = (payload.get("payload", {}).get("snapshot_assets", {}) if kind == "event"
                      else payload.get("artifacts", {}))
            for asset in assets.values():
                path = _resolve_snapshot_path(self.status, asset["ref"])
                if path is None:
                    raise ValueError("snapshot disappeared before upload")
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                ack = self.request(prefix + "/artifacts/" + digest, content, "PUT", asset["content_type"])
                if ack.get("sha256") != digest:
                    raise ValueError("artifact acknowledgement mismatch")
                asset["artifact_id"] = digest
            envelope = dict(schema_version="1.0", edge_id=self.edge_id,
                            record_id=record_id, kind=kind, payload=payload)
            if kind == "event":
                envelope["source_runtime_session_id"] = raw.get("runtime_session_id")
                envelope["missing_artifact_kinds"] = sorted(set(_event_snapshot_assets(raw)) - set(assets))
            ack = self.request(prefix + "/records", envelope)
            if ack.get("record_id") != record_id:
                raise ValueError("record acknowledgement mismatch")
            self.store.ack(record_id)
            self.delivered += 1
        self.request(prefix + "/status", {"runtime": self.status.as_payload(),
                                          "pending_records": self.store.count()})

    def run(self):
        delay = 1
        while not self.stop_event.is_set():
            try:
                self.cycle()
                self.last_error = None
                delay = 1
            except Exception as exc:
                # Do not include request URLs, headers, or biometric payloads in errors.
                self.last_error = type(exc).__name__
                delay = min(30, delay * 2)
            self.status.management_sync = {"enabled": True, "pending_records": self.store.count(),
                                           "delivered": self.delivered, "last_error": self.last_error}
            self.stop_event.wait(delay)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=25)


def configure_sync(status):
    path = os.getenv("MANAGEMENT_SYNC_CONFIG")
    if not path or status.solution_pack != "surveillance":
        return None
    config = json.loads(Path(path).read_text())
    sync = ManagementSync(status, config)
    os.environ["MANAGEMENT_SYNC_ROOT"] = str(sync.store.root)
    os.environ["MANAGEMENT_API_TOKEN_FILE"] = config["token_file"]
    return sync
