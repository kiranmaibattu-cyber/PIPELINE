"""OpenVINO model path helpers for the Sentinel V5.2 image."""
from __future__ import annotations

import os

OPENVINO = "openvino"


def openvino_model_path(model_name: str) -> str:
    """Resolve the OpenVINO IR (.xml) for a model under OPENVINO_MODELS_DIR."""
    base = os.getenv("OPENVINO_MODELS_DIR", "/models/openvino")
    return os.path.join(base, f"{model_name}.xml")
