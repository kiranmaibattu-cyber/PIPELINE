#!/usr/bin/env python3
"""Deterministic V8 detector and complete-package parity harness.

The harness runs two immutable images on the same recorded pixels. It first
compares raw vehicle/person detections before tracking, then optionally runs the
complete V8 worker and captures packages through a local delivery-agent socket.
Random identifiers and absolute wall-clock timestamps are normalized only for
comparison; the original packages and evidence bytes remain in the report.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import math
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.request import urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "v8_detector_probe.py"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def command(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout)


def image_ref(value: str) -> str:
    command(["podman", "image", "inspect", value])
    return value


def safe_name(path: Path) -> str:
    return "".join(character if character.isalnum() else "-" for character in path.stem).strip("-")


def detector_probe(image: str, video: Path, output: Path, samples: int, every: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command([
        "podman", "run", "--rm", "--network=none",
        "--device", "/dev/dri", "--group-add", "keep-groups",
        "-e", "PYTHONPATH=/opt/traffic-pilot/services/worker:/opt/traffic-pilot",
        "-v", f"{video.resolve()}:/recordings/input.mp4:ro",
        "-v", f"{PROBE.resolve()}:/parity/v8_detector_probe.py:ro",
        "-v", f"{output.parent.resolve()}:/output",
        "--entrypoint", "python", image,
        "/parity/v8_detector_probe.py",
        "--video", "/recordings/input.mp4",
        "--output", f"/output/{output.name}",
        "--maximum-samples", str(samples),
        "--sample-every", str(every),
    ], timeout=300)


def iou(left: list[float], right: list[float]) -> float:
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def compare_detections(baseline_path: Path, candidate_path: Path) -> dict[str, Any]:
    baseline = json.loads(baseline_path.read_text())
    candidate = json.loads(candidate_path.read_text())
    candidate_frames = {item["frame_index"]: item for item in candidate["frames"]}
    matched = []
    unmatched_baseline = 0
    unmatched_candidate = 0
    class_mismatches = 0
    missing_frames = []
    for left_frame in baseline["frames"]:
        right_frame = candidate_frames.get(left_frame["frame_index"])
        if right_frame is None:
            missing_frames.append(left_frame["frame_index"])
            unmatched_baseline += len(left_frame["detections"])
            continue
        remaining = list(range(len(right_frame["detections"])))
        for left in left_frame["detections"]:
            same_class = [index for index in remaining
                          if right_frame["detections"][index]["class_name"] == left["class_name"]]
            pool = same_class or remaining
            if not pool:
                unmatched_baseline += 1
                continue
            best = max(pool, key=lambda index: iou(
                left["bbox"], right_frame["detections"][index]["bbox"]
            ))
            right = right_frame["detections"][best]
            remaining.remove(best)
            overlap = iou(left["bbox"], right["bbox"])
            if left["class_name"] != right["class_name"]:
                class_mismatches += 1
            matched.append({
                "frame_index": left_frame["frame_index"],
                "class": left["class_name"],
                "iou": overlap,
                "confidence_delta": abs(left["confidence"] - right["confidence"]),
            })
        unmatched_candidate += len(remaining)
    ious = [item["iou"] for item in matched]
    deltas = [item["confidence_delta"] for item in matched]
    result = {
        "baseline_samples": baseline["sample_count"],
        "candidate_samples": candidate["sample_count"],
        "matched_detections": len(matched),
        "unmatched_baseline": unmatched_baseline,
        "unmatched_candidate": unmatched_candidate,
        "class_mismatches": class_mismatches,
        "missing_frames": missing_frames,
        "bbox_iou_min": min(ious) if ious else None,
        "bbox_iou_mean": sum(ious) / len(ious) if ious else None,
        "confidence_delta_max": max(deltas) if deltas else None,
        "confidence_delta_mean": sum(deltas) / len(deltas) if deltas else None,
    }
    result["strict_parity"] = bool(
        not missing_frames and unmatched_baseline == 0 and unmatched_candidate == 0
        and class_mismatches == 0
        and (not ious or min(ious) >= 0.999999)
        and (not deltas or max(deltas) <= 1e-6)
    )
    return result


class CaptureState:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.index = 0
        self.sequence = 1

    def save(self, manifest: dict[str, Any], parts: dict[str, bytes]) -> dict[str, Any]:
        with self.lock:
            self.index += 1
            index = self.index
            package_dir = self.root / f"{index:06d}"
            package_dir.mkdir()
            evidence_index = []
            for part_name, body in sorted(parts.items()):
                filename = f"{part_name}.jpg"
                (package_dir / filename).write_bytes(body)
                evidence_index.append({
                    "part_name": part_name,
                    "filename": filename,
                    "size_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                })
            record = {"manifest": manifest, "evidence_parts": evidence_index}
            (package_dir / "package.json").write_bytes(canonical(record))
            rows = [("observation", manifest["observation"]["observation_id"])]
            rows.extend(("evidence", item["metadata"]["evidence_id"])
                        for item in manifest["evidence"])
            rows.extend(("embedding", item["embedding_id"])
                        for item in manifest["embeddings"])
            first = self.sequence
            self.sequence += len(rows)
            return {
                "status": "durable",
                "package_id": manifest["package_id"],
                "deployment_id": manifest["deployment_id"],
                "sequences": [
                    {"sequence": first + offset, "object_type": kind, "object_id": identifier}
                    for offset, (kind, identifier) in enumerate(rows)
                ],
            }


class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def tcp_replay_handler(video: Path, replay_fps: int):
    """Serve one independent MPEG-TS replay to every raw TCP consumer."""

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            ffmpeg_args = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                "-stream_loop", "-1", "-i", str(video), "-map", "0:v:0",
                "-an",
            ]
            if replay_fps > 0:
                ffmpeg_args += [
                    "-vf", f"fps={replay_fps}", "-c:v", "libx264",
                    "-preset", "ultrafast", "-tune", "zerolatency",
                    "-g", str(max(2, replay_fps * 2)), "-pix_fmt", "yuv420p",
                ]
            else:
                ffmpeg_args += ["-c:v", "copy", "-bsf:v", "h264_mp4toannexb"]
            ffmpeg_args += ["-f", "mpegts", "pipe:1"]
            ffmpeg = subprocess.Popen(
                ffmpeg_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            try:
                while True:
                    chunk = os.read(ffmpeg.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        break
                    self.request.sendall(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                ffmpeg.terminate()
                try:
                    ffmpeg.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    ffmpeg.kill()
                    ffmpeg.wait(timeout=3)

    return Handler


def replay_handler(video: Path, replay_fps: int):
    """Create a multi-client, camera-rate MPEG-TS replay endpoint.

    Source probing and decoding use separate HTTP connections. Each request
    therefore receives its own real-time FFmpeg replay from frame zero.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path != "/stream.ts":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            ffmpeg_args = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                "-stream_loop", "-1", "-i", str(video), "-map", "0:v:0",
                "-an",
            ]
            if replay_fps > 0:
                ffmpeg_args += [
                    "-vf", f"fps={replay_fps}", "-c:v", "libx264",
                    "-preset", "ultrafast", "-tune", "zerolatency",
                    "-g", str(max(2, replay_fps * 2)), "-pix_fmt", "yuv420p",
                ]
            else:
                ffmpeg_args += ["-c:v", "copy", "-bsf:v", "h264_mp4toannexb"]
            ffmpeg_args += ["-f", "mpegts", "pipe:1"]
            ffmpeg = subprocess.Popen(
                ffmpeg_args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            try:
                while True:
                    chunk = os.read(ffmpeg.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                ffmpeg.terminate()
                try:
                    ffmpeg.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    ffmpeg.kill()
                    ffmpeg.wait(timeout=3)

    return Handler


def capture_handler(state: CaptureState):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            request_line = self.rfile.readline().decode("ascii").strip()
            if request_line != "POST /v1/packages HTTP/1.1":
                return
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
            parts = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                for part in message.iter_parts()
            }
            manifest = json.loads(parts.pop("manifest"))
            expected = {item["part_name"] for item in manifest["evidence"]}
            if set(parts) != expected:
                raise ValueError("package evidence parts do not match manifest")
            response = canonical(state.save(manifest, parts))
            self.wfile.write(
                b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(response)}\r\nConnection: close\r\n\r\n".encode()
                + response
            )
    return Handler


def desired_state(camera_id: str, fps: int) -> dict[str, Any]:
    prefix = camera_id
    return {
        "contract": "sentry-v8",
        "schema_version": "8.0",
        "edge_id": "parity-replay",
        "deployment_id": "parity-replay",
        "revision": 1,
        "cameras": [{
            "camera_id": camera_id,
            "source": f"file:/run/secrets/sentinel/cameras/{camera_id}.url",
            "fps": fps,
            "geometry": {
                "zones": [{
                    "id": "full-zone", "name": "Full frame",
                    "poly": [[0.01, 0.01], [0.99, 0.01], [0.99, 0.99], [0.01, 0.99]],
                }],
                "counting_lines": [{
                    "id": "mid-line", "name": "Middle",
                    "a": [0.05, 0.5], "b": [0.95, 0.5],
                    "direction_mapping": {"left_to_right": "out", "right_to_left": "in"},
                }],
            },
            "bindings": [
                {"use_case_id": f"{prefix}-vehicle-count", "app": "vehicle_counting", "scope": "geometry", "geometry_ids": ["full-zone"], "object_types": ["vehicle"]},
                {"use_case_id": f"{prefix}-person-count", "app": "people_counting", "scope": "geometry", "geometry_ids": ["full-zone"], "object_types": ["person"]},
                {"use_case_id": f"{prefix}-vehicle-presence", "app": "vehicle_presence", "scope": "geometry", "geometry_ids": ["full-zone"], "object_types": ["vehicle"], "embedding_profile_ids": ["siglip2-base-v1"]},
                {"use_case_id": f"{prefix}-person-presence", "app": "person_presence", "scope": "geometry", "geometry_ids": ["full-zone"], "object_types": ["person"]},
                {"use_case_id": f"{prefix}-anpr", "app": "anpr", "scope": "geometry", "geometry_ids": ["full-zone"], "object_types": ["vehicle"]},
                {"use_case_id": f"{prefix}-crossing", "app": "vehicle_entry_exit_counts", "scope": "geometry", "geometry_ids": ["mid-line"], "object_types": ["vehicle"]},
                {"use_case_id": f"{prefix}-identity", "app": "person_identity", "scope": "full_frame", "geometry_ids": [], "object_types": ["person"], "embedding_profile_ids": ["adaface-ir101-v18.1", "transreid-ssl-v18.1", "gaitbase-v18.1"]},
                {"use_case_id": f"{prefix}-scene", "app": "scene_search", "scope": "full_frame", "geometry_ids": [], "object_types": [], "embedding_profile_ids": ["siglip2-base-v1"], "sample_interval_seconds": 2, "scene_trigger_use_case_ids": [f"{prefix}-vehicle-presence", f"{prefix}-person-presence"]},
                {"use_case_id": f"{prefix}-fire", "app": "fire_smoke", "scope": "geometry", "geometry_ids": ["full-zone"], "object_types": []},
            ],
        }],
    }


def health(port: int) -> dict[str, Any] | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return json.loads(response.read())
    except Exception:
        return None


def run_runtime(
    image: str,
    video: Path,
    output: Path,
    *,
    port: int,
    target_inference_frames: int,
    fps: int,
    replay_fps: int,
    motion_gate: bool,
    surface_motion_gate: bool = False,
    source_mode: str = "http",
    timeout_seconds: float | None = None,
    source_uri: str | None = None,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    camera_id = "parity-camera"
    with tempfile.TemporaryDirectory(prefix="sentinel-v8-parity-") as temp_value:
        temp = Path(temp_value)
        config_dir = temp / "config"
        secret_dir = temp / "secrets"
        socket_dir = temp / "delivery"
        config_dir.mkdir(); secret_dir.mkdir(); socket_dir.mkdir()
        (config_dir / "desired_state.json").write_bytes(canonical(desired_state(camera_id, fps)))
        source_port = port + 1000
        if source_uri is not None:
            (secret_dir / f"{camera_id}.url").write_text(
                str(source_uri).strip() + "\n"
            )
        elif source_mode == "tcp":
            (secret_dir / f"{camera_id}.url").write_text(
                f"tcp://127.0.0.1:{source_port}\n"
            )
        else:
            (secret_dir / f"{camera_id}.url").write_text(
                f"http://127.0.0.1:{source_port}/stream.ts\n"
            )
        socket_path = socket_dir / "agent.sock"
        capture = CaptureState(output / "packages")
        server = ThreadedUnixServer(str(socket_path), capture_handler(capture))
        os.chmod(socket_path, 0o777)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        container_name = f"sentinel-v8-parity-{uuid.uuid4().hex[:10]}"
        source_server = source_thread = None
        if source_uri is not None:
            pass
        elif source_mode == "http":
            source_server = ThreadingHTTPServer(
                ("127.0.0.1", source_port), replay_handler(video, replay_fps)
            )
            source_thread = threading.Thread(
                target=source_server.serve_forever, daemon=True
            )
            source_thread.start()
        elif source_mode == "tcp":
            source_server = ThreadedTCPServer(
                ("127.0.0.1", source_port), tcp_replay_handler(video, replay_fps)
            )
            source_thread = threading.Thread(
                target=source_server.serve_forever, daemon=True
            )
            source_thread.start()
        accelerator_args = ["--device", "/dev/dri"]
        if Path("/dev/accel/accel0").exists():
            accelerator_args.extend(["--device", "/dev/accel/accel0"])
        run_args = [
            "podman", "run", "-d", "--name", container_name,
            "--network=host", *accelerator_args, "--group-add", "keep-groups",
            "-e", f"APEX_API_PORT={port}",
            "-e", "PYTHONFAULTHANDLER=1",
            "-e", f"SENTINEL_MOTION_GATE_ENABLED={'true' if motion_gate else 'false'}",
            "-e", (
                "SENTINEL_SURFACE_MOTION_GATE_ENABLED="
                f"{'true' if surface_motion_gate else 'false'}"
            ),
            "-v", f"{config_dir / 'desired_state.json'}:/configs/desired_state.json:ro",
            "-v", f"{secret_dir}:/run/secrets/sentinel/cameras:ro",
            "-v", f"{socket_dir}:/run/sentinel-delivery",
        ]
        run_args.append(image)
        started = time.monotonic()
        final_health = None
        logs = ""
        target_reached = False
        target_metric = (
            "decoded_frames" if motion_gate or surface_motion_gate
            else "inference_frames"
        )
        try:
            command(run_args, timeout=30)
            deadline = time.monotonic() + (
                float(timeout_seconds) if timeout_seconds is not None
                else max(180, target_inference_frames / max(1, fps) * 4)
            )
            while time.monotonic() < deadline:
                current = health(port)
                if current and current.get("worker_running") and current.get("revision") == 1:
                    cameras = (current.get("telemetry") or {}).get("cameras") or []
                    if cameras:
                        totals = ((cameras[0].get("processing") or {}).get("totals") or {})
                        if int(totals.get(target_metric, 0)) >= target_inference_frames:
                            final_health = current
                            target_reached = True
                            break
                time.sleep(0.05)
            if final_health is None:
                final_health = health(port)
            if target_reached:
                time.sleep(2.0)
                final_health = health(port) or final_health
        finally:
            try:
                log_result = command(["podman", "logs", container_name], timeout=30)
                logs = log_result.stdout + log_result.stderr
            except Exception:
                pass
            subprocess.run(["podman", "stop", "--time", "10", container_name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["podman", "rm", container_name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if source_server is not None:
                source_server.shutdown(); source_server.server_close()
            if source_thread is not None:
                source_thread.join(timeout=3)
            server.shutdown(); server.server_close(); server_thread.join(timeout=3)
        (output / "health.json").write_bytes(canonical(final_health))
        (output / "runtime.log").write_text(logs, encoding="utf-8")
        return {
            "packages": capture.index,
            "elapsed_seconds": time.monotonic() - started,
            "health": final_health,
            "target_reached": target_reached,
            "target_metric": target_metric,
        }


def frame_number(value: Any) -> int | None:
    try:
        return int(str(value).rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return None


def load_packages(root: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted((root / "packages").glob("*/package.json")):
        record = json.loads(path.read_text())
        record["package_dir"] = str(path.parent)
        records.append(record)
    return records


def event_group(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("event_type"), event.get("use_case_id"), event.get("object_type"),
        event.get("kind"), event.get("zone_id"), event.get("line_id"),
        event.get("trigger"), event.get("state"), event.get("outcome"),
    )


def event_bbox(event: dict[str, Any]) -> dict[str, float] | None:
    return event.get("bbox_normalized") or event.get("plate_bbox_normalized")


def bbox_dict_iou(left: dict[str, float], right: dict[str, float]) -> float:
    return iou(
        [left["x1"], left["y1"], left["x2"], left["y2"]],
        [right["x1"], right["y1"], right["x2"], right["y2"]],
    )


def cosine(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    dot = sum(a * b for a, b in zip(left, right))
    ln = math.sqrt(sum(value * value for value in left))
    rn = math.sqrt(sum(value * value for value in right))
    return dot / (ln * rn) if ln and rn else None


def compare_runtime(baseline_root: Path, candidate_root: Path, frame_tolerance: int) -> dict[str, Any]:
    baseline = load_packages(baseline_root)
    candidate = load_packages(candidate_root)
    baseline = [item for item in baseline if item["manifest"]["observation"]["event_type"] != "camera_health"]
    candidate = [item for item in candidate if item["manifest"]["observation"]["event_type"] != "camera_health"]
    left_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    right_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in baseline:
        left_groups[event_group(item["manifest"]["observation"])].append(item)
    for item in candidate:
        right_groups[event_group(item["manifest"]["observation"])].append(item)
    matched = []
    missing = []
    extra = []
    bbox_ious = []
    confidence_deltas = []
    embedding_cosines = []
    embedding_mismatches = []
    evidence_exact = 0
    evidence_compared = 0
    evidence_mismatches = []
    field_mismatches = []
    compare_fields = (
        "count", "count_semantics", "coverage_state", "direction", "direction_raw",
        "plate_text", "partial", "outcome", "reason", "scope", "zone_id",
        "line_id", "object_type", "kind", "sample_reason", "vehicle_type",
    )
    for group in sorted(set(left_groups) | set(right_groups), key=str):
        left_items = sorted(left_groups.get(group, []),
                            key=lambda item: frame_number(item["manifest"]["observation"].get("source_frame_id")) or -1)
        right_items = sorted(right_groups.get(group, []),
                             key=lambda item: frame_number(item["manifest"]["observation"].get("source_frame_id")) or -1)
        unused = set(range(len(right_items)))
        for left in left_items:
            left_event = left["manifest"]["observation"]
            left_frame = frame_number(left_event.get("source_frame_id"))
            choices = list(unused)
            left_match_box = event_bbox(left_event)
            if left_frame is not None:
                def match_score(index: int) -> tuple[float, float, float, float]:
                    candidate_event = right_items[index]["manifest"]["observation"]
                    candidate_frame = frame_number(candidate_event.get("source_frame_id"))
                    frame_delta = abs((candidate_frame if candidate_frame is not None else -10**9)
                                      - left_frame)
                    left_track = left_event.get("track_id")
                    candidate_track = candidate_event.get("track_id")
                    track_penalty = float(
                        left_track is not None and candidate_track is not None
                        and str(left_track) != str(candidate_track)
                    )
                    left_hashes = {
                        part.get("sha256") for part in (left.get("evidence_parts") or [])
                    }
                    candidate_hashes = {
                        part.get("sha256")
                        for part in (right_items[index].get("evidence_parts") or [])
                    }
                    evidence_penalty = float(bool(left_hashes or candidate_hashes)
                                             and not bool(left_hashes & candidate_hashes))
                    candidate_box = event_bbox(candidate_event)
                    overlap_penalty = (
                        1.0 - bbox_dict_iou(left_match_box, candidate_box)
                        if left_match_box and candidate_box else 1.0
                    )
                    return frame_delta, track_penalty, evidence_penalty, overlap_penalty
                choices.sort(key=match_score)
            if not choices:
                missing.append({"group": group, "frame": left_frame})
                continue
            selected = choices[0]
            right = right_items[selected]
            right_event = right["manifest"]["observation"]
            right_frame = frame_number(right_event.get("source_frame_id"))
            delta = (abs(right_frame - left_frame)
                     if left_frame is not None and right_frame is not None else None)
            if delta is not None and delta > frame_tolerance:
                missing.append({"group": group, "frame": left_frame, "nearest": right_frame})
                continue
            unused.remove(selected)
            differences = {field: [left_event.get(field), right_event.get(field)]
                           for field in compare_fields
                           if left_event.get(field) != right_event.get(field)}
            if differences:
                field_mismatches.append({"group": group, "frame": left_frame,
                                         "differences": differences})
            left_box, right_box = event_bbox(left_event), event_bbox(right_event)
            if left_box and right_box:
                bbox_ious.append(bbox_dict_iou(left_box, right_box))
            if left_event.get("confidence") is not None and right_event.get("confidence") is not None:
                confidence_deltas.append(abs(
                    float(left_event["confidence"]) - float(right_event["confidence"])
                ))
            left_embeddings = left["manifest"].get("embeddings") or []
            right_embeddings = right["manifest"].get("embeddings") or []
            for left_embedding in left_embeddings:
                compatible = [item for item in right_embeddings
                              if item.get("profile_id") == left_embedding.get("profile_id")
                              and item.get("kind") == left_embedding.get("kind")]
                if compatible:
                    similarity = cosine(left_embedding["vector"], compatible[0]["vector"])
                    if similarity is not None:
                        embedding_cosines.append(similarity)
                        if similarity < 0.9999:
                            embedding_mismatches.append({
                                "event_type": left_event.get("event_type"),
                                "profile_id": left_embedding.get("profile_id"),
                                "baseline_frame": left_frame,
                                "candidate_frame": right_frame,
                                "cosine": similarity,
                            })
            left_evidence = left.get("evidence_parts") or []
            right_evidence = right.get("evidence_parts") or []
            for left_part, right_part in zip(left_evidence, right_evidence):
                evidence_compared += 1
                exact = left_part["sha256"] == right_part["sha256"]
                evidence_exact += int(exact)
                if not exact:
                    evidence_mismatches.append({
                        "event_type": left_event.get("event_type"),
                        "baseline_frame": left_frame,
                        "candidate_frame": right_frame,
                        "baseline_sha256": left_part["sha256"],
                        "candidate_sha256": right_part["sha256"],
                    })
            if len(left_evidence) != len(right_evidence):
                evidence_mismatches.append({
                    "event_type": left_event.get("event_type"),
                    "baseline_frame": left_frame,
                    "candidate_frame": right_frame,
                    "baseline_parts": len(left_evidence),
                    "candidate_parts": len(right_evidence),
                })
            matched.append({"group": group, "baseline_frame": left_frame,
                            "candidate_frame": right_frame, "frame_delta": delta})
        for index in sorted(unused):
            event = right_items[index]["manifest"]["observation"]
            extra.append({"group": group, "frame": frame_number(event.get("source_frame_id"))})
    type_counts = lambda items: {
        kind: sum(1 for item in items
                  if item["manifest"]["observation"]["event_type"] == kind)
        for kind in sorted({item["manifest"]["observation"]["event_type"] for item in items})
    }
    result = {
        "baseline_event_counts": type_counts(baseline),
        "candidate_event_counts": type_counts(candidate),
        "matched_events": len(matched),
        "missing_events": missing,
        "extra_events": extra,
        "field_mismatches": field_mismatches,
        "maximum_frame_delta": max((item["frame_delta"] or 0 for item in matched), default=0),
        "bbox_iou_min": min(bbox_ious) if bbox_ious else None,
        "bbox_iou_mean": sum(bbox_ious) / len(bbox_ious) if bbox_ious else None,
        "confidence_delta_max": max(confidence_deltas) if confidence_deltas else None,
        "embedding_cosine_min": min(embedding_cosines) if embedding_cosines else None,
        "embedding_cosine_mean": (sum(embedding_cosines) / len(embedding_cosines)
                                   if embedding_cosines else None),
        "embedding_mismatches": embedding_mismatches,
        "evidence_exact": evidence_exact,
        "evidence_compared": evidence_compared,
        "evidence_mismatches": evidence_mismatches,
    }
    result["strict_parity"] = bool(
        not missing and not extra and not field_mismatches
        and (not bbox_ious or min(bbox_ious) >= 0.999)
        and (not confidence_deltas or max(confidence_deltas) <= 1e-5)
        and (not embedding_cosines or min(embedding_cosines) >= 0.9999)
        and evidence_exact == evidence_compared
    )
    return result


def compact_runtime_summary(run: dict[str, Any]) -> dict[str, Any]:
    payload = run.get("health") or {}
    telemetry = payload.get("telemetry") or {}
    cameras = telemetry.get("cameras") or []
    camera = cameras[0] if cameras else {}
    return {
        "packages": run["packages"],
        "elapsed_seconds": round(run["elapsed_seconds"], 3),
        "target_reached": run.get("target_reached"),
        "target_metric": run.get("target_metric"),
        "ready": payload.get("ready"),
        "camera_health": (camera.get("camera_health") or {}).get("state"),
        "processing": camera.get("processing"),
        "outputs": camera.get("outputs"),
        "handoff": (telemetry.get("handoff_scheduler") or {}).get("counters"),
        "memory_mb": ((telemetry.get("system") or {}).get("container_cgroup") or {}).get("memory_current_mb"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-image", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--video", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--detector-samples", type=int, default=40)
    parser.add_argument("--detector-sample-every", type=int, default=5)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--runtime-frames", type=int, default=150)
    parser.add_argument("--fps", type=int, default=5)
    parser.add_argument(
        "--replay-fps", type=int, default=0,
        help="transcode replay to this FPS; zero preserves the recorded H.264 stream",
    )
    parser.add_argument("--motion-gate", action="store_true")
    parser.add_argument(
        "--candidate-surface", action="store_true",
        help="run the candidate through its VA-surface/zero-copy path",
    )
    parser.add_argument(
        "--baseline-surface", action="store_true",
        help="run the baseline through its VA-surface path as well",
    )
    parser.add_argument(
        "--runtime-source-mode", choices=("http", "tcp"), default="http",
        help="TCP mode qualifies the candidate VA-surface path",
    )
    parser.add_argument("--frame-tolerance", type=int, default=2)
    args = parser.parse_args()

    baseline_image = image_ref(args.baseline_image)
    candidate_image = image_ref(args.candidate_image)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "baseline_image": baseline_image,
        "candidate_image": candidate_image,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "videos": {},
    }
    for index, video_value in enumerate(args.video):
        video = Path(video_value).resolve()
        if not video.is_file():
            raise FileNotFoundError(video)
        name = safe_name(video)
        video_root = output / name
        baseline_probe = video_root / "baseline-detections.json"
        candidate_probe = video_root / "candidate-detections.json"
        detector_probe(baseline_image, video, baseline_probe,
                       args.detector_samples, args.detector_sample_every)
        detector_probe(candidate_image, video, candidate_probe,
                       args.detector_samples, args.detector_sample_every)
        video_report = {
            "path": str(video),
            "detector": compare_detections(baseline_probe, candidate_probe),
        }
        if args.runtime:
            baseline_runtime = run_runtime(
                baseline_image, video, video_root / "baseline-runtime",
                port=18200 + index * 2,
                target_inference_frames=args.runtime_frames,
                fps=args.fps,
                replay_fps=args.replay_fps,
                motion_gate=args.motion_gate,
                surface_motion_gate=args.baseline_surface,
                source_mode=args.runtime_source_mode,
            )
            candidate_runtime = run_runtime(
                candidate_image, video, video_root / "candidate-runtime",
                port=18201 + index * 2,
                target_inference_frames=args.runtime_frames,
                fps=args.fps,
                replay_fps=args.replay_fps,
                motion_gate=args.motion_gate,
                surface_motion_gate=args.candidate_surface,
                source_mode=args.runtime_source_mode,
            )
            video_report["baseline_runtime"] = compact_runtime_summary(baseline_runtime)
            video_report["candidate_runtime"] = compact_runtime_summary(candidate_runtime)
            video_report["runtime"] = compare_runtime(
                video_root / "baseline-runtime",
                video_root / "candidate-runtime",
                args.frame_tolerance,
            )
        report["videos"][name] = video_report
        (output / "report.json").write_bytes(canonical(report))
    report["detector_strict_parity"] = all(
        item["detector"]["strict_parity"] for item in report["videos"].values()
    )
    runtime_results = [item.get("runtime") for item in report["videos"].values()
                       if item.get("runtime") is not None]
    report["runtime_strict_parity"] = (
        all(item["strict_parity"] for item in runtime_results)
        if runtime_results else None
    )
    (output / "report.json").write_bytes(canonical(report))
    print(json.dumps(report, indent=2))
    return 0 if report["detector_strict_parity"] and report["runtime_strict_parity"] is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
