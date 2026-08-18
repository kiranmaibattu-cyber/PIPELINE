"""Surveillance solution-pack runtime entrypoint."""
from __future__ import annotations

import argparse
from pathlib import Path

from edge_runtime.runtime.plan_loader import RuntimePlanLoader
from edge_runtime.solution_packs.surveillance.runtime.config_adapter import SurveillanceConfigAdapter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare surveillance runtime config")
    parser.add_argument("--plan", default="/plans/surveillance.runtime_plan.json")
    parser.add_argument("--output-dir", default="/generated/surveillance")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = RuntimePlanLoader().load(Path(args.plan))
    SurveillanceConfigAdapter().write(plan, Path(args.output_dir))
    print(f"surveillance config written to {Path(args.output_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
