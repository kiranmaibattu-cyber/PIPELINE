"""Local end-to-end validation without Docker."""
from __future__ import annotations

import argparse
from pathlib import Path

from edge_runtime.agent.edge_agent import build_agent
from edge_runtime.solution_packs.surveillance.runtime.config_adapter import SurveillanceConfigAdapter
from edge_runtime.solution_packs.traffic.runtime.config_adapter import TrafficConfigAdapter
from edge_runtime.runtime.plan_loader import RuntimePlanLoader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile graph and generate solution configs")
    parser.add_argument("--root", default=".")
    parser.add_argument("--desired-state", default="configs/desired_state.example.json")
    parser.add_argument("--output-dir", default="run/plans")
    parser.add_argument("--generated-dir", default="run/generated")
    parser.add_argument("--container-engine", default="docker")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    output_dir = (root / args.output_dir).resolve()
    generated_dir = (root / args.generated_dir).resolve()

    agent_args = argparse.Namespace(
        root=str(root),
        output_dir=str(output_dir),
        apply=False,
        container_engine=args.container_engine,
    )
    agent = build_agent(agent_args)
    agent.run((root / args.desired_state).resolve(), output_dir)

    loader = RuntimePlanLoader()
    surveillance_plan = output_dir / "surveillance.runtime_plan.json"
    if surveillance_plan.exists():
        SurveillanceConfigAdapter().write(
            loader.load(surveillance_plan),
            generated_dir / "surveillance",
        )
    traffic_plan = output_dir / "traffic.runtime_plan.json"
    if traffic_plan.exists():
        TrafficConfigAdapter().write(
            loader.load(traffic_plan),
            generated_dir / "traffic",
        )
    print(f"generated config written to {generated_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
