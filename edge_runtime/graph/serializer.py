"""Serialize compiled graph dataclasses to JSON-compatible dictionaries."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
import json


class GraphPlanWriter:
    def write(self, compiled_graph, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        data = _to_data(compiled_graph)
        (output_dir / "compiled_graph.json").write_text(
            json.dumps(data, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (output_dir / "management_api_tags.json").write_text(
            json.dumps(_api_contract(data), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        for plan in compiled_graph.solution_plans:
            path = output_dir / f"{plan.solution_pack}.runtime_plan.json"
            path.write_text(json.dumps(_to_data(plan), indent=2, sort_keys=True), encoding="utf-8")


def _to_data(value):
    if is_dataclass(value):
        return {k: _to_data(v) for k, v in asdict(value).items()}
    if isinstance(value, tuple):
        return [_to_data(v) for v in value]
    if isinstance(value, list):
        return [_to_data(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_data(v) for k, v in value.items()}
    return value


def _api_contract(compiled_graph: dict) -> dict:
    solution_packs = {}
    for plan in compiled_graph.get("solution_plans") or []:
        cameras = {}
        services = {}
        for camera in plan.get("cameras") or []:
            cameras[camera["camera_id"]] = camera.get("api_tags") or {}
        for service in plan.get("shared_services") or []:
            services[service["name"]] = service.get("api_tags") or {}
        solution_packs[plan["solution_pack"]] = {
            "solution": plan.get("api_tags") or {},
            "cameras": cameras,
            "services": services,
        }
    return {
        "api_version": "edge-api/v1",
        "edge_id": compiled_graph["edge_id"],
        "revision": compiled_graph["revision"],
        "solution_packs": solution_packs,
    }
