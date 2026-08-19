"""Application service coordinating model delivery and validation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from edge_runtime.graph.models import ModelBundleReference, SolutionRuntimePlan
from edge_runtime.model_registry.delivery import ModelBundleResolver
from edge_runtime.model_registry.registry import ModelPreparationResult, ModelPreparer


@dataclass(frozen=True)
class PreparedModels:
    results: tuple[ModelPreparationResult, ...]
    runtime_mounts: Mapping[str, Path]
    deliveries: Mapping[str, dict[str, str]]


class ModelManager:
    """Turns desired bundle references into host-visible runtime mounts."""

    def __init__(
        self,
        resolver: ModelBundleResolver,
        verifier: ModelPreparer,
        local_models_root: Path,
        host_models_root: Path,
    ) -> None:
        self._resolver = resolver
        self._verifier = verifier
        self._local_models_root = local_models_root.resolve()
        self._host_models_root = host_models_root.resolve()

    def prepare(
        self,
        plans: tuple[SolutionRuntimePlan, ...],
        references: tuple[ModelBundleReference, ...],
    ) -> PreparedModels:
        solution_packs = {plan.solution_pack for plan in plans if plan.cameras}
        solution_packs.update(reference.solution_pack for reference in references)
        installed = self._resolver.resolve(solution_packs, references)
        local_roots = {pack: bundle.path for pack, bundle in installed.items()}
        results = self._verifier.prepare(plans, local_roots)
        runtime_mounts = {
            pack: self._host_models_root / bundle.path.resolve().relative_to(self._local_models_root)
            for pack, bundle in installed.items()
        }
        deliveries = {
            pack: {
                "version": bundle.version,
                "sha256": bundle.sha256,
                "source": bundle.source,
                "status": "ready",
            }
            for pack, bundle in installed.items()
        }
        return PreparedModels(
            results=results,
            runtime_mounts=runtime_mounts,
            deliveries=deliveries,
        )
