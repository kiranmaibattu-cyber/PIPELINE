"""Container and accelerator resource counters with explicit measurement scope."""
from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_number(path: Path) -> float | None:
    value = _read_text(path)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class CgroupSampler:
    """Read this container's cgroup-v2 CPU and memory accounting."""

    def __init__(self, root: str | Path | None = None) -> None:
        base = Path(root or os.getenv("SENTINEL_CGROUP_ROOT", "/sys/fs/cgroup"))
        self.root = self._process_cgroup(base)
        self._last_usage_usec: float | None = None
        self._last_mono: float | None = None
        self._prime()

    @staticmethod
    def _process_cgroup(base: Path) -> Path:
        proc_path = Path(os.getenv("SENTINEL_PROC_SELF_CGROUP", "/proc/self/cgroup"))
        value = _read_text(proc_path)
        if value:
            for line in value.splitlines():
                prefix, separator, relative = line.partition("::")
                if separator and prefix == "0":
                    candidate = base / relative.lstrip("/")
                    if candidate.is_dir():
                        return candidate
        return base

    def _cpu_usage_usec(self) -> float | None:
        value = _read_text(self.root / "cpu.stat")
        if value is None:
            return None
        for line in value.splitlines():
            key, _, raw = line.partition(" ")
            if key == "usage_usec":
                try:
                    return float(raw)
                except ValueError:
                    return None
        return None

    def _prime(self) -> None:
        self._last_usage_usec = self._cpu_usage_usec()
        self._last_mono = time.monotonic()

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        usage_usec = self._cpu_usage_usec()
        elapsed = None if self._last_mono is None else max(1e-6, now - self._last_mono)
        cpu_cores = None
        if usage_usec is not None and self._last_usage_usec is not None and elapsed is not None:
            cpu_cores = max(0.0, usage_usec - self._last_usage_usec) / (elapsed * 1_000_000.0)
        self._last_usage_usec = usage_usec
        self._last_mono = now

        cpu_max = _read_text(self.root / "cpu.max")
        cpu_limit_cores = None
        if cpu_max:
            quota, _, period = cpu_max.partition(" ")
            if quota != "max":
                try:
                    cpu_limit_cores = float(quota) / float(period)
                except (TypeError, ValueError, ZeroDivisionError):
                    cpu_limit_cores = None
        host_cores = max(1, int(os.cpu_count() or 1))
        capacity_cores = cpu_limit_cores or float(host_cores)

        memory_current = _read_number(self.root / "memory.current")
        memory_max_raw = _read_text(self.root / "memory.max")
        try:
            memory_limit = None if memory_max_raw in (None, "max") else float(memory_max_raw)
        except ValueError:
            memory_limit = None
        memory_pct = None
        if memory_current is not None and memory_limit not in (None, 0):
            memory_pct = 100.0 * memory_current / memory_limit

        pids = _read_number(self.root / "pids.current")
        available = usage_usec is not None or memory_current is not None
        return {
            "scope": "container_cgroup",
            "measurement_status": "measured" if available else "counter_unavailable",
            "cpu_cores_used": round(cpu_cores, 3) if cpu_cores is not None else None,
            "cpu_capacity_cores": round(capacity_cores, 3),
            "cpu_limit_cores": round(cpu_limit_cores, 3) if cpu_limit_cores is not None else None,
            "cpu_capacity_utilization_pct": (
                round(100.0 * cpu_cores / capacity_cores, 2)
                if cpu_cores is not None and capacity_cores > 0 else None
            ),
            "memory_current_mb": (
                round(memory_current / 1024 / 1024, 2) if memory_current is not None else None
            ),
            "memory_limit_mb": (
                round(memory_limit / 1024 / 1024, 2) if memory_limit is not None else None
            ),
            "memory_limit_utilization_pct": round(memory_pct, 2) if memory_pct is not None else None,
            "pids_current": int(pids) if pids is not None else None,
        }


class AcceleratorSampler:
    """Read node-device counters without estimating missing utilization."""

    def __init__(self) -> None:
        self.npu_busy_path = Path(os.getenv(
            "SENTINEL_NPU_BUSY_TIME_PATH",
            "/sys/class/accel/accel0/device/npu_busy_time_us",
        ))
        self.npu_memory_path = Path(os.getenv(
            "SENTINEL_NPU_MEMORY_PATH",
            "/sys/class/accel/accel0/device/npu_memory_utilization",
        ))
        self.npu_frequency_path = Path(os.getenv(
            "SENTINEL_NPU_FREQUENCY_PATH",
            "/sys/class/accel/accel0/device/npu_current_frequency_mhz",
        ))
        self.npu_max_frequency_path = Path(os.getenv(
            "SENTINEL_NPU_MAX_FREQUENCY_PATH",
            "/sys/class/accel/accel0/device/npu_max_frequency_mhz",
        ))
        self.gpu_frequency_path = Path(os.getenv(
            "SENTINEL_GPU_FREQUENCY_PATH",
            "/sys/class/drm/card1/device/drm/card1/gt_act_freq_mhz",
        ))
        self.gpu_max_frequency_path = Path(os.getenv(
            "SENTINEL_GPU_MAX_FREQUENCY_PATH",
            "/sys/class/drm/card1/device/drm/card1/gt_max_freq_mhz",
        ))
        self.gpu_rc6_path = Path(os.getenv(
            "SENTINEL_GPU_RC6_RESIDENCY_PATH",
            "/sys/class/drm/card1/device/drm/card1/gt/gt1/rc6_residency_ms",
        ))
        self.gpu_rc6_enable_path = Path(os.getenv(
            "SENTINEL_GPU_RC6_ENABLE_PATH",
            "/sys/class/drm/card1/device/drm/card1/gt/gt1/rc6_enable",
        ))
        self._last_npu_busy = _read_number(self.npu_busy_path)
        self._last_gpu_rc6 = _read_number(self.gpu_rc6_path)
        self._last_mono = time.monotonic()

    def sample(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        elapsed = max(1e-6, now - self._last_mono)
        npu_busy = _read_number(self.npu_busy_path)
        npu_utilization = None
        if npu_busy is not None and self._last_npu_busy is not None:
            delta = max(0.0, npu_busy - self._last_npu_busy)
            npu_utilization = min(100.0, 100.0 * delta / (elapsed * 1_000_000.0))
        self._last_npu_busy = npu_busy
        self._last_mono = now

        npu_memory = _read_number(self.npu_memory_path)
        npu_frequency = _read_number(self.npu_frequency_path)
        npu_max_frequency = _read_number(self.npu_max_frequency_path)
        gpu_frequency = _read_number(self.gpu_frequency_path)
        gpu_max_frequency = _read_number(self.gpu_max_frequency_path)
        gpu_rc6 = _read_number(self.gpu_rc6_path)
        gpu_rc6_enabled = _read_number(self.gpu_rc6_enable_path)
        gpu_utilization = None
        if (
            gpu_rc6_enabled not in (None, 0)
            and gpu_rc6 is not None
            and self._last_gpu_rc6 is not None
            and gpu_rc6 >= self._last_gpu_rc6
        ):
            # RC6 is the Intel GT low-power idle state. Its cumulative residency
            # gives a real node-device active-residency measurement without
            # pretending that model wall time is hardware utilization.
            idle_ms = gpu_rc6 - self._last_gpu_rc6
            elapsed_ms = elapsed * 1000.0
            gpu_utilization = min(100.0, max(0.0, 100.0 * (1.0 - idle_ms / elapsed_ms)))
        self._last_gpu_rc6 = gpu_rc6
        return {
            "GPU": {
                "scope": "node_device",
                "hardware_utilization_pct": (
                    round(gpu_utilization, 2) if gpu_utilization is not None else None
                ),
                "hardware_measurement_status": (
                    "measured" if gpu_utilization is not None else "counter_unavailable"
                ),
                "utilization_counter": str(self.gpu_rc6_path) if gpu_utilization is not None else None,
                "utilization_semantics": (
                    "non_rc6_active_residency; node-wide GT activity, not per-process engine busy"
                    if gpu_utilization is not None else None
                ),
                "current_frequency_mhz": gpu_frequency,
                "max_frequency_mhz": gpu_max_frequency,
            },
            "NPU": {
                "scope": "node_device",
                "hardware_utilization_pct": (
                    round(npu_utilization, 2) if npu_utilization is not None else None
                ),
                "hardware_measurement_status": (
                    "measured" if npu_utilization is not None else "counter_unavailable"
                ),
                "busy_counter": str(self.npu_busy_path) if npu_busy is not None else None,
                "memory_used_bytes": int(npu_memory) if npu_memory is not None else None,
                "current_frequency_mhz": npu_frequency,
                "max_frequency_mhz": npu_max_frequency,
            },
        }
