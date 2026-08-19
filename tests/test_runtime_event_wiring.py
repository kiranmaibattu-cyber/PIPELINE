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
            )
            sink.publish_packet(packet, [{
                "event_type": "wrong_way",
                "use_case": "wrong_way",
                "camera": {"id": "cam4"},
                "observed_at": "2026-08-19T10:00:00Z",
            }])
            row = json.loads((Path(tmp) / "events.jsonl").read_text().splitlines()[0])

            self.assertEqual("traffic", row["solution_pack"])
            self.assertEqual("wrong_way", row["event_type"])
            self.assertEqual("snapshots", Path(row["snapshot_ref"]).parts[0])
            self.assertTrue((Path(tmp) / row["snapshot_ref"]).exists())

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
