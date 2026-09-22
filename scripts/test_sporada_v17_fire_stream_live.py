#!/usr/bin/env python3
"""Verify positive fire detection from an HTTP-served public flame clip."""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.request import Request, urlopen

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"sporada-v17-fire-stream-{int(time.time())}"
NAME = "sporada-v17-fire-stream"
IMAGE = "localhost/traffic-pilot-runtime:intel-285h-2026.09.22-v17"
API = "http://127.0.0.1:18089"
MEDIA_URL = os.getenv(
    "SPORADA_FIRE_TEST_MEDIA_URL",
    "https://upload.wikimedia.org/wikipedia/commons/6/6d/"
    "Video_of_tabletop_fireplace_%28or_fire_pit%29_burning_with_removed_limiter_grid_-_"
    "don%27t_try_this_at_home%2C_just_for_demo.webm",
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        return


def ready() -> bool:
    try:
        with urlopen(API + "/readyz", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def main() -> None:
    for folder in ("configs", "secrets", "state", "media"):
        (RUN / folder).mkdir(parents=True, exist_ok=True)
    media = RUN / "media/fire.webm"
    request = Request(MEDIA_URL, headers={"User-Agent": "ApexFabric-CV-Acceptance/1.0"})
    with urlopen(request, timeout=30) as response, media.open("wb") as output:
        shutil.copyfileobj(response, output)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 19019), partial(QuietHandler, directory=str(RUN / "media"))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (RUN / "secrets/fire.url").write_text("http://127.0.0.1:19019/fire.webm\n")
    (RUN / "configs/desired_state.json").write_text(json.dumps({
        "edge_id": "sporada-v17-fire-live",
        "revision": 1,
        "cameras": [{
            "camera_id": "fire-live",
            "source": "file:/run/secrets/apexfabric/fire.url",
            "solution_pack": "sporada-secure",
            "fps": 8,
            "apps": ["fire_smoke_detection"],
            "config": {
                "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
                "emission": {"minimum_quality": 0.55, "cooldown_seconds": 30,
                             "material_change_threshold": 0.15},
                "zones": {},
            },
        }],
    }, indent=2))
    subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
    command = [
        "podman", "run", "-d", "--name", NAME, "--network=host", "--group-add", "keep-groups",
        "--security-opt", "label=disable", "-e", "APEX_API_PORT=18089",
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
        assert ready(), "v17 fire profile did not become ready"
        sse = RUN / "events.sse"
        with sse.open("w", encoding="utf-8") as output:
            subprocess.run(
                ["curl", "-sSN", "--max-time", "55", API + "/events"],
                stdout=output, stderr=subprocess.DEVNULL, check=False,
            )
        events = [
            json.loads(line[6:]) for line in sse.read_text().splitlines()
            if line.startswith("data: ")
        ]
        alerts = [event for event in events if event.get("application") == "fire_smoke_detection"]
        assert alerts, "no fire/smoke alert was produced for the positive flame clip"
        schema = json.loads(
            (ROOT / "delivery/apexfabric-v1/intel-285h/traffic-v17/analytics-event.schema.json").read_text()
        )
        for event in alerts:
            jsonschema.validate(event, schema)
        snapshot_url = alerts[0]["payload"]["snapshot_url"]
        with urlopen(API + snapshot_url, timeout=5) as response:
            snapshot_bytes = len(response.read())
        report = {
            "image": IMAGE,
            "source": MEDIA_URL,
            "event_types": sorted({event["event_type"] for event in alerts}),
            "positive_events": len(alerts),
            "snapshot_bytes": snapshot_bytes,
            "schema_validated": True,
        }
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
