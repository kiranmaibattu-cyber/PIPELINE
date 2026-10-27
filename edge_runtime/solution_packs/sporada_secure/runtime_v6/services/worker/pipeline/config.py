import json
import os
from dataclasses import dataclass

from .common import positive_int

DEFAULT_WORKER_CONFIG_PATH = "/app/config/worker.json"


@dataclass(frozen=True)
class ModelConfig:
    vehicle_model_path: str
    vehicle_classes: str | list[str] | list[int] | None
    vehicle_confidence: float | dict[str, float]
    vehicle_tensorrt: dict
    vehicle_batch_size: int | str | None
    vehicle_processing_interval: int
    vehicle_tracker: dict
    vehicle_backend: str | None
    plate_model_path: str
    plate_confidence: float
    plate_tensorrt: dict
    plate_batch_size: int | None
    plate_backend: str | None
    lp_ocr_model_path: str
    lp_ocr_stable_char_ratio: float
    lp_ocr_batch_size: int | None
    ocr_backend: str | None


def load_worker_config(path=None):
    config_path = path or os.getenv("WORKER_CONFIG_PATH", DEFAULT_WORKER_CONFIG_PATH)
    with open(config_path) as config_file:
        return json.load(config_file)


def model_config_from_worker_config(config):
    models = (config or {}).get("models") or {}
    vehicle = models.get("vehicle") or {}
    plate = models.get("plate") or {}
    ocr = models.get("license_plate_ocr") or {}
    return ModelConfig(
        vehicle_model_path=vehicle.get("path")
        or "/weights/yolo26n.pt",
        vehicle_classes=vehicle.get("classes"),
        vehicle_confidence=vehicle.get("confidence", 0.25),
        vehicle_tensorrt=model_tensorrt_config(vehicle),
        vehicle_batch_size=vehicle.get("batch_size", "streams"),
        vehicle_processing_interval=max(0, int(vehicle.get("processing_interval", 0))),
        vehicle_tracker=tracker_config(vehicle.get("tracker") or {}),
        vehicle_backend=vehicle.get("backend"),
        plate_model_path=plate.get("path")
        or "/weights/yolo26n_plate_detection_224.pt",
        plate_confidence=float(plate.get("confidence", 0.25)),
        plate_tensorrt=model_tensorrt_config(plate),
        plate_batch_size=positive_int(
            plate.get("batch_size", plate.get("max_batch_size")),
        ),
        plate_backend=plate.get("backend"),
        lp_ocr_model_path=ocr.get("path") or "/weights/cct.onnx",
        lp_ocr_stable_char_ratio=float(ocr.get("stable_char_ratio", 0.75)),
        lp_ocr_batch_size=positive_int(
            ocr.get("batch_size", ocr.get("max_batch_size")),
        ),
        ocr_backend=ocr.get("backend"),
    )


def model_tensorrt_config(model):
    tensorrt = model.get("tensorrt", False)
    if isinstance(tensorrt, bool):
        return {
            "enabled": tensorrt,
            "dynamic": True,
            "half": True,
            "device": 0,
            "batch": positive_int(model.get("batch_size")) or 64,
        }

    config = dict(tensorrt or {})
    config.setdefault("enabled", True)
    config.setdefault("dynamic", True)
    config.setdefault("half", True)
    config.setdefault("device", 0)
    config.setdefault("batch", positive_int(model.get("batch_size")) or 64)
    return config


def tracker_config(config):
    resolved = dict(config or {})
    resolved.setdefault("max_distance", 320.0)
    resolved.setdefault("max_disappeared", 45)
    resolved.setdefault("bbox_smoothing", 0.25)
    resolved.setdefault("min_iou", 0.01)
    resolved.setdefault("class_aware", False)
    resolved.setdefault("class_switch_cost", 0.15)
    resolved.setdefault("draw_predictions", True)
    return resolved


def confidence_floor(value, fallback=0.25):
    if isinstance(value, dict):
        numbers = [float(item) for item in value.values()]
        return min(numbers) if numbers else fallback
    return float(value)
