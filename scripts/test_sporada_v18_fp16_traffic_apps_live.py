#!/usr/bin/env python3
"""Exercise the four non-face, non-crossing rebuilt v18 FP16 apps on live RTSP."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

import jsonschema
import cv2


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"sporada-v18-fp16-traffic-apps-{int(time.time())}"
NAME = "sporada-v18-fp16-traffic-apps"
IMAGE = "localhost/sporada:intel-285h-2026.09.23-v18"
API = "http://127.0.0.1:18088"


def ready() -> bool:
    try:
        with urlopen(API + "/readyz", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def parse_sse(path: Path) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("data: ")
    ]


def render_detection_proof(event: dict, destination: Path) -> int:
    payload = event.get("payload") or {}
    snapshot_ref = payload.get("snapshot_ref")
    if not snapshot_ref:
        return 0
    source = RUN / "state" / snapshot_ref
    frame = cv2.imread(str(source))
    if frame is None:
        return 0
    objects = payload.get("objects") or []
    for item in objects:
        bbox = item.get("bbox") or {}
        try:
            x1, y1, x2, y2 = (int(bbox[key]) for key in ("x1", "y1", "x2", "y2"))
        except (KeyError, TypeError, ValueError):
            continue
        label = f"{item.get('type', 'object')} {float(item.get('confidence', 0)):.2f} id={item.get('track_id')}"
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 3)
        cv2.putText(frame, label, (x1, max(24, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.65, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(frame, label, (x1, max(24, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.65, (0, 255, 255), 2, cv2.LINE_AA)
    cv2.imwrite(str(destination), frame, [cv2.IMWRITE_JPEG_QUALITY, 94])
    return len(objects)


def main() -> None:
    for folder in ("configs", "secrets", "state"):
        (RUN / folder).mkdir(parents=True, exist_ok=True)
    (RUN / "secrets/traffic.url").write_text("rtsp://192.168.1.95:8554/traffic1\n")
    desired = {
        "edge_id": "sporada-v18-fp16-traffic-live",
        "revision": 1,
        "cameras": [{
            "camera_id": "traffic-live",
            "source": "file:/run/secrets/apexfabric/traffic.url",
            "solution_pack": "sporada-secure",
            "fps": 8,
            "apps": ["anpr", "vehicle_counting", "pedestrian_counting", "fire_smoke_detection"],
            "config": {
                "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
                "emission": {"minimum_quality": 0.55, "cooldown_seconds": 30,
                             "material_change_threshold": 0.15},
                "zones": {},
            },
        }],
    }
    (RUN / "configs/desired_state.json").write_text(json.dumps(desired, indent=2))
    subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
    command = [
        "podman", "run", "-d", "--name", NAME, "--network=host", "--group-add", "keep-groups",
        "--security-opt", "label=disable", "-e", "APEX_API_PORT=18088",
        "-v", f"{RUN / 'configs'}:/configs:ro", "-v", f"{RUN / 'secrets'}:/run/secrets/apexfabric:ro",
        "-v", f"{RUN / 'state'}:/state:U",
    ]
    for device in ("/dev/dri", "/dev/accel"):
        if Path(device).exists():
            command.extend(["--device", f"{device}:{device}"])
    command.append(IMAGE)
    try:
        subprocess.run(command, check=True, capture_output=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not ready():
            time.sleep(1)
        assert ready(), "rebuilt v18 FP16 traffic profile did not become ready"
        sse = RUN / "events.sse"
        with sse.open("w", encoding="utf-8") as output:
            subprocess.run(
                ["curl", "-sSN", "--max-time", "75", API + "/events"],
                stdout=output, stderr=subprocess.DEVNULL, check=False,
            )
        events = parse_sse(sse)
        schema = json.loads(
            (ROOT / "delivery/apexfabric-v1/intel-285h/traffic-v18/analytics-event.schema.json").read_text()
        )
        for event in events:
            jsonschema.validate(event, schema)
        by_app: dict[str, list[dict]] = {}
        for event in events:
            by_app.setdefault(event["application"], []).append(event)
        assert by_app.get("vehicle_counting"), "no live vehicle-counting event"
        assert by_app.get("pedestrian_counting"), "no live pedestrian-counting event"
        checked_snapshots = {}
        for app in ("vehicle_counting", "pedestrian_counting", "anpr", "fire_smoke_detection"):
            app_events = by_app.get(app) or []
            if not app_events:
                continue
            url = app_events[0].get("payload", {}).get("snapshot_url")
            if url:
                with urlopen(API + url, timeout=5) as response:
                    checked_snapshots[app] = len(response.read())
        with urlopen(API + "/metrics", timeout=5) as response:
            metrics = response.read().decode()
        (RUN / "metrics.prom").write_text(metrics)
        visual_event = next(event for event in by_app["vehicle_counting"]
                            if event.get("payload", {}).get("snapshot_ref"))
        visual_path = RUN / "vehicle-detections-visual.jpg"
        visual_objects = render_detection_proof(visual_event, visual_path)
        assert visual_objects > 0 and visual_path.is_file(), "could not render vehicle proof"
        live_logs = subprocess.run(
            ["podman", "logs", NAME], capture_output=True, text=True
        )
        report = {
            "image": IMAGE,
            "events_by_application": {key: len(value) for key, value in sorted(by_app.items())},
            "snapshots_checked_bytes": checked_snapshots,
            "anpr_positive_event": bool(by_app.get("anpr")),
            "fire_smoke_positive_event": bool(by_app.get("fire_smoke_detection")),
            "schema_validated_events": len(events),
            "gpu_resource_error": "CL_OUT_OF_RESOURCES" in (live_logs.stdout + live_logs.stderr),
            "visual_proof": str(visual_path),
            "visual_objects": visual_objects,
        }
        assert not report["gpu_resource_error"], "FP16 runtime encountered CL_OUT_OF_RESOURCES"
        (RUN / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        logs = subprocess.run(["podman", "logs", NAME], capture_output=True, text=True)
        (RUN / "container.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
        print("Artifacts:", RUN)


if __name__ == "__main__":
    main()
