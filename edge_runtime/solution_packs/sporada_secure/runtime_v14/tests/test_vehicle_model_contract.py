from __future__ import annotations

import ast
from pathlib import Path
import xml.etree.ElementTree as ET


REPOSITORY = Path(__file__).resolve().parents[5]
MODEL = REPOSITORY / "models/traffic-v11/openvino/vehicle.xml"


def test_vehicle_model_preserves_runtime_tensor_contract():
    root = ET.parse(MODEL).getroot()
    parameter = root.find("./layers/layer[@type='Parameter']")
    result = root.find("./layers/layer[@type='Result']")

    assert parameter is not None
    assert parameter.find("data").attrib["shape"] == "1,3,640,640"
    assert result is not None
    result_input = result.find("input/port")
    assert result_input is not None
    assert [dim.text for dim in result_input.findall("dim")] == ["1", "300", "6"]


def test_vehicle_model_class_ids_match_worker_mapping():
    root = ET.parse(MODEL).getroot()
    names = root.find("./rt_info/framework/names")

    assert names is not None
    classes = ast.literal_eval(names.attrib["value"])
    assert {class_id: classes[class_id] for class_id in (0, 2, 3, 5, 7)} == {
        0: "person",
        2: "car",
        3: "motorcycle",
        5: "bus",
        7: "truck",
    }
