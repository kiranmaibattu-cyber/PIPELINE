from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edge_runtime.solution_packs.surveillance.runtime.config_adapter import SurveillanceConfigAdapter
from edge_runtime.solution_packs.traffic.runtime.config_adapter import TrafficConfigAdapter
from edge_runtime.solution_packs.traffic.runtime_pilot import launch as traffic_launch


ROOT = Path(__file__).resolve().parents[1]


class RuntimeAdapterTest(unittest.TestCase):
    def test_surveillance_config_scopes_apps_and_feature_flags(self) -> None:
        plan = json.loads((ROOT / "run" / "plans" / "surveillance.runtime_plan.json").read_text())
        rendered = SurveillanceConfigAdapter().render(plan)
        usecases = rendered["runtime_usecases"]["usecases"]
        self.assertEqual(usecases["reid"], ["cam1", "cam2"])
        self.assertEqual(usecases["face"], ["cam2"])
        self.assertEqual(usecases["counting"], ["cam3"])
        self.assertTrue(rendered["camera_features"]["cam1"]["gait"])
        self.assertFalse(rendered["camera_features"]["cam3"]["body"])

    def test_traffic_config_maps_apps_to_traffic_usecases(self) -> None:
        plan = json.loads((ROOT / "run" / "plans" / "traffic.runtime_plan.json").read_text())
        rendered = TrafficConfigAdapter().render(plan)
        cameras = {c["camera_id"]: c for c in rendered["cameras"]}
        self.assertIn("plate_detection", cameras["cam4"]["analytics"])
        self.assertIn("wrong_way_driving_detection", cameras["cam4"]["analytics"])
        self.assertIn("parking_violation_detection", cameras["cam5"]["analytics"])
        self.assertNotIn("plate_detection", cameras["cam5"]["analytics"])

    def test_traffic_counting_apps_get_default_lines_without_roi(self) -> None:
        plan = {
            "cameras": [{
                "camera_id": "cam-counting",
                "source": "file:/run/secrets/apexfabric/cam-counting.rtsp",
                "fps": 8,
                "apps": ["vehicle_counting", "pedestrian_counting"],
                "config": {},
            }]
        }
        rendered = TrafficConfigAdapter().render(plan)
        analytics = rendered["cameras"][0]["analytics"]

        vehicle_lines = analytics["vehicle_counting"]["lines"]
        pedestrian_lines = analytics["pedestrian_counting"]["lines"]
        self.assertEqual(1, len(vehicle_lines))
        self.assertEqual("default_vehicle_count_line", vehicle_lines[0]["id"])
        self.assertEqual("both", vehicle_lines[0]["direction"])
        self.assertEqual(1, len(pedestrian_lines))
        self.assertEqual("default_pedestrian_count_line", pedestrian_lines[0]["id"])
        self.assertEqual("both", pedestrian_lines[0]["direction"])

    def test_surveillance_runtime_usecases_stay_in_generated_directory(self) -> None:
        plan = json.loads((ROOT / "run" / "plans" / "surveillance.runtime_plan.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            SurveillanceConfigAdapter().write(plan, output_dir)
            runtime_config = output_dir / "runtime_usecases.generated.json"
            usecases = json.loads(runtime_config.read_text(encoding="utf-8"))["usecases"]
        self.assertEqual(usecases["reid"], ["cam1", "cam2"])
        self.assertEqual(usecases["intrusion"], ["cam2", "cam3"])

    def test_traffic_launcher_writes_openvino_worker_config(self) -> None:
        plan = json.loads((ROOT / "run" / "plans" / "traffic.runtime_plan.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            worker_config = traffic_launch._write_worker_config(plan, Path(tmp))
            data = json.loads(worker_config.read_text(encoding="utf-8"))
        self.assertEqual(data["models"]["vehicle"]["backend"], "openvino")
        self.assertEqual(data["models"]["plate"]["backend"], "openvino")
        self.assertEqual(data["models"]["license_plate_ocr"]["backend"], "openvino")
        self.assertFalse(data["json_streaming"]["debug_tap"])
        self.assertEqual([], data["json_streaming"]["outputs"])


if __name__ == "__main__":
    unittest.main()
