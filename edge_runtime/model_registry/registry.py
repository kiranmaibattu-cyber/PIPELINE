"""External model-bundle registry and verifier."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from edge_runtime.graph.models import SolutionRuntimePlan


@dataclass(frozen=True)
class ModelFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class ModelBundle:
    model_id: str
    solution_pack: str
    version: str
    mount_dir: str
    files: tuple[ModelFile, ...]


@dataclass(frozen=True)
class ModelPreparationResult:
    solution_pack: str
    model_id: str
    version: str
    status: str
    files: tuple[str, ...]


class ModelRegistry:
    """Read-only registry for model artifacts kept outside Docker images."""

    def __init__(self, bundles: Iterable[ModelBundle]) -> None:
        self._by_key = {(b.solution_pack, b.model_id): b for b in bundles}

    @classmethod
    def from_file(cls, path: Path) -> "ModelRegistry":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        bundles = []
        for solution_pack, models in (data.get("models") or {}).items():
            for model_id, spec in (models or {}).items():
                bundles.append(ModelBundle(
                    model_id=str(model_id),
                    solution_pack=str(solution_pack),
                    version=str(spec["version"]),
                    mount_dir=str(spec["mount_dir"]),
                    files=tuple(
                        ModelFile(path=str(item["path"]), sha256=str(item["sha256"]))
                        for item in spec.get("files") or ()
                    ),
                ))
        return cls(bundles)

    def get(self, solution_pack: str, model_id: str) -> ModelBundle:
        key = (solution_pack, model_id)
        if key not in self._by_key:
            raise KeyError(f"unknown model bundle {solution_pack}/{model_id}")
        return self._by_key[key]


class ModelPreparer:
    """Validates that required external model bundles are present on the edge box."""

    def __init__(self, registry: ModelRegistry, models_root: Path) -> None:
        self._registry = registry
        self._models_root = models_root

    def prepare(self, plans: tuple[SolutionRuntimePlan, ...]) -> tuple[ModelPreparationResult, ...]:
        results = []
        for solution_pack, model_ids in self._required_models(plans).items():
            for model_id in sorted(model_ids):
                bundle = self._registry.get(solution_pack, model_id)
                files = tuple(str(self._models_root / solution_pack / item.path) for item in bundle.files)
                self._verify_files(bundle, files)
                results.append(ModelPreparationResult(
                    solution_pack=solution_pack,
                    model_id=model_id,
                    version=bundle.version,
                    status="ready",
                    files=files,
                ))
            if solution_pack == "surveillance" and "face_embedder" in model_ids:
                bundle = self._registry.get("surveillance", "face_reid_assets")
                files = tuple(str(self._models_root / "surveillance" / item.path) for item in bundle.files)
                self._verify_files(bundle, files)
                results.append(ModelPreparationResult(
                    solution_pack="surveillance",
                    model_id="face_reid_assets",
                    version=bundle.version,
                    status="ready",
                    files=files,
                ))
        return tuple(results)

    def _verify_files(self, bundle: ModelBundle, files: tuple[str, ...]) -> None:
        for model_file, path_text in zip(bundle.files, files):
            path = Path(path_text)
            if not path.is_file():
                raise FileNotFoundError(
                    f"missing model file for {bundle.solution_pack}/{bundle.model_id}: {path}"
                )
            digest = _sha256(path)
            if digest != model_file.sha256:
                raise ValueError(
                    f"checksum mismatch for {bundle.solution_pack}/{bundle.model_id}: "
                    f"{path} expected {model_file.sha256} got {digest}"
                )

    @staticmethod
    def _required_models(plans: tuple[SolutionRuntimePlan, ...]) -> dict[str, set[str]]:
        by_pack: dict[str, set[str]] = {}
        for plan in plans:
            model_ids = by_pack.setdefault(plan.solution_pack, set())
            for service in plan.shared_services:
                model_ids.update(service.models)
        return by_pack


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
