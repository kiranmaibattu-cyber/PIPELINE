"""Verify full-frame counting and a live ROI update in the traffic v12 image."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"traffic-v12-optional-roi-{int(time.time())}"
NAME = "pipeline-traffic-v12-optional-roi"
IMAGE = "localhost/traffic-pilot-runtime:intel-285h-2026.09.18-v12"
API = "http://127.0.0.1:18080"


def desired(revision: int, with_roi: bool) -> dict:
    camera = {
        "camera_id": "traffic-optional-roi",
        "source": "file:/run/secrets/apexfabric/traffic.rtsp",
        "solution_pack": "sporada-secure",
        "fps": 5,
        "apps": ["vehicle_counting", "pedestrian_counting"],
    }
    if with_roi:
        whole_frame = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        camera["config"] = {"zones": {
            "vehicle_counting": [{"id": "vehicle-roi", "name": "Vehicle ROI", "poly": whole_frame}],
            "pedestrian_counting": [{"id": "pedestrian-roi", "name": "Pedestrian ROI", "poly": whole_frame}],
        }}
    return {"edge_id": "traffic-v12-live-test", "revision": revision, "cameras": [camera]}


def write_desired(document: dict) -> None:
    target = RUN / "configs" / "desired_state.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(document), encoding="utf-8")
    temporary.replace(target)


def wait_ready(revision: int, timeout: int = 75) -> dict:
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        try:
            with urlopen(API + "/metrics", timeout=3) as response:
                latest = json.load(response)
            runtime = latest["runtime"]
            if runtime["revision"] == revision and runtime["child_running"]:
                return latest
        except (OSError, URLError, KeyError, json.JSONDecodeError):
            pass
        time.sleep(1)
    raise AssertionError(f"revision {revision} did not become ready: {latest}")


def collect(seconds: int, filename: str) -> list[dict]:
    path = RUN / filename
    with path.open("w", encoding="utf-8") as output:
        subprocess.run(
            ["curl", "-sSN", "--max-time", str(seconds), API + "/events"],
            stdout=output,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    return [
        json.loads(line[6:])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("data: ")
    ]


def location_ids(events: list[dict]) -> set[str]:
    return {
        event.get("payload", {}).get("location", {}).get("id")
        for event in events
        if event.get("application") in {"vehicle_counting", "pedestrian_counting"}
    }


def worker_pid() -> str:
    rows = subprocess.check_output(
        ["podman", "top", NAME, "pid,args"], text=True
    ).splitlines()
    matches = [row.split(None, 1)[0] for row in rows if "stream_fleet_openvino" in row]
    if len(matches) != 1:
        raise AssertionError(f"expected one OpenVINO worker, found: {rows}")
    return matches[0]


def main() -> None:
    for folder in ("configs", "secrets", "state"):
        (RUN / folder).mkdir(parents=True, exist_ok=True)
    write_desired(desired(1, with_roi=False))
    (RUN / "secrets" / "traffic.rtsp").write_text(
        "rtsp://192.168.1.95:8554/traffic1\n", encoding="utf-8"
    )
    subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)
    try:
        subprocess.run([
            "podman", "run", "-d", "--name", NAME, "--network=host",
            "--group-add", "keep-groups", "--security-opt", "label=disable",
            "--device", "/dev/dri:/dev/dri", "--device", "/dev/accel:/dev/accel",
            "-e", "APEX_API_PORT=18080",
            "-v", f"{RUN / 'configs'}:/configs:ro",
            "-v", f"{RUN / 'secrets'}:/run/secrets/apexfabric:ro",
            "-v", f"{RUN / 'state'}:/state:U",
            IMAGE,
        ], check=True, stdout=subprocess.DEVNULL)

        first_metrics = wait_ready(1)
        container_id = subprocess.check_output(
            ["podman", "inspect", "-f", "{{.Id}}", NAME], text=True
        ).strip()
        initial_worker_pid = worker_pid()
        full_frame_events = collect(25, "full-frame.sse")
        full_frame_ids = location_ids(full_frame_events)
        assert "zone:whole_frame" in full_frame_ids, full_frame_ids

        write_desired(desired(2, with_roi=True))
        second_metrics = wait_ready(2)
        time.sleep(4)
        roi_events = collect(25, "roi.sse")
        roi_ids = location_ids(roi_events)
        assert {"vehicle-roi", "pedestrian-roi"} <= roi_ids, roi_ids
        assert worker_pid() == initial_worker_pid
        assert subprocess.check_output(
            ["podman", "inspect", "-f", "{{.Id}}", NAME], text=True
        ).strip() == container_id

        report = {
            "image": IMAGE,
            "full_frame_events": len(full_frame_events),
            "full_frame_location_ids": sorted(item for item in full_frame_ids if item),
            "roi_events": len(roi_events),
            "roi_location_ids": sorted(item for item in roi_ids if item),
            "container_restarted": False,
            "worker_restarted": False,
            "active_revision": second_metrics["runtime"]["revision"],
        }
        (RUN / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
        print(f"Artifacts: {RUN}", flush=True)
    finally:
        logs = subprocess.run(["podman", "logs", NAME], capture_output=True, text=True)
        (RUN / "container.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        subprocess.run(["podman", "rm", "-f", NAME], capture_output=True)


if __name__ == "__main__":
    main()
