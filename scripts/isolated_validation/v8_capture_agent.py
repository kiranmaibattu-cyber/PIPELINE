#!/usr/bin/env python3
"""Schema-validating API capture fixture; durable ACKs are simulated.

This validates /v1/packages bytes, hashes and associations. It is not the
production delivery agent and makes no disk-fsync/remote-upload guarantee.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import json
import os
from pathlib import Path
import socketserver
import sys
import threading
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


class Store:
    def __init__(self, schema_dir: Path, log_path: Path, status_path: Path) -> None:
        package_schema = json.loads(
            (schema_dir / "delivery-agent-package.schema.json").read_text()
        )
        package_schema["properties"]["observation"] = {"type": "object"}
        package_schema["properties"]["evidence"]["items"]["properties"]["metadata"] = {
            "type": "object"
        }
        package_schema["properties"]["embeddings"]["items"] = {"type": "object"}
        self.validator = Draft202012Validator(
            package_schema,
            format_checker=FormatChecker(),
        )
        self.observation_validator = Draft202012Validator(
            json.loads((schema_dir / "observation.schema.json").read_text()),
            format_checker=FormatChecker(),
        )
        self.evidence_validator = Draft202012Validator(
            json.loads((schema_dir / "evidence.schema.json").read_text()),
            format_checker=FormatChecker(),
        )
        self.embedding_validator = Draft202012Validator(
            json.loads((schema_dir / "embedding.schema.json").read_text()),
            format_checker=FormatChecker(),
        )
        self.log_path = log_path
        self.status_path = status_path
        self.lock = threading.Lock()
        self.packages: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        self.next_sequence = 1
        self.counts: dict[str, int] = {}
        self.cameras: dict[str, int] = {}
        self.evidence_bytes = 0
        self.embedding_count = 0

    @staticmethod
    def digest(manifest: dict[str, Any], evidence: list[tuple[str, bytes]]) -> str:
        value = hashlib.sha256(canonical(manifest))
        for name, body in evidence:
            value.update(b"\0")
            value.update(name.encode())
            value.update(b"\0")
            value.update(body)
        return value.hexdigest()

    def accept(self, manifest: dict[str, Any], parts: dict[str, bytes]):
        self.validator.validate(manifest)
        self.observation_validator.validate(manifest["observation"])
        for item in manifest["evidence"]:
            self.evidence_validator.validate(item["metadata"])
        for item in manifest["embeddings"]:
            self.embedding_validator.validate(item)
        expected_names = [item["part_name"] for item in manifest["evidence"]]
        if len(expected_names) != len(set(expected_names)) or set(parts) != set(expected_names):
            raise ValueError("multipart evidence parts do not match manifest")
        observation = manifest["observation"]
        observation_id = observation["observation_id"]
        evidence_ids = set(observation.get("evidence_ids") or [])
        ordered_parts: list[tuple[str, bytes]] = []
        for item in manifest["evidence"]:
            metadata = item["metadata"]
            body = parts[item["part_name"]]
            if metadata["observation_id"] != observation_id:
                raise ValueError("evidence parent mismatch")
            if metadata["evidence_id"] not in evidence_ids:
                raise ValueError("evidence not listed by observation")
            if metadata["size_bytes"] != len(body):
                raise ValueError("evidence size mismatch")
            if metadata["sha256"] != "sha256:" + hashlib.sha256(body).hexdigest():
                raise ValueError("evidence digest mismatch")
            ordered_parts.append((item["part_name"], body))
        embedding_ids = set(observation.get("embedding_ids") or [])
        for item in manifest["embeddings"]:
            if item["observation_id"] != observation_id:
                raise ValueError("embedding parent mismatch")
            if item["embedding_id"] not in embedding_ids:
                raise ValueError("embedding not listed by observation")
            if item["evidence_id"] not in evidence_ids:
                raise ValueError("embedding evidence mismatch")

        digest = self.digest(manifest, ordered_parts)
        package_id = manifest["package_id"]
        with self.lock:
            previous = self.packages.get(package_id)
            if previous is not None:
                if previous[0] != digest:
                    return 409, {
                        "error": {"code": "package_id_conflict", "message": "conflict", "retryable": False}
                    }
                return 200, {
                    "status": "duplicate", "package_id": package_id,
                    "deployment_id": manifest["deployment_id"], "sequences": previous[1],
                }
            objects = [("observation", observation_id)]
            objects.extend(
                ("evidence", item["metadata"]["evidence_id"])
                for item in manifest["evidence"]
            )
            objects.extend(
                ("embedding", item["embedding_id"])
                for item in manifest["embeddings"]
            )
            first = self.next_sequence
            sequences = [
                {"sequence": first + index, "object_type": kind, "object_id": identifier}
                for index, (kind, identifier) in enumerate(objects)
            ]
            self.next_sequence += len(sequences)
            self.packages[package_id] = (digest, sequences)
            event_type = observation["event_type"]
            camera_id = observation["camera_id"]
            self.counts[event_type] = self.counts.get(event_type, 0) + 1
            self.cameras[camera_id] = self.cameras.get(camera_id, 0) + 1
            self.evidence_bytes += sum(len(body) for _, body in ordered_parts)
            self.embedding_count += len(manifest["embeddings"])
            record = {
                "package_id": package_id,
                "camera_id": camera_id,
                "stream_session_id": observation.get("stream_session_id"),
                "frame_id": observation.get("frame_id"),
                "event_type": event_type,
                "presence_id": observation.get("presence_id"),
                "track_id": observation.get("track_id"),
                "plate_text": observation.get("plate_text"),
                "plate_outcome": observation.get("outcome"),
                "hazard": observation.get("hazard"),
                "hazard_episode_id": observation.get("hazard_episode_id"),
                "evidence": [item["metadata"]["evidence_type"] for item in manifest["evidence"]],
                "embeddings": [item["profile_id"] for item in manifest["embeddings"]],
                "observed_at": observation["observed_at"],
                "sequences": sequences,
            }
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            temporary = self.status_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "packages": len(self.packages), "next_sequence": self.next_sequence,
                "event_types": self.counts, "cameras": self.cameras,
                "evidence_bytes": self.evidence_bytes,
                "embeddings": self.embedding_count,
            }, separators=(",", ":")))
            os.replace(temporary, self.status_path)
            return 201, {
                "status": "durable", "package_id": package_id,
                "deployment_id": manifest["deployment_id"], "sequences": sequences,
            }


class Handler(socketserver.StreamRequestHandler):
    store: Store

    def handle(self) -> None:
        request = self.rfile.readline().decode("ascii", "replace").strip().split()
        if request[:2] != ["POST", "/v1/packages"]:
            self._send(404, {"error": {"code": "not_found", "message": "not found", "retryable": False}})
            return
        headers = {}
        while True:
            line = self.rfile.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            key, value = line.decode("latin-1").split(":", 1)
            headers[key.lower()] = value.strip()
        try:
            length = int(headers["content-length"])
            if length > 64 * 1024 * 1024:
                raise ValueError("request too large")
            body = self.rfile.read(length)
            message = BytesParser(policy=email_policy).parsebytes(
                f"Content-Type: {headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
                + body
            )
            parts = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                for part in message.iter_parts()
            }
            manifest = json.loads(parts.pop("manifest"))
            status, payload = self.store.accept(manifest, parts)
        except Exception as exc:  # noqa: BLE001
            print(f"invalid_package: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            status, payload = 400, {
                "error": {"code": "invalid_package", "message": str(exc)[:200], "retryable": False}
            }
        self._send(status, payload)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        reason = {200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found", 409: "Conflict"}.get(status, "Error")
        body = canonical(payload)
        self.wfile.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--schemas", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.socket.unlink(missing_ok=True)
    Handler.store = Store(args.schemas, args.log, args.status)
    with socketserver.ThreadingUnixStreamServer(str(args.socket), Handler) as server:
        os.chmod(args.socket, 0o666)
        server.serve_forever()


if __name__ == "__main__":
    main()
