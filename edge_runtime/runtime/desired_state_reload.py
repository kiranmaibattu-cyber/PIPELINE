"""Transactional desired-state observation and runtime-plan compilation."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class DesiredStateReloadError(RuntimeError):
    """Raised when a desired-state candidate cannot be observed or compiled."""


@dataclass(frozen=True)
class DesiredStateSnapshot:
    content: bytes
    digest: str
    revision: int


@dataclass(frozen=True)
class CompiledRuntimePlan:
    content: bytes
    payload: dict


class DesiredStateWatcher:
    """Reads immutable snapshots and identifies them by their content digest."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def snapshot(self) -> DesiredStateSnapshot:
        try:
            content = self.path.read_bytes()
        except OSError as exc:
            raise DesiredStateReloadError(f"desired state cannot be read: {exc}") from exc
        try:
            payload = json.loads(content)
            revision = int(payload["revision"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DesiredStateReloadError(f"desired state header is invalid: {exc}") from exc
        return DesiredStateSnapshot(
            content=content,
            digest=hashlib.sha256(content).hexdigest(),
            revision=revision,
        )


class SubprocessGraphCompiler:
    """Compiles one exact desired-state snapshot without touching the live plan."""

    def __init__(
        self,
        solution_pack: str,
        models_root: Path,
        work_root: Path,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.solution_pack = solution_pack
        self.models_root = models_root
        self.work_root = work_root
        self.timeout_seconds = timeout_seconds

    def compile(self, snapshot: DesiredStateSnapshot) -> CompiledRuntimePlan:
        self.work_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="desired-", dir=self.work_root) as tmp:
                root = Path(tmp)
                desired_path = root / "desired_state.json"
                output_dir = root / "plans"
                desired_path.write_bytes(snapshot.content)
                command = [
                    sys.executable,
                    "-m",
                    "edge_runtime.agent.edge_agent",
                    "--desired-state",
                    str(desired_path),
                    "--output-dir",
                    str(output_dir),
                    "--models-root",
                    str(self.models_root),
                    "--solution-pack",
                    self.solution_pack,
                    "--apexfabric-v1",
                    "--compile-only",
                ]
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout or "unknown compiler failure").strip()
                    raise DesiredStateReloadError(
                        f"graph compiler exited {completed.returncode}: {detail}"
                    )
                plan_path = output_dir / f"{self.solution_pack}.runtime_plan.json"
                content = plan_path.read_bytes()
                payload = json.loads(content)
        except subprocess.TimeoutExpired as exc:
            raise DesiredStateReloadError(
                f"graph compiler timed out after {self.timeout_seconds:g}s"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise DesiredStateReloadError(f"compiled plan is invalid: {exc}") from exc
        return CompiledRuntimePlan(content=content, payload=payload)
