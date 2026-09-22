from __future__ import annotations

from pathlib import Path
import threading
import time


class SnapshotRetention(threading.Thread):
    def __init__(
        self,
        root: Path,
        high_bytes: int = 8 * 1024**3,
        low_bytes: int = 6 * 1024**3,
        maximum_age_seconds: int = 86400,
        scan_seconds: int = 60,
    ) -> None:
        super().__init__(name="snapshot-retention", daemon=True)
        if not 0 <= low_bytes <= high_bytes:
            raise ValueError("snapshot retention requires 0 <= low_bytes <= high_bytes")
        self.root = root
        self.high_bytes = high_bytes
        self.low_bytes = low_bytes
        self.maximum_age_seconds = maximum_age_seconds
        self.scan_seconds = scan_seconds
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.enforce()
            self.stop_event.wait(self.scan_seconds)

    def enforce(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        files = []
        for path in self.root.rglob("*") if self.root.exists() else ():
            try:
                if path.is_file():
                    stat = path.stat()
                    files.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
        cutoff = now - self.maximum_age_seconds
        for modified, _, path in list(files):
            if modified < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass
        files = []
        total = 0
        for path in self.root.rglob("*") if self.root.exists() else ():
            try:
                if path.is_file():
                    stat = path.stat()
                    files.append((stat.st_mtime, stat.st_size, path))
                    total += stat.st_size
            except OSError:
                continue
        if total <= self.high_bytes:
            return
        for _, size, path in sorted(files):
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
            if total <= self.low_bytes:
                break
