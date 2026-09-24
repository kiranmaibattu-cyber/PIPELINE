"""Verify v18 full-frame counting and a live ROI update without restarts."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"sporada-v18-hot-reload-{int(time.time())}"
NAME = "sporada-v18-hot-reload"
IMAGE = "localhost/sporada:intel-285h-2026.09.23-v18"
API = "http://127.0.0.1:18080"


def desired(revision: int, with_roi: bool) -> dict:
    camera = {
        "camera_id": "traffic-optional-roi",
        "source": "file:/run/secrets/apexfabric/traffic.url",
        "solution_pack": "sporada-secure",
        "fps": 5,
        "apps": ["vehicle_counting", "pedestrian_counting"],
        "config": {
            "embedding": {"model_id": "face-embedding-model-v1", "dimensions": 512},
            "emission": {"minimum_quality": 0.2, "preferred_quality": 0.65,
                         "selection_window_seconds": 1.5, "cooldown_seconds": 30,
                         "material_change_threshold": 0.15},
            "zones": {},
        },
    }
    if with_roi:
        whole_frame = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        camera["config"]["zones"] = {
            "vehicle_counting": [{"id": "vehicle-roi", "name": "Vehicle ROI", "poly": whole_frame}],
            "pedestrian_counting": [{"id": "pedestrian-roi", "name": "Pedestrian ROI", "poly": whole_frame}],
        }
    return {"edge_id": "sporada-v18-hot-reload", "revision": revision, "cameras": [camera]}


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
                metrics = response.read().decode("utf-8")
            try:
                with urlopen(API + "/readyz", timeout=3) as response:
                    readiness = json.load(response)
            except HTTPError as exc:
                readiness = json.load(exc)
            match = re.search(r"^apexfabric_runtime_revision (\d+)$", metrics, re.MULTILINE)
            active_revision = int(match.group(1)) if match else None
            latest = {"readiness": readiness, "revision": active_revision, "metrics": metrics}
            if readiness.get("ready") and readiness.get("worker_running") and active_revision == revision:
                return latest
        except (OSError, URLError, ValueError, json.JSONDecodeError):
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
    (RUN / "secrets" / "traffic.url").write_text(
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
            "active_revision": second_metrics["revision"],
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
