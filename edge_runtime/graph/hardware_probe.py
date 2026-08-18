"""Local hardware discovery for an edge box."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from edge_runtime.graph.models import HardwareProfile


class HardwareProbe:
    """Detects runtime capabilities without assuming identical edge boxes."""

    def probe(self, edge_id: str) -> HardwareProfile:
        devices = ["CPU"]
        runtimes = []
        if Path("/dev/dri").exists():
            devices.append("GPU")
            runtimes.append("vaapi_decode")
        if Path("/dev/accel").exists() or self._openvino_has("NPU"):
            devices.append("NPU")
        if shutil.which("docker"):
            runtimes.append("docker")
        if self._openvino_has("GPU") or self._openvino_has("NPU"):
            runtimes.append("openvino")
        return HardwareProfile(
            edge_id=edge_id,
            cpu_cores=os.cpu_count() or 1,
            ram_gb=self._ram_gb(),
            devices=tuple(dict.fromkeys(devices)),
            runtimes=tuple(dict.fromkeys(runtimes)),
        )

    @staticmethod
    def _ram_gb() -> float:
        try:
            text = Path("/proc/meminfo").read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 2)
        except OSError:
            pass
        return 0.0

    @staticmethod
    def _openvino_has(device: str) -> bool:
        try:
            import openvino as ov

            return device.upper() in {d.split(".")[0].upper() for d in ov.Core().available_devices}
        except Exception:
            return False
