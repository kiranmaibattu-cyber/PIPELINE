"""Typed graph contracts used by the edge compiler."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AppManifest:
    app_id: str
    solution_pack: str
    version: str
    required_data: tuple[str, ...] = ()
    required_services: tuple[str, ...] = ()
    optional_data: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    produced_data: tuple[str, ...] = ()
    produced_events: tuple[str, ...] = ()
    preferred_hardware: dict[str, str] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CameraDesiredState:
    camera_id: str
    source: str
    solution_pack: str
    apps: tuple[str, ...]
    fps: float = 10.0
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DesiredState:
    edge_id: str
    revision: int
    cameras: tuple[CameraDesiredState, ...]
    management_url: str | None = None


@dataclass(frozen=True)
class HardwareProfile:
    edge_id: str
    cpu_cores: int
    ram_gb: float
    devices: tuple[str, ...]
    runtimes: tuple[str, ...]

    def has_device(self, device: str) -> bool:
        return device.upper() in {d.upper() for d in self.devices}


@dataclass(frozen=True)
class CameraGraph:
    camera_id: str
    source: str
    solution_pack: str
    apps: tuple[str, ...]
    fps: float
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    required_data: tuple[str, ...]
    required_services: tuple[str, ...]
    required_models: tuple[str, ...]
    required_state: tuple[str, ...]
    plugins: tuple[str, ...]
    feature_flags: dict[str, bool]
    api_tags: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SharedService:
    name: str
    solution_pack: str
    device: str
    models: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    api_tags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SolutionRuntimePlan:
    edge_id: str
    revision: int
    solution_pack: str
    cameras: tuple[CameraGraph, ...]
    shared_services: tuple[SharedService, ...]
    status: str
    warnings: tuple[str, ...] = ()
    api_tags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompiledGraph:
    edge_id: str
    revision: int
    hardware: HardwareProfile
    solution_plans: tuple[SolutionRuntimePlan, ...]
