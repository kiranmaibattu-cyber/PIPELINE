"""Load solution-pack app manifests from YAML files."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import yaml

from edge_runtime.graph.models import AppManifest


class ManifestRepository:
    """Read-only manifest repository.

    The graph builder depends on this abstraction, not on file paths, so a future
    management server or database-backed app registry can replace it.
    """

    def __init__(self, manifests: Iterable[AppManifest]) -> None:
        self._by_key = {(m.solution_pack, m.app_id): m for m in manifests}

    @classmethod
    def from_directory(cls, root: Path) -> "ManifestRepository":
        manifests = []
        for path in sorted(root.glob("*/manifests/*.yaml")):
            manifests.append(_load_manifest(path))
        return cls(manifests)

    def get(self, solution_pack: str, app_id: str) -> AppManifest:
        key = (solution_pack, app_id)
        if key not in self._by_key:
            raise KeyError(f"unknown app {solution_pack}/{app_id}")
        return self._by_key[key]


def _as_tuple(data: dict, section: str, name: str) -> tuple[str, ...]:
    value = ((data.get(section) or {}).get(name) or [])
    return tuple(str(v) for v in value)


def _load_manifest(path: Path) -> AppManifest:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    required = data.get("requires") or {}
    optional = data.get("optional") or {}
    produced = data.get("produces") or {}
    hardware = data.get("hardware") or {}
    return AppManifest(
        app_id=str(data["app_id"]),
        solution_pack=str(data["solution_pack"]),
        version=str(data.get("version", "0.0.0")),
        required_data=tuple(str(v) for v in required.get("data") or ()),
        required_services=tuple(str(v) for v in required.get("services") or ()),
        optional_data=tuple(str(v) for v in optional.get("data") or ()),
        models=tuple(str(v) for v in data.get("models") or ()),
        state=tuple(str(v) for v in data.get("state") or ()),
        produced_data=tuple(str(v) for v in produced.get("data") or ()),
        produced_events=tuple(str(v) for v in produced.get("events") or ()),
        preferred_hardware={str(k): str(v) for k, v in (hardware.get("preferred") or {}).items()},
        policy=dict(data.get("policy") or {}),
    )
