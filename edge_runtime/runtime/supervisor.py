"""Runtime supervisor boundary.

Phase 1 only writes the runtime plans. Later this class will start the two
solution-pack containers and watch their health.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from edge_runtime.graph.models import SolutionRuntimePlan
from edge_runtime.runtime.container_commands import ContainerCommand, ContainerCommandBuilder, IMAGE_BY_PACK


@dataclass(frozen=True)
class SupervisionResult:
    solution_pack: str
    action: str
    detail: str


class RuntimeSupervisor:
    def __init__(self, plan_dir: Path, root: Path, dry_run: bool = True, engine: str = "docker") -> None:
        self._plan_dir = plan_dir
        self._commands = ContainerCommandBuilder(root, plan_dir=plan_dir, engine=engine)
        self._dry_run = dry_run
        self._engine = engine

    def apply(self, plans: tuple[SolutionRuntimePlan, ...]) -> tuple[SupervisionResult, ...]:
        results = []
        wanted = {plan.solution_pack for plan in plans if plan.cameras}
        for solution_pack in sorted(set(IMAGE_BY_PACK) - wanted):
            results.append(self._execute(
                solution_pack=solution_pack,
                action="stop",
                command=self._commands.stop(solution_pack),
                ignore_failure=True,
            ))
        for plan in plans:
            plan_path = self._plan_dir / f"{plan.solution_pack}.runtime_plan.json"
            if not plan.cameras:
                continue
            results.append(self._execute(
                solution_pack=plan.solution_pack,
                action="restart",
                command=self._commands.stop(plan.solution_pack),
                ignore_failure=True,
            ))
            results.append(self._execute(
                solution_pack=plan.solution_pack,
                action="start",
                command=self._commands.build(plan),
                prefix=f"runtime plan written to {plan_path}; ",
            ))
        return tuple(results)

    def _execute(
        self,
        solution_pack: str,
        action: str,
        command: ContainerCommand,
        ignore_failure: bool = False,
        prefix: str = "",
    ) -> SupervisionResult:
        shell_line = command.shell_line()
        if self._dry_run:
            return SupervisionResult(solution_pack, f"dry_run_{action}", f"{prefix}command: {shell_line}")

        if not shutil.which(self._engine):
            return SupervisionResult(solution_pack, "failed", f"{self._engine} is not installed; command: {shell_line}")

        completed = subprocess.run(command.command, capture_output=True, text=True, check=False)
        if completed.returncode == 0 or ignore_failure:
            detail = (completed.stdout or completed.stderr or shell_line).strip()
            return SupervisionResult(solution_pack, action, detail)
        detail = (completed.stderr or completed.stdout or shell_line).strip()
        return SupervisionResult(solution_pack, "failed", detail)
