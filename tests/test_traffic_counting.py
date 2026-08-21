from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np


WORKER_ROOT = (
    Path(__file__).resolve().parents[1]
    / "edge_runtime/solution_packs/traffic/runtime_pilot/worker"
)
sys.path.insert(0, str(WORKER_ROOT))

from pipeline.analytics import TrafficAnalyticsStage, geometry_points  # noqa: E402
from pipeline.output_sinks import simple_event  # noqa: E402
from pipeline.types import Detection, FramePacket  # noqa: E402
from stream_fleet_openvino import PLATE_PARENT_CLASSES, VEHICLE_CLASS_IDS  # noqa: E402


def _packet(index: int, hits: int, class_name: str = "car") -> FramePacket:
    packet = FramePacket(
        index=index,
        name="traffic1",
        frame=np.zeros((1080, 1920, 3), dtype=np.uint8),
    )
    packet.detections = [Detection(
        bbox=[500, 400 + index * 10, 800, 700 + index * 10],
        class_id=2,
        class_name=class_name,
        confidence=0.9,
        model_name="vehicle",
        metadata={"track_id": 7, "track_hits": hits},
    )]
    return packet


class TrafficCountingTest(unittest.TestCase):
    def test_openvino_detector_includes_pedestrians_without_plate_cascade(self) -> None:
        self.assertIn(0, VEHICLE_CLASS_IDS)
        self.assertNotIn("pedestrian", PLATE_PARENT_CLASSES)

    def test_normalized_geometry_scales_to_current_frame(self) -> None:
        line = {"shape": "line", "points": [{"x": 0.1, "y": 0.65}, {"x": 0.9, "y": 0.65}]}
        self.assertEqual(
            [(192.0, 702.0), (1728.0, 702.0)],
            geometry_points(line, (1080, 1920, 3), {}),
        )

    def test_vehicle_counting_without_geometry_counts_unique_stable_track(self) -> None:
        config = {"traffic1": {"runtime_analytics": {
            "vehicle_counting": {"lines": [], "zones": [], "constraint_zones": []},
        }}}
        stage = TrafficAnalyticsStage(config)
        first = _packet(1, 1)
        second = _packet(2, 2)
        third = _packet(3, 3)

        stage.process([first])
        stage.process([second])
        stage.process([third])

        self.assertEqual([], first.analytics_events)
        self.assertEqual(1, len(second.analytics_events))
        self.assertEqual("vehicle_count", second.analytics_events[0]["type"])
        self.assertEqual(1, second.analytics_events[0]["value"])
        self.assertEqual("unique_track", second.analytics_events[0]["count_mode"])
        self.assertEqual(
            "unique_track",
            simple_event(second.analytics_events[0])["count"]["mode"],
        )
        self.assertEqual([], third.analytics_events)


if __name__ == "__main__":
    unittest.main()
