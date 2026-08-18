"""Container health probe.

Invoked by `HEALTHCHECK` in Dockerfile.intel-axelera. Exits non-zero when
the worker hasn't ticked frames in the last HEALTH_MAX_STALE_SECS seconds.

We use a sentinel file at /tmp/worker_heartbeat that main.py touches every
pipeline tick. No frame for 30 s is the default failure threshold (12 FPS *
2 cameras = ~24 frames/s expected).
"""
from __future__ import annotations

import os
import sys
import time

HEARTBEAT = os.getenv("HEALTH_HEARTBEAT_FILE", "/tmp/worker_heartbeat")
MAX_STALE = float(os.getenv("HEALTH_MAX_STALE_SECS", "30"))


def main() -> int:
    if not os.path.exists(HEARTBEAT):
        sys.stderr.write(f"heartbeat file missing: {HEARTBEAT}\n")
        return 1
    age = time.time() - os.path.getmtime(HEARTBEAT)
    if age > MAX_STALE:
        sys.stderr.write(f"heartbeat stale: {age:.1f}s > {MAX_STALE}s\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
