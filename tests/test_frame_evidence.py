import unittest
from edge_runtime.runtime.frame_evidence import FrameEvidenceCache, read_evidence


class FrameEvidenceTests(unittest.TestCase):
    def test_delayed_read_keeps_original_frame_and_restart_identity(self):
        first = FrameEvidenceCache(max_frames=2)
        second = FrameEvidenceCache(max_frames=2)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        original = first.put(b"frame-N", "camera", 1, 100.0, [32, 32])
        first.put(b"frame-N-plus-one", "camera", 2, 101.0, [32, 32])
        restarted = second.put(b"restart-frame", "camera", 1, 102.0, [32, 32])
        self.assertEqual(b"frame-N", read_evidence(original))
        self.assertNotEqual(original["stream_session_id"], restarted["stream_session_id"])
        first.put(b"frame-N-plus-two", "camera", 3, 103.0, [32, 32])
        self.assertIsNone(read_evidence(original))

    def test_byte_limit_and_replaced_content_are_rejected(self):
        from pathlib import Path
        cache = FrameEvidenceCache(max_bytes=8)
        self.addCleanup(cache.close)
        evidence = cache.put(b"1234", "camera", 1, 100.0, [1, 1])
        Path(evidence["path"]).write_bytes(b"5678")
        self.assertIsNone(read_evidence(evidence))
        too_large = cache.put(b"123456789", "camera", 2, 101.0, [1, 1])
        self.assertIsNone(read_evidence(too_large))


if __name__ == "__main__":
    unittest.main()
