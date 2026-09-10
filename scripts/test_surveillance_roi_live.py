"""Test polygon counts, evidence URLs, and live ROI replacement on ch9."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"exact-evidence-roi-{int(time.time())}"
NAME = "pipeline-roi-count-test"
IMAGE = "localhost/surveillance-edge-runtime:intel-285h-2026.09.10-v3"
API = "http://127.0.0.1:18081"


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def ready(revision):
    for _ in range(60):
        try:
            with urlopen(API + "/readyz", timeout=2) as response:
                data = json.load(response)
            if data.get("ready") and data.get("revision") == revision:
                return
        except OSError:
            pass
        time.sleep(1)
    raise AssertionError(f"Revision {revision} did not become ready")


def collect(label, seconds, old_ids=None):
    path = RUN / f"{label}.sse"
    with path.open("w") as output:
        subprocess.run(["curl", "-sSN", "--max-time", str(seconds), API + "/events"],
                       stdout=output, stderr=subprocess.DEVNULL)
    events = [json.loads(line[6:]) for line in path.read_text().splitlines()
              if line.startswith("data: ")]
    events = [e for e in events if e["event_id"] not in (old_ids or set())]
    assert events, f"No events in {label}"
    counts, checked, unavailable = {}, 0, 0
    for event in events:
        assert event["event_type"] == "people_count_event"
        payload = event["payload"]
        assert payload["mode"] == "roi_occupancy", payload
        counts.setdefault(payload["zone"], []).append(payload["count"])
        assets = payload.get("snapshot_assets") or {}
        if payload.get("evidence_status") == "available":
            assert "frame" in assets
            for kind, asset in assets.items():
                with urlopen(API + asset["url"], timeout=5) as response:
                    jpeg = response.read()
                assert jpeg.startswith(b"\xff\xd8")
                if kind == "frame":
                    assert hashlib.sha256(jpeg).hexdigest() == payload["evidence"]["sha256"]
                    sample = RUN / f"{label}-{payload['zone']}.jpg"
                    if not sample.exists():
                        sample.write_bytes(jpeg)
                checked += 1
        else:
            unavailable += 1
            assert not assets
    report = dict(stage=label, events=len(events), counts=counts,
                  verified_assets=checked, unavailable=unavailable)
    (RUN / f"{label}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    return events, report


def main():
    for folder in ("configs", "secrets", "state"):
        (RUN / folder).mkdir(parents=True)
    config = RUN / "configs/desired_state.json"
    zones = [
        {"name": "whole", "poly": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        {"name": "left", "poly": [[0, 0], [.5, 0], [.5, 1], [0, 1]]},
        {"name": "right", "poly": [[.5, 0], [1, 0], [1, 1], [.5, 1]]},
    ]
    desired = dict(edge_id="roi-test", revision=1, cameras=[dict(
        camera_id="ch9-roi", source="file:/run/secrets/apexfabric/ch9.rtsp",
        solution_pack="surveillance", fps=8, apps=["people_counting"],
        config={"zones": {"people_counting": zones}})])
    config.write_text(json.dumps(desired))
    (RUN / "secrets/ch9.rtsp").write_text("rtsp://192.168.1.95:8554/ch9\n")
    print(f"Artifacts: {RUN}", flush=True)
    try:
        command("podman", "run", "-d", "--name", NAME, "--network=host",
                "--group-add", "keep-groups", "--security-opt", "label=disable",
                "--device", "/dev/dri:/dev/dri", "--device", "/dev/accel:/dev/accel",
                "-e", "APEX_API_PORT=18081", "-v", f"{RUN/'configs'}:/configs:ro",
                "-v", f"{RUN/'secrets'}:/run/secrets/apexfabric:ro",
                "-v", f"{RUN/'state'}:/state:U", IMAGE)
        ready(1)
        started = command("podman", "inspect", NAME, "--format", "{{.State.StartedAt}}")
        events, report = collect("three-rois", 60)
        assert set(report["counts"]) == {"whole", "left", "right"}
        assert max(report["counts"]["whole"]) > 0
        assert report["verified_assets"] > 0
        desired["revision"] = 2
        desired["cameras"][0]["config"]["zones"]["people_counting"] = [
            {"name": "empty-corner", "poly": [[0, 0], [.001, 0], [.001, .001], [0, .001]]}]
        pending = config.with_suffix(".tmp")
        pending.write_text(json.dumps(desired))
        pending.replace(config)
        ready(2)
        # Skip events produced by the previous worker during reconfiguration.
        time.sleep(3)
        current, report = collect("changed-roi", 35, {e["event_id"] for e in events})
        corner = [e for e in current if e["payload"]["zone"] == "empty-corner"]
        assert corner, "New polygon never emitted an occupancy event"
        assert all(e["payload"]["count"] == 0 for e in corner)
        activated = min(e["timestamp"] for e in corner)
        assert all(e["payload"]["zone"] == "empty-corner" for e in current
                   if e["timestamp"] >= activated), "Old ROI emitted after replacement"
        assert command("podman", "inspect", NAME, "--format", "{{.State.StartedAt}}") == started
        print("PASS: polygon counts, snapshots, zero occupancy, and live ROI update", flush=True)
    finally:
        result = subprocess.run(["podman", "logs", NAME], capture_output=True, text=True)
        (RUN / "container.log").write_text(result.stdout + result.stderr)
        subprocess.run(["podman", "stop", "-t", "10", NAME], capture_output=True)
        subprocess.run(["podman", "rm", NAME], capture_output=True)


if __name__ == "__main__":
    main()
