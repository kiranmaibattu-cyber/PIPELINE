"""Run inside the surveillance image to verify the actual snapshot callback."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, "/opt/pipeline/edge_runtime/solution_packs/surveillance/runtime_8090")
from PLATF.app import App
from PLATF.core import Event, EventBus, PersonStore, TrackObservation
from PLATF.core.plugin import PluginContext
from edge_runtime.runtime.frame_evidence import FrameEvidenceCache


class ExactSnapshotTest(unittest.TestCase):
    def test_delayed_event_uses_observation_frame_and_bbox(self):
        cache = FrameEvidenceCache(max_frames=2)
        self.addCleanup(cache.close)
        with tempfile.TemporaryDirectory() as root:
            os.environ["MANAGEMENT_SNAPSHOT_DIR"] = str(Path(root) / "snapshots")
            frame = np.zeros((96, 128, 3), np.uint8)
            frame[10:70, 20:60] = [40, 180, 220]
            jpeg = cv2.imencode(".jpg", frame)[1].tobytes()
            source = cache.put(jpeg, "cam", 120, 1789010000.0, [128, 96])
            obs = TrackObservation("cam", 7, (20, 10, 60, 70), 1789010000.0,
                                   meta={"evidence": source})
            context = PluginContext(PersonStore(), EventBus())
            context.observation = obs
            event = Event("intrusion", obs.t, "cam")
            context.emit(event)
            cache.put(b"different later frame", "cam", 121, obs.t + 1, [128, 96])
            app = App.__new__(App)
            # No live-view or preview attributes: any access to them fails this test.
            row = {**event.as_dict(), "event_id": "test-event"}
            assets = app._save_management_snapshot("cam", "intrusion", row)
            saved = (Path(root) / assets["frame"]).read_bytes()
            self.assertEqual(source["sha256"], hashlib.sha256(saved).hexdigest())
            decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            expected = cv2.imencode(".jpg", decoded[10:70, 20:60],
                                    [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
            self.assertEqual(expected, (Path(root) / assets["person_crop"]).read_bytes())
            cache.put(b"evicts frame 120", "cam", 122, obs.t + 2, [128, 96])
            self.assertIsNone(app._save_management_snapshot("cam", "intrusion", row))


if __name__ == "__main__":
    unittest.main()
