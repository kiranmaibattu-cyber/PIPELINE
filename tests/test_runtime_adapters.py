from __future__ import annotations

import json
import tempfile
import unittest
import os
from pathlib import Path

from edge_runtime.solution_packs.surveillance.runtime.config_adapter import SurveillanceConfigAdapter
from edge_runtime.solution_packs.surveillance.runtime_8090 import launch as surveillance_launch
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

    def test_traffic_counting_apps_use_unique_track_mode_without_roi(self) -> None:
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
        self.assertEqual([], vehicle_lines)
        self.assertEqual([], pedestrian_lines)

    def test_surveillance_runtime_usecases_stay_in_generated_directory(self) -> None:
        plan = json.loads((ROOT / "run" / "plans" / "surveillance.runtime_plan.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            SurveillanceConfigAdapter().write(plan, output_dir)
            runtime_config = output_dir / "runtime_usecases.generated.json"
            usecases = json.loads(runtime_config.read_text(encoding="utf-8"))["usecases"]
        self.assertEqual(usecases["reid"], ["cam1", "cam2"])
        self.assertEqual(usecases["intrusion"], ["cam2", "cam3"])

    def test_surveillance_launcher_initializes_persistent_face_gallery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gallery = Path(tmp) / "state" / "face_gallery"
            surveillance_launch._initialize_face_gallery(gallery)
            self.assertTrue((gallery / "index.json").is_file())
            self.assertTrue((gallery / "vectors.npy").is_file())

    def test_surveillance_runtime_config_preserves_face_groups_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated"
            state = root / "state"
            generated.mkdir()
            (generated / "runtime_usecases.generated.json").write_text(json.dumps({
                "usecases": {"reid": ["cam-new"]},
                "zones": {"cameras": {"cam-new": {}}},
                "face_groups": {},
            }), encoding="utf-8")
            persisted = state / "runtime" / "runtime_usecases.json"
            persisted.parent.mkdir(parents=True)
            persisted.write_text(json.dumps({
                "usecases": {"reid": ["stale-cam"]},
                "zones": {"cameras": {"stale-cam": {}}},
                "face_groups": {"Alice": "unauthorised"},
            }), encoding="utf-8")

            path = surveillance_launch._prepare_runtime_config(generated, state)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(["cam-new"], data["usecases"]["reid"])
        self.assertIn("cam-new", data["zones"]["cameras"])
        self.assertEqual({"Alice": "unauthorised"}, data["face_groups"])

    def test_surveillance_launcher_exports_runtime_gallery_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            models = Path(tmp) / "models"
            old = {key: os.environ.get(key) for key in ("FACE_GALLERY", "REJOIN_STORE")}
            try:
                os.environ.pop("FACE_GALLERY", None)
                os.environ.pop("REJOIN_STORE", None)
                surveillance_launch._configure_environment({"cameras": []}, state, models, 8090)
                self.assertEqual(str(state / "face_gallery"), os.environ["FACE_GALLERY"])
                self.assertEqual(str(state / "reid_gallery"), os.environ["REJOIN_STORE"])
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

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
