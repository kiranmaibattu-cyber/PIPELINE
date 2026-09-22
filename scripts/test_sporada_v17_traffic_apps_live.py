#!/usr/bin/env python3
"""Exercise the four non-face, non-crossing v17 applications on live RTSP."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"sporada-v17-traffic-apps-{int(time.time())}"
NAME = "sporada-v17-traffic-apps"
IMAGE = "localhost/traffic-pilot-runtime:intel-285h-2026.09.22-v17"
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


def main() -> None:
    for folder in ("configs", "secrets", "state"):
        (RUN / folder).mkdir(parents=True, exist_ok=True)
    (RUN / "secrets/traffic.url").write_text("rtsp://192.168.1.95:8554/traffic1\n")
    desired = {
        "edge_id": "sporada-v17-traffic-live",
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
        assert ready(), "v17 traffic profile did not become ready"
        sse = RUN / "events.sse"
        with sse.open("w", encoding="utf-8") as output:
            subprocess.run(
                ["curl", "-sSN", "--max-time", "75", API + "/events"],
                stdout=output, stderr=subprocess.DEVNULL, check=False,
            )
        events = parse_sse(sse)
        schema = json.loads(
            (ROOT / "delivery/apexfabric-v1/intel-285h/traffic-v17/analytics-event.schema.json").read_text()
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
        report = {
            "image": IMAGE,
            "events_by_application": {key: len(value) for key, value in sorted(by_app.items())},
            "snapshots_checked_bytes": checked_snapshots,
            "anpr_positive_event": bool(by_app.get("anpr")),
            "fire_smoke_positive_event": bool(by_app.get("fire_smoke_detection")),
            "schema_validated_events": len(events),
        }
        (RUN / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        logs = subprocess.run(["podman", "logs", NAME], capture_output=True, text=True)
        (RUN / "container.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
        print("Artifacts:", RUN)


if __name__ == "__main__":
    main()
