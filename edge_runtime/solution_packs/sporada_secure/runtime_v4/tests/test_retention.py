from __future__ import annotations

import os
import time

from traffic_pilot_runtime.retention import SnapshotRetention


def test_snapshot_retention_removes_expired_files(tmp_path):
    old = tmp_path / "old.jpg"
    current = tmp_path / "current.jpg"
    old.write_bytes(b"old")
    current.write_bytes(b"new")
    now = time.time()
    os.utime(old, (now - 100, now - 100))

    SnapshotRetention(tmp_path, high_bytes=100, low_bytes=50,
                      maximum_age_seconds=60).enforce(now)

    assert not old.exists()
    assert current.exists()


def test_snapshot_retention_reduces_high_watermark_to_low(tmp_path):
    for index in range(4):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(bytes(10))
        os.utime(path, (100 + index, 100 + index))

    SnapshotRetention(tmp_path, high_bytes=30, low_bytes=20,
                      maximum_age_seconds=10_000).enforce(now=1000)

    assert sum(path.stat().st_size for path in tmp_path.glob("*.jpg")) <= 20
