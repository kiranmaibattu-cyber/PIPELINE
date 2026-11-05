#!/usr/bin/env python3
"""Run a bounded multi-camera capacity qualification against a V8 image."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "newdetails" / "sentry-v8"
AGENT = ROOT / "run" / "v8-live" / "mock_delivery_agent.py"
SOURCE_URLS = [f"tcp://192.168.1.92:{port}" for port in range(9001, 9011)]


def canonical(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def steady_throughput(samples: list[dict], minimum_ratio: float) -> dict:
    """Use cumulative counts over measured time, not just final rolling FPS."""
    steady = [s for s in samples if s.get("sample_phase") == "steady"]
    result = {"passed": False, "per_camera": {}, "duration_seconds": 0.0}
    if len(steady) < 2:
        return result
    first, last = steady[0], steady[-1]
    duration = last["sample_monotonic_seconds"] - first["sample_monotonic_seconds"]
    cameras = first.get("per_camera", {})
    if duration <= 0 or not cameras or set(cameras) != set(last.get("per_camera", {})):
        return result
    target = float(first["target_decode_fps"]) / len(cameras)
    if target <= 0:
        return result
    for camera, initial in cameras.items():
        begin = initial.get("totals", {}).get("decoded_frames")
        end = last["per_camera"][camera].get("totals", {}).get("decoded_frames")
        if begin is None or end is None or end < begin:
            return result
        fps = (end - begin) / duration
        result["per_camera"][camera] = {"selected_fps": fps, "ratio": fps / target}
    result["duration_seconds"] = duration
    result["passed"] = all(c["ratio"] >= minimum_ratio for c in result["per_camera"].values())
    return result


def sanitized_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def desired_state(
    camera_count: int,
    fps: int,
    *,
    omit_fps: bool = False,
    full_usecases: bool = False,
) -> dict:
    cameras = []
    for index in range(camera_count):
        number = index + 1
        camera_id = f"capacity-{number:02d}"
        zone_id = f"zone-{number}"
        camera = {
            "camera_id": camera_id,
            "source": f"file:/run/secrets/sentinel/cameras/{camera_id}.url",
            "geometry": {
                "zones": [{
                    "id": zone_id,
                    "name": "Full",
                    "poly": [[0.01, 0.01], [0.99, 0.01],
                             [0.99, 0.99], [0.01, 0.99]],
                }],
                "counting_lines": ([{
                    "id": f"line-{number}",
                    "name": "Middle",
                    "a": [0.05, 0.5],
                    "b": [0.95, 0.5],
                    "direction_mapping": {
                        "left_to_right": "out",
                        "right_to_left": "in",
                    },
                }] if full_usecases else []),
            },
            "bindings": [
                {
                    "use_case_id": f"count-{number}",
                    "app": "vehicle_counting",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": ["vehicle"],
                },
                {
                    "use_case_id": f"anpr-{number}",
                    "app": "anpr",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": ["vehicle"],
                },
            ],
        }
        if full_usecases:
            camera["bindings"].extend([
                {
                    "use_case_id": f"people-count-{number}",
                    "app": "people_counting",
                    "scope": "full_frame",
                    "geometry_ids": [],
                    "object_types": ["person"],
                },
                {
                    "use_case_id": f"vehicle-presence-{number}",
                    "app": "vehicle_presence",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": ["vehicle"],
                },
                {
                    "use_case_id": f"person-presence-{number}",
                    "app": "person_presence",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": ["person"],
                },
                {
                    "use_case_id": f"crossing-{number}",
                    "app": "vehicle_entry_exit_counts",
                    "scope": "geometry",
                    "geometry_ids": [f"line-{number}"],
                    "object_types": ["vehicle"],
                },
                {
                    "use_case_id": f"identity-{number}",
                    "app": "person_identity",
                    "scope": "full_frame",
                    "geometry_ids": [],
                    "object_types": ["person"],
                    "embedding_profile_ids": [
                        "adaface-ir101-v18.1",
                        "transreid-ssl-v18.1",
                        "gaitbase-v18.1",
                    ],
                },
                {
                    "use_case_id": f"scene-{number}",
                    "app": "scene_search",
                    "scope": "full_frame",
                    "geometry_ids": [],
                    "object_types": [],
                    "embedding_profile_ids": ["siglip2-base-v1"],
                    "sample_interval_seconds": 2,
                    "scene_trigger_use_case_ids": [
                        f"vehicle-presence-{number}",
                        f"person-presence-{number}",
                    ],
                },
                {
                    "use_case_id": f"hazard-{number}",
                    "app": "fire_smoke",
                    "scope": "geometry",
                    "geometry_ids": [zone_id],
                    "object_types": [],
                },
            ])
        if not omit_fps:
            camera["fps"] = fps
        cameras.append(camera)
    return {
        "contract": "sentry-v8",
        "schema_version": "8.0",
        "edge_id": f"v8-capacity-{camera_count}",
        "deployment_id": f"v8-capacity-{camera_count}",
        "revision": 1,
        "cameras": cameras,
    }


def health(port: int) -> dict | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            return json.loads(response.read())
    except Exception:
        return None


def camera_ready(camera: dict) -> bool:
    processing = camera.get("processing") or {}
    totals = processing.get("totals") or {}
    decoder = camera.get("decoder") or {}
    return bool(
        (camera.get("camera_health") or {}).get("state") == "healthy"
        and int(totals.get("decoded_frames") or 0) >= 30
        and float(decoder.get("last_frame_age_seconds") or 10**6) < 30.0
    )


def summarize(payload: dict, camera_count: int, fps: int) -> dict:
    telemetry = payload.get("telemetry") or {}
    cameras = telemetry.get("cameras") or []
    decoded = sum(float((item.get("processing") or {}).get("decoded_fps") or 0)
                  for item in cameras)
    inference = sum(float((item.get("processing") or {}).get("inference_fps") or 0)
                    for item in cameras)
    target = float(camera_count * fps)
    states: dict[str, int] = {}
    for item in cameras:
        state = str((item.get("camera_health") or {}).get("state") or "missing")
        states[state] = states.get(state, 0) + 1
    scheduler = telemetry.get("handoff_scheduler") or {}
    counters = scheduler.get("counters") or {}
    cgroup = ((telemetry.get("system") or {}).get("container_cgroup") or {})
    processes = telemetry.get("runtime_processes") or {}
    accelerators = telemetry.get("accelerators") or {}
    return {
        "ready": payload.get("ready"),
        "worker_running": payload.get("worker_running"),
        "camera_records": len(cameras),
        "camera_states": states,
        "cameras_with_recent_frames": sum(camera_ready(item) for item in cameras),
        "target_decode_fps": target,
        "aggregate_decode_fps": round(decoded, 3),
        "decode_target_ratio": round(decoded / target, 4) if target else 0.0,
        "aggregate_inference_fps": round(inference, 3),
        "per_camera": {
            str(item.get("camera_id")): {
                "selected_fps": (item.get("processing") or {}).get("decoded_fps"),
                "inference_fps": (item.get("processing") or {}).get("inference_fps"),
                "totals": (item.get("processing") or {}).get("totals", {}),
                "pending_admissions": ((item.get("processing") or {}).get("gauges") or {}).get("dls_pending_admissions"),
                "last_frame_age_seconds": (item.get("decoder") or {}).get("last_frame_age_seconds"),
            } for item in cameras
        },
        "unique_camera_processes": processes.get("unique_camera_processes"),
        "camera_process_rss_mb": processes.get("camera_process_rss_mb_sum"),
        "camera_process_pss_mb": processes.get("camera_process_pss_mb_sum"),
        "container_memory_mb": cgroup.get("memory_current_mb"),
        "container_cpu_cores": cgroup.get("cpu_cores_used"),
        "container_pids": cgroup.get("pids_current"),
        "gpu_utilization_pct": (accelerators.get("GPU") or {}).get(
            "hardware_utilization_pct"
        ),
        "npu_utilization_pct": (accelerators.get("NPU") or {}).get(
            "hardware_utilization_pct"
        ),
        "handoff": counters,
    }


def camera_runtime_snapshot(payload: dict) -> dict[str, dict]:
    """Return the isolation-relevant health fields keyed by camera ID."""
    result = {}
    for camera in ((payload.get("telemetry") or {}).get("cameras") or []):
        camera_id = str(camera.get("camera_id") or "")
        health_record = camera.get("camera_health") or {}
        decoder = camera.get("decoder") or {}
        result[camera_id] = {
            "state": health_record.get("state"),
            "retrying": bool(health_record.get("retrying")),
            "camera_process_pid": decoder.get("camera_process_pid"),
            "last_frame_age_seconds": decoder.get("last_frame_age_seconds"),
            "reconnects": int(decoder.get("reconnects") or 0),
            "decoder_errors": int(decoder.get("decoder_errors") or 0),
        }
    return result


def run_probe(args: argparse.Namespace) -> tuple[dict, Path]:
    output = Path(tempfile.mkdtemp(
        prefix=f"sentry-v8-capacity-{args.cameras}.", dir="/tmp"
    ))
    config_dir = output / "config"
    secret_dir = output / "cameras"
    delivery_dir = output / "delivery"
    for path in (config_dir, secret_dir, delivery_dir):
        path.mkdir(mode=0o755)
    os.chmod(delivery_dir, 0o777)

    source_processes: list[list[object | None]] = []
    source_log_dir = None

    def launch_source(index: int, *, append: bool = False) -> list[object]:
        port = args.source_port_base + index
        mode = "ab" if append else "wb"
        log_stream = (source_log_dir / f"source-{index + 1:02d}.log").open(mode)
        if os.environ.get("SENTRY_TEST_MARKER_REPLAY"):
            process = subprocess.Popen([
                "python3", str(ROOT / "scripts/isolated_validation/marker_sender.py"),
                "--port", str(port), "--fixture", (json.loads(os.environ["SENTRY_TEST_RECORDINGS"])[index] if os.environ.get("SENTRY_TEST_RECORDINGS") else str(args.recording)),
                "--logdir", os.environ["SENTRY_TEST_MARKER_REPLAY"],
            ], stdout=subprocess.DEVNULL, stderr=log_stream)
            return [process, log_stream]
        if os.environ.get("SENTRY_TEST_REUSABLE_REPLAY"):
            suite=json.loads(os.environ.get("SENTRY_TEST_RECORDINGS","[]"))
            fixture=suite[index % len(suite)] if suite else str(args.recording)
            process=subprocess.Popen([
                "python3",str(ROOT / "scripts/isolated_validation/reusable_sender.py"),
                "--port",str(port),"--fixture",fixture,
            ],stdout=subprocess.DEVNULL,stderr=log_stream)
            return [process,log_stream]
        process = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",
            "-stream_loop", "-1", "-i", str((json.loads(os.environ["SENTRY_TEST_RECORDINGS"])[index % len(json.loads(os.environ["SENTRY_TEST_RECORDINGS"]))]) if os.environ.get("SENTRY_TEST_RECORDINGS") else args.recording),
            "-map", "0:v:0", "-an", "-c:v", "copy", "-f", "mpegts",
            "-listen", "1", f"tcp://127.0.0.1:{port}",
        ], stdout=subprocess.DEVNULL, stderr=log_stream)
        return [process, log_stream]

    if args.recording:
        source_log_dir = output / "source-logs"
        source_log_dir.mkdir()
        source_urls = []
        for index in range(args.cameras):
            port = args.source_port_base + index
            source_urls.append(f"tcp://127.0.0.1:{port}")
            source_processes.append(launch_source(index))
        time.sleep(2)
        failed_sources = [
            index + 1 for index, (process, _) in enumerate(source_processes)
            if process.poll() is not None
        ]
        if failed_sources:
            raise RuntimeError(f"local TCP sources failed: {failed_sources}")
    elif args.source_url:
        source_urls = [args.source_url for _ in range(args.cameras)]
    else:
        source_urls = [SOURCE_URLS[index % len(SOURCE_URLS)]
                       for index in range(args.cameras)]

    state = desired_state(
        args.cameras,
        args.fps,
        omit_fps=args.omit_fps,
        full_usecases=args.full_usecases,
    )
    (config_dir / "desired_state.json").write_text(canonical(state) + "\n")
    for index, camera in enumerate(state["cameras"]):
        camera_id = camera["camera_id"]
        (secret_dir / f"{camera_id}.url").write_text(
            source_urls[index] + "\n"
        )
    os.chmod(config_dir / "desired_state.json", 0o644)
    for path in secret_dir.iterdir():
        os.chmod(path, 0o644)

    socket_path = delivery_dir / "agent.sock"
    agent_log = (output / "agent.log").open("wb")
    agent = subprocess.Popen([
        "python3", str(AGENT), "--socket", str(socket_path),
        "--schemas", str(SCHEMAS), "--log", str(output / "packages.jsonl"),
        "--status", str(output / "agent-status.json"),
    ], stdout=agent_log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 10
    while not socket_path.exists() and time.monotonic() < deadline:
        if agent.poll() is not None:
            raise RuntimeError("mock delivery agent exited during startup")
        time.sleep(0.1)
    if not socket_path.exists():
        raise RuntimeError("mock delivery-agent socket did not appear")

    name = f"sentinel-v8-capacity-{args.cameras}-{os.getpid()}"
    run = [
        "podman", "run", "-d", "--name", name, "--network=host",
        "--device", "/dev/dri", "--group-add", "keep-groups",
        "-e", f"APEX_API_PORT={args.port}",
        "-e", "PYTHONFAULTHANDLER=1",
        "-v", f"{config_dir / 'desired_state.json'}:/configs/desired_state.json:ro",
        "-v", f"{secret_dir}:/run/secrets/sentinel/cameras:ro",
        "-v", f"{delivery_dir}:/run/sentinel-delivery",
    ]
    if args.gva_nireq is not None:
        run[6:6] = ["-e", f"SENTINEL_GVA_NIREQ={args.gva_nireq}"]
    for value in args.container_env:
        if "=" not in value or value.startswith("="):
            raise ValueError("--container-env requires KEY=VALUE")
        run.extend(["-e", value])
    if Path("/dev/accel/accel0").exists():
        run[6:6] = ["--device", "/dev/accel/accel0"]
    run.extend(["-v", "/home/admin1/Documents/sentry-validation/fixtures:/fixtures:ro"])
    run.append(args.image)

    samples: list[dict] = []
    isolation_samples: list[dict] = []
    outage_started_at = None
    outage_restored_at = None
    outage_index = (args.outage_camera - 1) if args.outage_camera else None
    if outage_index is not None and not args.recording:
        raise ValueError("--outage-camera requires --recording")
    if outage_index is not None and not 0 <= outage_index < args.cameras:
        raise ValueError("--outage-camera must identify a configured camera")
    logs = ""
    last_health_payload: dict | None = None
    started = time.monotonic()
    startup_completed = None
    try:
        subprocess.run(run, check=True, timeout=60, stdout=subprocess.DEVNULL)
        print(f"[{args.cameras}] container started; waiting for all cameras", flush=True)
        deadline = time.monotonic() + args.startup_timeout
        while time.monotonic() < deadline:
            payload = health(args.port)
            if payload:
                last_health_payload = payload
                summary = summarize(payload, args.cameras, args.fps)
                if (summary["camera_records"] == args.cameras
                        and summary["cameras_with_recent_frames"] == args.cameras):
                    startup_completed = time.monotonic()
                    samples.append(summary)
                    print(
                        f"[{args.cameras}] all cameras active after "
                        f"{startup_completed - started:.1f}s; "
                        f"decode={summary['aggregate_decode_fps']}/"
                        f"{summary['target_decode_fps']} fps",
                        flush=True,
                    )
                    break
                if int(time.monotonic() - started) % 30 == 0:
                    print(
                        f"[{args.cameras}] startup: records={summary['camera_records']} "
                        f"recent={summary['cameras_with_recent_frames']} "
                        f"decode={summary['aggregate_decode_fps']}",
                        flush=True,
                    )
            time.sleep(1)

        if startup_completed is None:
            print(f"[{args.cameras}] startup timeout", flush=True)
        else:
            if args.warmup_seconds > 0:
                print(
                    f"[{args.cameras}] warming shared graph for "
                    f"{args.warmup_seconds}s",
                    flush=True,
                )
                time.sleep(args.warmup_seconds)
            steady_deadline = time.monotonic() + args.steady_seconds
            next_sample = time.monotonic()
            while time.monotonic() < steady_deadline:
                now = time.monotonic()
                if (outage_index is not None and outage_started_at is None
                        and now >= startup_completed + args.outage_after_seconds):
                    process, log_stream = source_processes[outage_index]
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    log_stream.close()
                    source_processes[outage_index] = [None, None]
                    outage_started_at = time.monotonic()
                    print(
                        f"[{args.cameras}] stopped source for camera "
                        f"{outage_index + 1}; checking peer isolation",
                        flush=True,
                    )
                if (outage_started_at is not None and outage_restored_at is None
                        and now >= outage_started_at + args.outage_seconds):
                    source_processes[outage_index] = launch_source(
                        outage_index, append=True
                    )
                    outage_restored_at = time.monotonic()
                    print(
                        f"[{args.cameras}] restored source for camera "
                        f"{outage_index + 1}; waiting for branch reconnect",
                        flush=True,
                    )
                if time.monotonic() >= next_sample:
                    payload = health(args.port)
                    if payload:
                        last_health_payload = payload
                        summary = summarize(payload, args.cameras, args.fps)
                        summary["sample_phase"] = "steady"
                        summary["sample_monotonic_seconds"] = time.monotonic()
                        samples.append(summary)
                        if outage_started_at is not None:
                            isolation_samples.append({
                                "phase": (
                                    "outage" if outage_restored_at is None
                                    else "recovery"
                                ),
                                "seconds_since_outage": round(
                                    time.monotonic() - outage_started_at, 3
                                ),
                                "cameras": camera_runtime_snapshot(payload),
                            })
                        print(
                            f"[{args.cameras}] steady: decode="
                            f"{summary['aggregate_decode_fps']}/"
                            f"{summary['target_decode_fps']} "
                            f"ratio={summary['decode_target_ratio']:.3f} "
                            f"recent={summary['cameras_with_recent_frames']} "
                            f"mem={summary['container_memory_mb']}MB "
                            f"gpu={summary['gpu_utilization_pct']}%",
                            flush=True,
                        )
                    next_sample += args.sample_seconds
                time.sleep(0.5)
    finally:
        final_health = health(args.port)
        if final_health:
            last_health_payload = final_health
        if last_health_payload:
            (output / "healthz-final.json").write_text(
                json.dumps(last_health_payload, indent=2) + "\n"
            )
        result = subprocess.run(
            ["podman", "logs", name], text=True, capture_output=True, timeout=60
        )
        logs = result.stdout + result.stderr
        (output / "container.log").write_text(logs)
        subprocess.run(["podman", "stop", "--time", "15", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["podman", "rm", "-f", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
        agent_log.close()
        for process, log_stream in source_processes:
            if process is not None:
                process.terminate()
        for process, log_stream in source_processes:
            if process is None:
                continue
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log_stream.close()

    fatal_pattern = re.compile(
        r"segmentation fault|sigsegv|sigabrt|core dumped|"
        r"multi_graphics_allocation|camera process (?:group )?exited|"
        r"longjmp causes uninitialized stack frame|CL_OUT_OF_RESOURCES|"
        r"camera_thread_unhandled_exception|secondary produced no output tensor|"
        r'dls_secondary_job.*"successful"\s*:\s*false',
        re.IGNORECASE,
    )
    fatal_lines = [line for line in logs.splitlines() if fatal_pattern.search(line)]
    # During an intentional source outage, the rolling FPS window necessarily
    # includes the missing interval after the branch has reconnected.  Require
    # two consecutive final healthy windows for that fault-injection case;
    # ordinary capacity qualification retains the stricter three-window rule.
    required_stable_samples = 2 if outage_index is not None else 3
    stable_samples = samples[-min(required_stable_samples, len(samples)):]
    minimum_ratio = min(
        (float(item["decode_target_ratio"]) for item in stable_samples),
        default=0.0,
    )
    final = samples[-1] if samples else (
        summarize(last_health_payload, args.cameras, args.fps)
        if last_health_payload else {}
    )
    handoff = final.get("handoff") or {}
    end_health = summarize(last_health_payload, args.cameras, args.fps) if last_health_payload else {}
    package_summary = {
        "package_count": 0,
        "event_types": {},
        "evidence_objects": 0,
        "embedding_objects": 0,
        "embedding_profiles": {},
    }
    packages_path = output / "packages.jsonl"
    if packages_path.is_file():
        for line in packages_path.read_text().splitlines():
            if not line.strip():
                continue
            package = json.loads(line)
            package_summary["package_count"] += 1
            event_type = str(package.get("event_type") or "missing")
            package_summary["event_types"][event_type] = (
                package_summary["event_types"].get(event_type, 0) + 1
            )
            evidence = package.get("evidence") or []
            embeddings = package.get("embeddings") or []
            package_summary["evidence_objects"] += len(evidence)
            package_summary["embedding_objects"] += len(embeddings)
            for profile in embeddings:
                profile = str(profile)
                package_summary["embedding_profiles"][profile] = (
                    package_summary["embedding_profiles"].get(profile, 0) + 1
                )
    full_usecases_passed = True
    if args.full_usecases:
        full_usecases_passed = bool(
            package_summary["event_types"].get("camera_health")
            and package_summary["event_types"].get("zone_count")
            and package_summary["event_types"].get("scene_sample")
            and package_summary["evidence_objects"] > 0
            and package_summary["embedding_profiles"].get("siglip2-base-v1")
        )
    isolation_passed = True
    if outage_index is not None:
        target_id = f"capacity-{outage_index + 1:02d}"
        interruption_records = [
            item for item in isolation_samples
            if (
                (item["cameras"].get(target_id) or {}).get("state")
                in {"degraded", "unavailable"}
                or int((item["cameras"].get(target_id) or {}).get(
                    "decoder_errors"
                ) or 0) > 0
                or int((item["cameras"].get(target_id) or {}).get(
                    "reconnects"
                ) or 0) > 0
            )
        ]
        recovery_records = [
            item for item in isolation_samples if item["phase"] == "recovery"
        ]
        target_interrupted = bool(interruption_records)
        peers_healthy = any(
            sum(
                1 for camera_id, record in item["cameras"].items()
                if camera_id != target_id and record.get("state") == "healthy"
            ) == args.cameras - 1
            for item in interruption_records
        )
        target_recovered = any(
            (item["cameras"].get(target_id) or {}).get("state") == "healthy"
            and int((item["cameras"].get(target_id) or {}).get("reconnects") or 0) > 0
            for item in recovery_records
        )
        isolation_passed = bool(
            target_interrupted and peers_healthy and target_recovered
        )
    else:
        target_interrupted = peers_healthy = target_recovered = None
    measured_throughput = steady_throughput(samples, args.minimum_decode_ratio)
    passed = bool(
        startup_completed is not None
        and len(stable_samples) >= required_stable_samples
        and minimum_ratio >= args.minimum_decode_ratio
        and (outage_index is not None or measured_throughput["passed"])
        and final.get("cameras_with_recent_frames") == args.cameras
        and final.get("camera_states", {}).get("healthy") == args.cameras
        and end_health.get("cameras_with_recent_frames") == args.cameras
        and end_health.get("camera_states", {}).get("healthy") == args.cameras
        and int(handoff.get("protocol_errors") or 0) == 0
        and int(handoff.get("exhausted") or 0) == 0
        and isolation_passed
        and full_usecases_passed
        and not fatal_lines
    )
    report = {
        "image": args.image,
        "camera_count": args.cameras,
        "fps_per_camera": args.fps,
        "fps_explicit_in_desired_state": not args.omit_fps,
        "source_urls": [sanitized_url(value) for value in source_urls],
        "source_reuse": bool(
            (args.source_url and args.cameras > 1)
            or (not args.recording and not args.source_url
                and args.cameras > len(SOURCE_URLS))
        ),
        "source_mode": (
            "local_independent_tcp_replays" if args.recording
            else "explicit_live_source" if args.source_url
            else "live_tcp"
        ),
        "motion_gate_enabled": True,
        "full_usecases": args.full_usecases,
        "steady_throughput": measured_throughput,
        "capacity_qualified": bool(passed and outage_index is None),
        "test_scope": "isolation" if outage_index is not None else "capacity",
        "end_health": end_health,
        "startup_seconds": (
            round(startup_completed - started, 3) if startup_completed else None
        ),
        "steady_seconds": args.steady_seconds,
        "minimum_required_decode_ratio": args.minimum_decode_ratio,
        "required_final_stable_samples": required_stable_samples,
        "minimum_observed_decode_ratio": round(minimum_ratio, 4),
        "passed": passed,
        "fatal_lines": fatal_lines,
        "final": final,
        "samples": samples,
        "isolation": {
            "requested": outage_index is not None,
            "camera_number": args.outage_camera,
            "target_interrupted": target_interrupted,
            "peers_healthy": peers_healthy,
            "target_recovered": target_recovered,
            "passed": isolation_passed,
            "samples": isolation_samples,
        },
        "package_summary": package_summary,
        "full_usecases_minimum_outputs_passed": full_usecases_passed,
        "artifact_directory": str(output),
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"[{args.cameras}] {'PASS' if passed else 'FAIL'} artifacts={output}", flush=True)
    print(canonical(report), flush=True)
    return report, output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--cameras", type=int, required=True)
    parser.add_argument("--fps", type=int, default=6)
    parser.add_argument(
        "--omit-fps", action="store_true",
        help="omit camera FPS and verify the contract/runtime default of 8",
    )
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--startup-timeout", type=int, default=240)
    parser.add_argument("--steady-seconds", type=int, default=90)
    parser.add_argument("--warmup-seconds", type=int, default=0)
    parser.add_argument("--sample-seconds", type=int, default=15)
    parser.add_argument("--minimum-decode-ratio", type=float, default=0.90)
    parser.add_argument("--recording", type=Path)
    parser.add_argument(
        "--container-env", action="append", default=[], metavar="KEY=VALUE",
        help="bounded runtime override used by qualification experiments",
    )
    parser.add_argument("--source-url")
    parser.add_argument("--full-usecases", action="store_true")
    parser.add_argument("--source-port-base", type=int, default=19001)
    parser.add_argument("--outage-camera", type=int)
    parser.add_argument("--outage-after-seconds", type=int, default=20)
    parser.add_argument("--outage-seconds", type=int, default=20)
    parser.add_argument("--gva-nireq", type=int, choices=range(1, 9))
    args = parser.parse_args()
    if not 1 <= args.cameras <= 50:
        parser.error("--cameras must be between 1 and 50")
    if args.recording and not args.recording.is_file():
        parser.error("--recording must be an existing video file")
    report, _ = run_probe(args)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
