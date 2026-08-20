"""Validation for models baked into an ApexFabric solution image."""
from __future__ import annotations

import hashlib
from pathlib import Path

from edge_runtime.model_registry.registry import ModelRegistry


class BakedModelValidator:
    def __init__(self, registry: ModelRegistry, models_root: Path) -> None:
        self._registry = registry
        self._models_root = models_root

    def validate(self, solution_pack: str) -> None:
        bundles = self._registry.for_solution_pack(solution_pack)
        if not bundles:
            raise ValueError(f"no model metadata registered for {solution_pack}")
        for bundle in bundles:
            for model_file in bundle.files:
                path = self._models_root / solution_pack / model_file.path
                if not path.is_file():
                    raise ValueError(
                        f"required baked model is missing: {solution_pack}/{model_file.path}"
                    )
                digest = _sha256(path)
                if digest != model_file.sha256:
                    raise ValueError(
                        f"baked model checksum mismatch: {solution_pack}/{model_file.path}"
                    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
