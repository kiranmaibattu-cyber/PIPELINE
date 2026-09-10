"""Check parking-only events and plate evidence through the traffic image API."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"exact-evidence-parking-{int(time.time())}"
NAME = "pipeline-parking-plate-test"
IMAGE = "localhost/traffic-edge-runtime:intel-285h-2026.09.10-v2"
API = "http://127.0.0.1:18080"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-anpr", action="store_true")
    parser.add_argument("--duration", type=int, default=70)
    args = parser.parse_args()
    for folder in ("configs", "secrets", "state"):
        (RUN / folder).mkdir(parents=True)
    desired = dict(edge_id="parking-test", revision=1, cameras=[dict(
        camera_id="parking", source="file:/run/secrets/apexfabric/parking.rtsp",
        solution_pack="traffic", fps=8,
        apps=["illegal_parking", "anpr"] if args.with_anpr else ["illegal_parking"],
        config={"zones": {"illegal_parking": [{"name": "test_roi", "poly":
                [[0, 0], [1, 0], [1, 1], [0, 1]]}]}})])
    (RUN / "configs/desired_state.json").write_text(json.dumps(desired))
    (RUN / "secrets/parking.rtsp").write_text("rtsp://192.168.1.95:8554/traffic1\n")
    print(f"Artifacts: {RUN}", flush=True)
    try:
        subprocess.run(["podman", "run", "-d", "--name", NAME, "--network=host",
            "--group-add", "keep-groups", "--security-opt", "label=disable",
            "--device", "/dev/dri:/dev/dri", "--device", "/dev/accel:/dev/accel",
            "-e", "APEX_API_PORT=18080", "-v", f"{RUN / 'configs'}:/configs:ro",
            "-v", f"{RUN / 'secrets'}:/run/secrets/apexfabric:ro",
            "-v", f"{RUN / 'state'}:/state:U", IMAGE], check=True)
        for _ in range(30):
            try:
                with urlopen(API + "/readyz", timeout=2) as response:
                    if json.load(response)["ready"]:
                        break
            except OSError:
                pass
            time.sleep(1)
        with (RUN / "events.sse").open("w") as output:
            subprocess.run(["curl", "-sSN", "--max-time", str(args.duration), API + "/events"],
                           stdout=output, stderr=subprocess.DEVNULL)
        events = [json.loads(line[6:]) for line in (RUN / "events.sse").read_text().splitlines()
                  if line.startswith("data: ")]
        assert events, "No parking trigger observed during test window"
        allowed = {"illegal_parking_event", "plate_read_event"} if args.with_anpr else {"illegal_parking_event"}
        assert all(event["event_type"] in allowed for event in events)
        parking_events = [e for e in events if e["event_type"] == "illegal_parking_event"]
        anpr_events = [e for e in events if e["event_type"] == "plate_read_event"]
        refs = {}
        asset_count = 0
        plate_crops = recognized = earlier = 0
        for event in events:
            payload = event["payload"]
            assets = payload.get("snapshot_assets") or {}
            assert "vehicle_crop" in assets
            assert "event_frame" in assets
            ref = payload.get("vehicle_ref")
            assert ref, "Missing vehicle reference"
            refs.setdefault(ref, []).append(event)
            for asset in assets.values():
                with urlopen(API + asset["url"], timeout=5) as response:
                    content = response.read()
                    assert content.startswith(b"\xff\xd8"), "Snapshot is not a JPEG"
                    asset_count += 1
            if event["event_type"] != "illegal_parking_event":
                assert "plate_crop" in assets
                continue
            plate_crops += "plate_crop" in assets
            recognized += payload.get("plate_status") == "recognized"
            earlier += (payload.get("plate_evidence") or {}).get("basis") == "earlier_track_frame"
        matched = {ref: rows for ref, rows in refs.items()
                   if len({row["event_type"] for row in rows}) == 2}
        report = dict(events=len(events), parking_events=len(parking_events),
                      anpr_events=len(anpr_events), fetched_snapshots=asset_count,
                      shared_vehicle_refs=list(matched), plate_crops=plate_crops,
                      recognized=recognized, earlier_plate_crops=earlier)
        (RUN / "matched_events.json").write_text(json.dumps(matched, indent=2))
        (RUN / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
        assert parking_events, "No parking events observed"
        if args.with_anpr:
            assert anpr_events, "No ANPR events observed"
            assert matched, "No vehicle observed in both event types during test window"
    finally:
        logs = subprocess.run(["podman", "logs", NAME], text=True, capture_output=True)
        (RUN / "container.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["podman", "stop", "-t", "10", NAME], capture_output=True)
        subprocess.run(["podman", "rm", NAME], capture_output=True)


if __name__ == "__main__":
    main()
