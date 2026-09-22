"""Backend selection helpers.

The worker pipeline never imports backends directly. It calls the factory
functions `VehicleDetector`, `PlateDetector`, and `LicensePlateOCRONNX`
(see services/worker/detectors/__init__.py), which dispatch to the
hardware-specific backend chosen *per stage*.

Backends:
    ultralytics    (default; PyTorch + Ultralytics + ONNXRuntime)
    axelera        (Axelera Voyager SDK runtime — Metis AIPU)
    openvino       (OpenVINO — Intel Arc iGPU / NPU / CPU)

The hybrid Edge-Box deployment runs different stages on different engines
(e.g. vehicle=axelera on the AIPU, plate+ocr=openvino on the Arc iGPU), so the
backend is resolved *per stage* rather than from one global env var. Precedence
for `resolve_backend(stage, explicit)`:

    1. per-stage env override   {STAGE}_BACKEND   (VEHICLE_BACKEND, PLATE_BACKEND, OCR_BACKEND)
    2. explicit value           (from config/worker.json `models.<m>.backend`)
    3. global env               DETECTOR_BACKEND
    4. default                  ultralytics

The legacy global `detector_backend()` is kept for backward compatibility.
"""
from __future__ import annotations

import os
from typing import Optional

ULTRALYTICS = "ultralytics"
AXELERA = "axelera"
OPENVINO = "openvino"
_VALID = {ULTRALYTICS, AXELERA, OPENVINO, "onnxruntime"}


def _normalize(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "onnxruntime":
        return ULTRALYTICS
    if normalized not in _VALID:
        raise ValueError(
            f"backend={value!r} is not supported. "
            f"Pick one of: {sorted(_VALID - {'onnxruntime'})}"
        )
    return normalized


def detector_backend() -> str:
    """Legacy global backend (DETECTOR_BACKEND env, default ultralytics)."""
    return _normalize(os.getenv("DETECTOR_BACKEND") or ULTRALYTICS)


def resolve_backend(stage: str, explicit: Optional[str] = None) -> str:
    """Resolve the backend for a single stage ('vehicle', 'plate', 'ocr').

    See module docstring for precedence.
    """
    candidates = (
        os.getenv(f"{stage.upper()}_BACKEND"),
        explicit,
        os.getenv("DETECTOR_BACKEND"),
    )
    for candidate in candidates:
        if candidate:
            return _normalize(candidate)
    return ULTRALYTICS


def axelera_model_path(model_name: str) -> str:
    """Resolve the compiled Voyager artifact for a model.

    The Voyager runtime loads a compiled model directory (containing
    `model.json` + weights), not the legacy single-file `.axl`. We accept
    either: if `AXELERA_MODELS_DIR/<name>` is a directory it is used as-is,
    otherwise we fall back to `AXELERA_MODELS_DIR/<name>.axl`.
    """
    base = os.getenv("AXELERA_MODELS_DIR", "/models/axelera")
    candidate_dir = os.path.join(base, model_name)
    if os.path.isdir(candidate_dir):
        return candidate_dir
    return os.path.join(base, f"{model_name}.axl")


def openvino_model_path(model_name: str) -> str:
    """Resolve the OpenVINO IR (.xml) for a model under OPENVINO_MODELS_DIR."""
    base = os.getenv("OPENVINO_MODELS_DIR", "/models/openvino")
    return os.path.join(base, f"{model_name}.xml")
