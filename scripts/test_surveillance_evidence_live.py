"""Exercise exact-frame evidence across a restart and live app update on ch9."""
import hashlib
from datetime import datetime
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run" / f"exact-evidence-{int(time.time())}"
NAME = "pipeline-exact-evidence-test"
IMAGE = "localhost/surveillance-edge-runtime:intel-285h-2026.09.10-v2"
API = "http://127.0.0.1:18081"


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def status():
    with urlopen(API + "/readyz", timeout=3) as response:
        return json.load(response)


def collect(label, since=None):
    path = RUN / f"{label}.sse"
    with path.open("w") as output:
        subprocess.run(["curl", "-sSN", "--max-time", "35", API + "/events"],
                       stdout=output, stderr=subprocess.DEVNULL)
    events = [json.loads(line[6:]) for line in path.read_text().splitlines()
              if line.startswith("data: ")]
    if since is not None:
        events = [event for event in events if datetime.fromisoformat(
            event["timestamp"].replace("Z", "+00:00")).timestamp() >= since]
    checked = 0
    unavailable = 0
    types = {}
    for event in events:
        types[event["event_type"]] = types.get(event["event_type"], 0) + 1
        payload = event["payload"]
        if payload.get("evidence_status") != "available":
            unavailable += 1
            continue
        evidence = payload["evidence"]
        frame_asset = payload["snapshot_assets"]["frame"]
        with urlopen(API + frame_asset["url"], timeout=5) as response:
            jpeg = response.read()
        assert hashlib.sha256(jpeg).hexdigest() == evidence["sha256"], event["event_id"]
        for asset in payload["snapshot_assets"].values():
            with urlopen(API + asset["url"], timeout=5) as response:
                assert response.read()
        checked += 1
    assert checked > 0, f"No verifiable evidence: {label}"
    if label == "after-app-change":
        assert set(types) == {"people_count_event"}, types
    report = dict(stage=label, events=len(events), checked=checked,
                  unavailable=unavailable, types=types, runtime=status())
    (RUN / f"{label}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != "runtime"}), flush=True)
    return {e["event_id"] for e in events}


def main():
    for folder in ("configs", "secrets", "state"):
        (RUN / folder).mkdir(parents=True)
    desired = dict(edge_id="intel-evidence-test", revision=1, cameras=[dict(
        camera_id="ch9-evidence", source="file:/run/secrets/apexfabric/ch9.rtsp",
        solution_pack="surveillance", fps=8, apps=["intrusion", "people_counting"],
        config={"zones": {"intrusion": [{"name": "test", "poly":
                [[0, 0], [1, 0], [1, 1], [0, 1]]}]}})])
    config = RUN / "configs" / "desired_state.json"
    config.write_text(json.dumps(desired))
    (RUN / "secrets" / "ch9.rtsp").write_text("rtsp://192.168.1.95:8554/ch9\n")
    print(f"Artifacts: {RUN}", flush=True)
    try:
        command("podman", "run", "-d", "--name", NAME, "--network=host",
                "--group-add", "keep-groups", "--security-opt", "label=disable",
                "--device", "/dev/dri:/dev/dri", "--device", "/dev/accel:/dev/accel",
                "-e", "APEX_API_PORT=18081", "-v", f"{RUN / 'configs'}:/configs:ro",
                "-v", f"{RUN / 'secrets'}:/run/secrets/apexfabric:ro",
                "-v", f"{RUN / 'state'}:/state:U", IMAGE)
        for _ in range(45):
            try:
                if status()["ready"]:
                    break
            except OSError:
                pass
            time.sleep(1)
        first = collect("before-restart")
        command("podman", "restart", "-t", "10", NAME)
        time.sleep(5)
        second = collect("after-restart")
        assert not first & second, "Previous runtime events replayed"
        container_before = command("podman", "inspect", NAME, "--format", "{{.State.StartedAt}}")
        desired["revision"] = 2
        desired["cameras"][0]["apps"] = ["people_counting"]
        candidate = config.with_suffix(".tmp")
        candidate.write_text(json.dumps(desired))
        candidate.replace(config)
        time.sleep(8)
        collect("after-app-change", since=time.time())
        assert status()["revision"] == 2
        assert command("podman", "inspect", NAME, "--format", "{{.State.StartedAt}}") == container_before
        desired["revision"] = 3
        desired["cameras"][0]["apps"] = ["reid", "face_recognition", "intrusion", "people_counting"]
        candidate.write_text(json.dumps(desired))
        candidate.replace(config)
        time.sleep(8)
        collect("identity-apps-enabled", since=time.time())
        assert status()["revision"] == 3
        print("PASS: exact frame hashes, persistent URLs, restart and live update", flush=True)
    finally:
        logs = subprocess.run(["podman", "logs", NAME], text=True, capture_output=True)
        (RUN / "container.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["podman", "stop", "-t", "10", NAME], capture_output=True)
        subprocess.run(["podman", "rm", NAME], capture_output=True)


if __name__ == "__main__":
    main()
