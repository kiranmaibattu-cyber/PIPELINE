from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from edge_runtime.runtime.management_outbox import ManagementEventWriter
from edge_runtime.solution_packs.traffic.runtime_pilot.worker.pipeline.output_sinks import (
    AsyncAnalyticsDispatcher,
)
from edge_runtime.solution_packs.traffic.runtime_pilot.worker.pipeline.local_event_sink import (
    LocalManagementEventSink,
)
from edge_runtime.solution_packs.traffic.runtime_pilot.worker.pipeline.types import Detection


class RuntimeEventWiringTest(unittest.TestCase):
    def test_management_event_writer_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            writer = ManagementEventWriter(Path(tmp), "surveillance")
            writer.write({"event_type": "intrusion", "camera_id": "cam1"})
            row = json.loads((Path(tmp) / "events.jsonl").read_text().splitlines()[0])

        self.assertEqual("surveillance", row["solution_pack"])
        self.assertEqual("intrusion", row["event_type"])
        self.assertIn("timestamp_utc", row)

    def test_traffic_local_sink_writes_event_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = LocalManagementEventSink(tmp)
            packet = SimpleNamespace(
                name="cam4",
                index=12,
                frame=np.zeros((16, 16, 3), dtype=np.uint8),
                detections=[],
            )
            sink.publish_packet(packet, [{
                "event_type": "wrong_way",
                "use_case": "wrong_way",
                "camera": {"id": "cam4"},
                "observed_at": "2026-08-19T10:00:00Z",
                "subject": {"bbox": {"x1": 2, "y1": 3, "x2": 10, "y2": 12}},
            }])
            row = json.loads((Path(tmp) / "events.jsonl").read_text().splitlines()[0])

            self.assertEqual("traffic", row["solution_pack"])
            self.assertEqual("wrong_way", row["event_type"])
            self.assertEqual("snapshots", Path(row["snapshot_ref"]).parts[0])
            self.assertTrue((Path(tmp) / row["snapshot_ref"]).exists())
            self.assertIn("event_frame", row["snapshot_refs"])
            self.assertIn("vehicle_crop", row["snapshot_refs"])
            self.assertTrue((Path(tmp) / row["snapshot_refs"]["vehicle_crop"]).exists())

    def test_traffic_local_sink_writes_anpr_vehicle_and_plate_crops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = LocalManagementEventSink(tmp)
            frame = np.zeros((32, 32, 3), dtype=np.uint8)
            frame[4:24, 3:28] = 100
            frame[18:24, 12:24] = 200
            packet = SimpleNamespace(
                name="cam4",
                index=13,
                frame=frame,
                detections=[
                    Detection(
                        bbox=[3, 4, 28, 24],
                        class_id=2,
                        class_name="car",
                        confidence=0.9,
                        model_name="vehicle",
                        metadata={"track_id": 7},
                    )
                ],
            )
            sink.publish_packet(packet, [{
                "event_type": "plate_read",
                "use_case": "plate_detection",
                "camera": {"id": "cam4"},
                "observed_at": "2026-08-19T10:00:01Z",
                "plate": {"text": "KA52P1295"},
                "subject": {
                    "track_id": 11,
                    "parent_track_id": 7,
                    "bbox": {"x1": 12, "y1": 18, "x2": 24, "y2": 24},
                },
            }])
            row = json.loads((Path(tmp) / "events.jsonl").read_text().splitlines()[0])

            self.assertEqual("plate_read", row["event_type"])
            self.assertIn("plate_crop", row["snapshot_refs"])
            self.assertIn("vehicle_crop", row["snapshot_refs"])
            self.assertTrue((Path(tmp) / row["snapshot_refs"]["plate_crop"]).exists())
            self.assertTrue((Path(tmp) / row["snapshot_refs"]["vehicle_crop"]).exists())
            self.assertEqual(row["snapshot_refs"]["vehicle_crop"], row["snapshot_ref"])
            self.assertEqual(7, row["vehicle_track_id"])
            self.assertEqual(row["vehicle_ref"], row["vehicle"]["ref"])
            self.assertEqual("KA52P1295", row["vehicle"]["plate"]["text"])

    def test_wrong_way_and_anpr_are_linked_to_same_vehicle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_session = os.environ.get("EDGE_RUNTIME_SESSION_ID")
            os.environ["EDGE_RUNTIME_SESSION_ID"] = "run-42"
            try:
                sink = LocalManagementEventSink(tmp)
            finally:
                if old_session is None:
                    os.environ.pop("EDGE_RUNTIME_SESSION_ID", None)
                else:
                    os.environ["EDGE_RUNTIME_SESSION_ID"] = old_session
            packet = SimpleNamespace(
                name="cam4",
                index=14,
                frame=np.zeros((32, 32, 3), dtype=np.uint8),
                detections=[
                    Detection(
                        bbox=[3, 4, 28, 24], class_id=2, class_name="car",
                        confidence=0.9, model_name="vehicle", metadata={"track_id": 7},
                    ),
                    Detection(
                        bbox=[12, 18, 24, 24], class_id=0, class_name="plate",
                        confidence=0.88, model_name="license_plate", parent_id=7,
                        metadata={"ocr_text": "KA52P1295"},
                    ),
                ],
            )
            common = {
                "camera": {"id": "cam4"},
                "observed_at": "2026-08-19T10:00:02Z",
            }
            sink.publish_packet(packet, [
                {
                    **common,
                    "event_type": "wrong_way",
                    "use_case": "wrong_way",
                    "object_id": 7,
                    "subject": {
                        "track_id": 7,
                        "bbox": {"x1": 3, "y1": 4, "x2": 28, "y2": 24},
                    },
                },
                {
                    **common,
                    "event_type": "plate_read",
                    "use_case": "plate_detection",
                    "object_id": 7,
                    "plate": {"text": "KA52P1295"},
                    "subject": {
                        "track_id": 7,
                        "parent_track_id": 7,
                        "bbox": {"x1": 12, "y1": 18, "x2": 24, "y2": 24},
                    },
                },
            ])
            rows = [json.loads(line) for line in (Path(tmp) / "events.jsonl").read_text().splitlines()]

            self.assertEqual(rows[0]["vehicle_ref"], rows[1]["vehicle_ref"])
            self.assertEqual("cam4:run-42:7", rows[0]["vehicle_ref"])
            self.assertEqual("KA52P1295", rows[0]["plate"]["text"])
            self.assertIn("vehicle_crop", rows[0]["snapshot_refs"])
            self.assertIn("plate_crop", rows[0]["snapshot_refs"])

    def test_traffic_dispatcher_writes_local_event_sink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("MANAGEMENT_STATE_DIR")
            os.environ["MANAGEMENT_STATE_DIR"] = tmp
            try:
                dispatcher = AsyncAnalyticsDispatcher(sink=None)
                packet = SimpleNamespace(
                    name="cam5",
                    index=8,
                    frame=np.zeros((16, 16, 3), dtype=np.uint8),
                    detections=[],
                    analytics_events=[{
                        "type": "illegal_parking",
                        "use_case": "illegal_parking",
                        "camera": {"id": "cam5"},
                    }],
                )
                dispatcher.publish_packets([packet])
                row = json.loads((Path(tmp) / "events.jsonl").read_text().splitlines()[0])
            finally:
                if old is None:
                    os.environ.pop("MANAGEMENT_STATE_DIR", None)
                else:
                    os.environ["MANAGEMENT_STATE_DIR"] = old

        self.assertEqual("traffic", row["solution_pack"])
        self.assertEqual("illegal_parking", row["event_type"])


if __name__ == "__main__":
    unittest.main()
