from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from edge_runtime.runtime.source_resolver import CameraSourceResolver


class CameraSourceResolverTest(unittest.TestCase):
    def test_resolves_env_reference(self) -> None:
        os.environ["PIPELINE_TEST_RTSP"] = "rtsp://user:pass@camera/stream"
        try:
            self.assertEqual(
                "rtsp://user:pass@camera/stream",
                CameraSourceResolver().resolve("env:PIPELINE_TEST_RTSP"),
            )
        finally:
            os.environ.pop("PIPELINE_TEST_RTSP", None)

    def test_resolves_secret_file_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "camera-url"
            secret.write_text("rtsp://secret-camera\n", encoding="utf-8")
            self.assertEqual(
                "rtsp://secret-camera",
                CameraSourceResolver().resolve(f"secret:{secret}"),
            )

    def test_plain_source_is_preserved(self) -> None:
        self.assertEqual("rtsp://plain", CameraSourceResolver().resolve("rtsp://plain"))


if __name__ == "__main__":
    unittest.main()
