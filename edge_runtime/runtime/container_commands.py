"""Generate container commands for solution-pack runtimes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from edge_runtime.graph.models import SolutionRuntimePlan


IMAGE_BY_PACK = {
    "surveillance": "surveillance-edge-runtime:latest",
    "traffic": "traffic-edge-runtime:latest",
}

CONTAINER_BY_PACK = {
    "surveillance": "pipeline-surveillance",
    "traffic": "pipeline-traffic",
}


@dataclass(frozen=True)
class ContainerCommand:
    solution_pack: str
    image: str
    command: tuple[str, ...]

    def shell_line(self) -> str:
        return " ".join(_quote(part) for part in self.command)


class ContainerCommandBuilder:
    def __init__(self, root: Path, plan_dir: Path | None = None, engine: str = "docker") -> None:
        self._root = root
        self._plan_dir = plan_dir
        self._engine = engine

    def build(self, plan: SolutionRuntimePlan) -> ContainerCommand:
        image = IMAGE_BY_PACK[plan.solution_pack]
        container = CONTAINER_BY_PACK[plan.solution_pack]
        generated = self._root / "run" / "generated" / plan.solution_pack
        models = self._root / "models" / plan.solution_pack
        state = self._root / "state" / plan.solution_pack
        plans = self._plan_dir or self._root / "run" / "plans"
        command = [
            self._engine,
            "run",
            "-d",
            "--network=host",
            "--shm-size=1g",
            "--name",
            container,
            "--restart",
            "unless-stopped",
            "--label",
            "pipeline.edge-runtime=true",
            "--label",
            f"pipeline.solution-pack={plan.solution_pack}",
            "--label",
            f"pipeline.revision={plan.revision}",
            "-v",
            f"{plans}:/plans",
            "-v",
            f"{generated}:/generated/{plan.solution_pack}",
            "-v",
            f"{models}:/models/{plan.solution_pack}",
            "-v",
            f"{state}:/state/{plan.solution_pack}",
        ]
        if self._engine.endswith("podman"):
            command.extend(["--group-add", "keep-groups"])
        command.extend(self._device_args())
        command.extend(self._intel_runtime_mounts())
        command.append(image)
        return ContainerCommand(plan.solution_pack, image, tuple(command))

    def stop(self, solution_pack: str) -> ContainerCommand:
        image = IMAGE_BY_PACK[solution_pack]
        container = CONTAINER_BY_PACK[solution_pack]
        return ContainerCommand(
            solution_pack=solution_pack,
            image=image,
            command=(self._engine, "rm", "-f", container),
        )

    @staticmethod
    def _device_args() -> list[str]:
        args = []
        for device in ("/dev/dri", "/dev/accel"):
            if Path(device).exists():
                args.extend(["--device", f"{device}:{device}"])
        return args

    @staticmethod
    def _intel_runtime_mounts() -> list[str]:
        # The Intel GPU/OpenCL/VA-API stack is baked into the image. The current
        # edge host has the NPU packages installed locally without an apt source,
        # so mount only the NPU driver/compiler until the package source is part
        # of the base image.
        if not Path("/dev/accel").exists():
            return []
        mounts = []
        for path in (
            "/usr/lib/x86_64-linux-gnu/libze_intel_npu.so.1.33.0",
            "/usr/lib/x86_64-linux-gnu/libze_intel_npu.so.1",
            "/usr/lib/x86_64-linux-gnu/libze_intel_npu.so",
            "/usr/lib/x86_64-linux-gnu/libopenvino_intel_npu_compiler.so",
            "/usr/lib/x86_64-linux-gnu/libopenvino_intel_npu_compiler_loader.so",
        ):
            mounts.extend(["-v", f"{path}:{path}:ro"])
        return mounts


def _quote(value: str) -> str:
    if not value or any(ch.isspace() for ch in value):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return value
