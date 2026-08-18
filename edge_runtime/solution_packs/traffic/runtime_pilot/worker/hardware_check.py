"""Hardware topology check.

Runs once at worker startup. Logs which device decode and inference end up
on, and aborts (when STRICT_HARDWARE=1) if a required device node is
missing. Operators read this block in `docker compose logs worker | head`
to confirm the iGPU and NPU are visible.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

INTEL_RENDER_NODE = os.getenv("INTEL_RENDER_NODE", "/dev/dri/renderD128")
# The Axelera Metis exposes a versioned node (e.g. /dev/metis-0:1:0), not a flat
# /dev/metis0 — probe the glob so detection works on the real box.
NPU_DEVICE_GLOB = os.getenv("NPU_DEVICE_GLOB", "/dev/metis-*")


def _axelera_node() -> str:
    matches = sorted(glob.glob(NPU_DEVICE_GLOB))
    return matches[0] if matches else NPU_DEVICE_GLOB


def _active_backends() -> List[str]:
    """The per-stage backends actually in use (set by the UI backend selector via
    backend_mode), falling back to the legacy single DETECTOR_BACKEND."""
    legacy = (os.getenv("DETECTOR_BACKEND") or "ultralytics").lower()
    return [
        (os.getenv(var) or legacy).lower()
        for var in ("VEHICLE_BACKEND", "PLATE_BACKEND", "OCR_BACKEND")
    ]


@dataclass(frozen=True)
class Topology:
    decoder_backend: str
    decode_available: bool
    detector_backend: str
    npu_available: bool
    npu_device: str
    models_resident: List[str]

    @property
    def decode_device(self) -> str:
        return INTEL_RENDER_NODE

    @property
    def ok(self) -> bool:
        if self.detector_backend == "axelera" and not self.npu_available:
            return False
        return True


def detect_topology() -> Topology:
    backends = _active_backends()
    uses_axelera = "axelera" in backends
    uses_openvino = "openvino" in backends
    # Name the inference engine that's actually carrying the stages, so the
    # Overview "inference" tile is truthful per the selected backend mode.
    if uses_axelera:
        detector_backend = "axelera"
        device = _axelera_node()
        available = bool(glob.glob(NPU_DEVICE_GLOB))
    elif uses_openvino:
        detector_backend = "openvino"
        device = INTEL_RENDER_NODE  # Arc 140T iGPU (GPU.0)
        available = os.path.exists(INTEL_RENDER_NODE)
    else:
        detector_backend = backends[0] if backends else "ultralytics"
        device = _axelera_node()
        available = bool(glob.glob(NPU_DEVICE_GLOB))
    # Stages on the named device: vehicle/plate/ocr that match its backend.
    stage_names = ["vehicle", "license_plate", "ocr"]
    models_resident = [
        name for name, backend in zip(stage_names, backends)
        if backend == detector_backend
    ]
    return Topology(
        decoder_backend=(os.getenv("DECODER_BACKEND") or "gstreamer").lower(),
        decode_available=os.path.exists(INTEL_RENDER_NODE),
        detector_backend=detector_backend,
        npu_available=available,
        npu_device=device,
        models_resident=models_resident,
    )


def log_topology(strict: bool | None = None) -> Topology:
    if strict is None:
        strict = os.getenv("STRICT_HARDWARE", "0").strip() in {"1", "true", "yes"}

    topology = detect_topology()

    decode_status = "[OK] available" if topology.decode_available else "[!]  MISSING"
    npu_status = "[OK] available" if topology.npu_available else "[!]  MISSING"
    decode_label = "GStreamer/QSV" if topology.decoder_backend == "gstreamer" else "PyAV/FFmpeg"
    npu_label = "Axelera (Voyager)" if topology.detector_backend == "axelera" else "Ultralytics (CPU/GPU)"

    block = [
        "+-- Hardware topology -------------------------------------+",
        f"| Decode    : {decode_label:<22} {topology.decode_device:<20} {decode_status} |",
        f"| Inference : {npu_label:<22} {topology.npu_device:<20} {npu_status} |",
        f"| Backend   : DECODER_BACKEND={topology.decoder_backend}, "
        f"DETECTOR_BACKEND={topology.detector_backend}".ljust(58) + " |",
    ]
    if topology.models_resident:
        block.append(
            "| Models    : "
            + ", ".join(topology.models_resident).ljust(45)
            + " |"
        )
    block.append("+----------------------------------------------------------+")
    for line in block:
        logger.info(line)

    if topology.detector_backend == "axelera" and not topology.npu_available:
        message = (
            f"DETECTOR_BACKEND=axelera but {topology.npu_device} is missing. "
            "Verify the Axelera kernel module is loaded on the host and "
            "the device is bind-mounted into the container."
        )
        if strict:
            raise SystemExit(message)
        logger.warning(message)

    if topology.decoder_backend == "gstreamer" and not topology.decode_available:
        logger.warning(
            "DECODER_BACKEND=gstreamer but %s is missing. Decode will fall "
            "back to CPU (libav). Mount /dev/dri/renderD128 into the worker "
            "container to use the Intel iGPU.",
            topology.decode_device,
        )

    if topology.decoder_backend == "gstreamer" and shutil.which("gst-inspect-1.0"):
        # Cheap sanity check: log whether the QSV decoder element is present.
        import subprocess
        for element in ("qsvh264dec", "vaapih264dec"):
            try:
                subprocess.run(
                    ["gst-inspect-1.0", element],
                    check=True,
                    capture_output=True,
                    timeout=5,
                )
                logger.info("GStreamer element available: %s", element)
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                logger.warning("GStreamer element missing: %s", element)

    return topology
