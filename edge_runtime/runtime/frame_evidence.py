"""Bounded immutable JPEG evidence shared by camera workers and event consumers."""
from collections import deque
import hashlib
import os
from pathlib import Path
import tempfile
import uuid


class FrameEvidenceCache:
    def __init__(self, max_frames=64, max_bytes=64 * 1024 * 1024):
        self.session = uuid.uuid4().hex
        root = os.getenv("FRAME_EVIDENCE_ROOT")
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="frame-evidence-", dir=root)
        self.entries = deque()
        self.size = 0
        self.max_frames = max_frames
        self.max_bytes = max_bytes

    def put(self, jpeg, camera, frame_id, captured_at, dimensions):
        digest = hashlib.sha256(jpeg).hexdigest()
        path = Path(self.directory.name) / f"{frame_id}.jpg"
        path.write_bytes(jpeg)
        self.entries.append((path, len(jpeg)))
        self.size += len(jpeg)
        while len(self.entries) > self.max_frames or self.size > self.max_bytes:
            old, size = self.entries.popleft()
            old.unlink(missing_ok=True)
            self.size -= size
        return dict(stream_session_id=self.session, camera_id=camera,
                    frame_id=frame_id, captured_at=captured_at,
                    frame_wh=dimensions, path=str(path), sha256=digest)

    def close(self):
        self.directory.cleanup()


def read_evidence(evidence):
    try:
        jpeg = Path(evidence["path"]).read_bytes()
        return jpeg if hashlib.sha256(jpeg).hexdigest() == evidence["sha256"] else None
    except (OSError, KeyError, TypeError):
        return None
