"""Single-image ApexFabric V1 startup: compile desired state, then run the pack."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PACK_RUNTIME = {
    "surveillance": {
        "module": "edge_runtime.solution_packs.surveillance.runtime_8090.launch",
        "models": "/models/surveillance",
        "runtime_port": "8090",
        "metrics_proxy": "http://127.0.0.1:8090/api/metrics",
    },
    "traffic": {
        "module": "edge_runtime.solution_packs.traffic.runtime_pilot.launch",
        "models": "/models/traffic/openvino",
    },
}


def main() -> int:
    solution_pack = os.environ.get("SOLUTION_PACK", "")
    if solution_pack not in PACK_RUNTIME:
        print("solution image startup failed: invalid SOLUTION_PACK", file=sys.stderr, flush=True)
        return 2

    work_root = Path("/tmp/apexfabric")
    work_root.mkdir(parents=True, exist_ok=True)
    plan_dir = Path("/plans")
    plan_dir.mkdir(parents=True, exist_ok=True)
    compile_command = [
        sys.executable,
        "-m",
        "edge_runtime.agent.edge_agent",
        "--desired-state",
        "/configs/desired_state.json",
        "--output-dir",
        str(plan_dir),
        "--models-root",
        "/models",
    ]
    compiled = subprocess.run(compile_command, check=False)
    if compiled.returncode != 0:
        print(
            f"solution image startup failed: graph compiler exited {compiled.returncode}",
            file=sys.stderr,
            flush=True,
        )
        return int(compiled.returncode or 2)

    runtime = PACK_RUNTIME[solution_pack]
    generated = work_root / "generated" / solution_pack
    state = work_root / "state" / solution_pack
    generated.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "edge_runtime.runtime.solution_pack_entrypoint",
        "--solution-pack",
        solution_pack,
        "--runtime-module",
        runtime["module"],
        "--plan",
        str(plan_dir / f"{solution_pack}.runtime_plan.json"),
        "--generated-dir",
        str(generated),
        "--state-dir",
        str(state),
        "--models-dir",
        runtime["models"],
        "--models-root",
        "/models",
    ]
    if runtime.get("runtime_port"):
        command.extend(["--runtime-port", runtime["runtime_port"]])
    if runtime.get("metrics_proxy"):
        command.extend(["--metrics-proxy-url", runtime["metrics_proxy"]])
    os.execv(sys.executable, command)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
