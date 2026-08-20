"""Launch the copied Traffic Pilot OpenVINO worker from a compiled traffic graph plan."""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any

from edge_runtime.runtime.plan_loader import RuntimePlanLoader
from edge_runtime.solution_packs.traffic.runtime.config_adapter import TrafficConfigAdapter


RUNTIME_ROOT = Path(__file__).resolve().parent
WORKER_ROOT = RUNTIME_ROOT / "worker"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and launch Traffic Pilot OpenVINO worker")
    parser.add_argument("--plan", default="/plans/traffic.runtime_plan.json")
    parser.add_argument("--generated-dir", default="/generated/traffic")
    parser.add_argument("--state-dir", default="/state/traffic")
    parser.add_argument("--models-dir", default="/models/traffic/openvino")
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_dir = Path(args.generated_dir)
    state_dir = Path(args.state_dir)
    models_dir = Path(args.models_dir)

    plan = RuntimePlanLoader().load(Path(args.plan))
    TrafficConfigAdapter().write(plan, generated_dir)
    worker_config = _write_worker_config(plan, generated_dir)
    _configure_environment(plan, generated_dir, state_dir, models_dir, worker_config)

    print(f"traffic OpenVINO config prepared in {generated_dir.resolve()}", flush=True)
    if args.prepare_only:
        return 0

    sys.path.insert(0, str(WORKER_ROOT))
    os.chdir(WORKER_ROOT)
    runpy.run_path(str(WORKER_ROOT / "stream_fleet_openvino.py"), run_name="__main__")
    return 0


def _write_worker_config(plan: dict[str, Any], generated_dir: Path) -> Path:
    generated_dir.mkdir(parents=True, exist_ok=True)
    path = generated_dir / "worker.generated.json"
    config = {
        "models": {
            "vehicle": {
                "backend": "openvino",
                "batch_size": "streams",
                "processing_interval": 0,
                "tracker": {
                    "max_distance": 320,
                    "max_disappeared": 45,
                    "bbox_smoothing": 0.25,
                    "min_iou": 0.01,
                    "class_aware": False,
                    "class_switch_cost": 0.15,
                    "draw_predictions": True,
                },
                "classes": ["pedestrian", "bicycle", "car", "motorcycle", "bus", "truck"],
                "confidence": {
                    "pedestrian": 0.25,
                    "bicycle": 0.25,
                    "car": 0.25,
                    "motorcycle": 0.25,
                    "bus": 0.25,
                    "truck": 0.25,
                },
            },
            "plate": {"backend": "openvino", "confidence": 0.25, "batch_size": 64},
            "license_plate_ocr": {
                "backend": "openvino",
                "stable_char_ratio": 0.75,
                "batch_size": 128,
            },
        },
        "visualization": {"text_renderer": "pillow"},
        "json_streaming": {
            "enabled": True,
            "debug_tap": False,
            "queue_size": 1000,
            "outputs": [],
        },
    }
    path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _configure_environment(
    plan: dict[str, Any],
    generated_dir: Path,
    state_dir: Path,
    models_dir: Path,
    worker_config: Path,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    placements = {service["name"]: service["device"] for service in plan.get("shared_services") or []}

    os.environ.setdefault("WORKER_MODE", "all_openvino")
    os.environ.setdefault("DETECTOR_BACKEND", "openvino")
    os.environ.setdefault("VEHICLE_BACKEND", "openvino")
    os.environ.setdefault("PLATE_BACKEND", "openvino")
    os.environ.setdefault("OCR_BACKEND", "openvino")
    os.environ.setdefault("OPENVINO_MODELS_DIR", str(models_dir))
    os.environ.setdefault("CAMERAS_FILE", str(generated_dir / "cameras.generated.json"))
    os.environ.setdefault("WORKER_CONFIG_PATH", str(worker_config))
    os.environ.setdefault("STATE_DIR", str(state_dir))
    os.environ.setdefault("MANAGEMENT_STATE_DIR", str(state_dir))
    os.environ.setdefault("VIDEO_DIR", str(state_dir / "videos"))
    os.environ.setdefault("INFER_FPS", _infer_fps(plan))

    os.environ.setdefault("VEHICLE_DEVICE", placements.get("vehicle_detector", "GPU"))
    os.environ.setdefault("PLATE_DEVICE", placements.get("plate_detector", "NPU"))
    os.environ.setdefault("OCR_DEVICE", _ocr_device(placements.get("ocr_service", "GPU")))
    os.environ.setdefault("DISABLE_DEBUG_TAP", "1")
    os.environ.setdefault("DISABLE_REDIS", "1")


def _infer_fps(plan: dict[str, Any]) -> str:
    fps_values = [float(camera.get("fps") or 0) for camera in plan.get("cameras") or []]
    return str(max(1, int(min(fps_values or [8]))))


def _ocr_device(device: str) -> str:
    device = str(device or "GPU").upper()
    if device == "GPU":
        return "MULTI:GPU,NPU"
    return device


if __name__ == "__main__":
    raise SystemExit(main())
