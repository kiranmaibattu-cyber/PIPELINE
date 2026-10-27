"""Hardware topology check.

Runs once at worker startup. Logs which device decode and inference end up
on, and aborts (when STRICT_HARDWARE=1) if a required device node is
missing. Operators read this block in `docker compose logs worker | head`
to confirm the iGPU and NPU are visible.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

INTEL_RENDER_NODE = os.getenv("INTEL_RENDER_NODE", "/dev/dri/renderD128")


def _openvino_devices() -> List[str]:
    try:
        import openvino as ov

        return list(ov.Core().available_devices)
    except Exception:  # noqa: BLE001
        return []


@dataclass(frozen=True)
class Topology:
    decoder_backend: str
    decode_available: bool
    detector_backend: str
    inference_available: bool
    inference_devices: str
    models_resident: List[str]

    @property
    def decode_device(self) -> str:
        return INTEL_RENDER_NODE

    @property
    def ok(self) -> bool:
        return self.decode_available and self.inference_available


def detect_topology() -> Topology:
    devices = _openvino_devices()
    return Topology(
        decoder_backend="vaapi",
        decode_available=os.path.exists(INTEL_RENDER_NODE),
        detector_backend="openvino",
        inference_available=bool(devices),
        inference_devices=",".join(devices) if devices else "unavailable",
        models_resident=["vehicle", "license_plate", "ocr"],
    )


def log_topology(strict: bool | None = None) -> Topology:
    if strict is None:
        strict = os.getenv("STRICT_HARDWARE", "0").strip() in {"1", "true", "yes"}

    topology = detect_topology()

    decode_status = "[OK] available" if topology.decode_available else "[!]  MISSING"
    inference_status = "[OK] available" if topology.inference_available else "[!]  MISSING"
    decode_label = "FFmpeg/VAAPI"
    inference_label = "OpenVINO"

    block = [
        "+-- Hardware topology -------------------------------------+",
        f"| Decode    : {decode_label:<22} {topology.decode_device:<20} {decode_status} |",
        f"| Inference : {inference_label:<22} {topology.inference_devices:<20} {inference_status} |",
        f"| Backend   : decode={topology.decoder_backend}, "
        f"inference={topology.detector_backend}".ljust(58) + " |",
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

    if not topology.inference_available:
        message = (
            "OpenVINO did not report any inference devices. Verify the Intel GPU/NPU "
            "runtime is installed and accelerator devices are mounted into the container."
        )
        if strict:
            raise SystemExit(message)
        logger.warning(message)

    if not topology.decode_available:
        logger.warning(
            "%s is missing. Hardware decode will not be available. Mount "
            "/dev/dri/renderD128 into the worker container to use the Intel iGPU.",
            topology.decode_device,
        )

    if shutil.which("gst-inspect-1.0"):
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
