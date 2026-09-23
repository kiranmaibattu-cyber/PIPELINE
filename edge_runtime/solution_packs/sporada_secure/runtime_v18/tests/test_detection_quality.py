from __future__ import annotations

import numpy as np

from services.worker.detectors.backends.openvino_ocr_async import ALPHABET, decode_logits
from services.worker.pipeline.detection_quality import (
    best_parent_vehicle,
    deduplicate_vehicles,
    plate_crop_is_usable,
)
from services.worker.pipeline.ocr_stabilizer import OcrStabilizer
from services.worker.pipeline.types import Detection


def _detection(class_name, bbox, confidence, *, model_name="vehicle"):
    return Detection(
        bbox=bbox,
        class_id=2,
        class_name=class_name,
        confidence=confidence,
        model_name=model_name,
    )


def test_cross_class_duplicate_is_removed_before_tracking():
    car = _detection("car", [100, 100, 300, 300], 0.82)
    truck = _detection("truck", [90, 90, 320, 320], 0.42)
    distant = _detection("car", [500, 100, 650, 250], 0.77)

    assert deduplicate_vehicles([car, truck, distant]) == [car, distant]


def test_person_overlap_is_not_suppressed_as_vehicle_duplicate():
    car = _detection("car", [100, 100, 300, 300], 0.8)
    person = _detection("pedestrian", [120, 110, 280, 300], 0.9)

    assert deduplicate_vehicles([car, person]) == [car, person]


def test_parent_selection_prefers_stronger_vehicle_when_boxes_overlap():
    broad = _detection("truck", [50, 50, 350, 350], 0.42)
    precise = _detection("car", [100, 100, 300, 300], 0.82)
    plate = _detection("license_plate", [180, 230, 230, 250], 0.7, model_name="license_plate")

    assert best_parent_vehicle(plate, [broad, precise]) is precise


def test_plate_quality_rejects_small_or_blurred_candidates():
    blurred = np.zeros((20, 60, 3), dtype=np.uint8)
    textured = np.indices((20, 60)).sum(axis=0) % 2 * 255
    textured = np.repeat(textured[:, :, None].astype(np.uint8), 3, axis=2)
    settings = dict(
        min_width=32,
        min_height=10,
        min_sharpness=20.0,
        min_aspect_ratio=1.2,
        max_aspect_ratio=8.0,
    )

    assert not plate_crop_is_usable(blurred, width=20, height=10, **settings)
    assert not plate_crop_is_usable(blurred, width=60, height=20, **settings)
    assert plate_crop_is_usable(textured, width=60, height=20, **settings)


def test_ocr_confidence_comes_from_logits_and_gates_confirmation():
    text = "TN22AK0158"
    logits = np.full((1, len(text), len(ALPHABET)), -4.0, dtype=np.float32)
    for position, character in enumerate(text):
        logits[0, position, ALPHABET.index(character)] = 5.0
    decoded, confidence = decode_logits(logits)

    assert decoded == text
    assert confidence > 0.9

    stabilizer = OcrStabilizer(min_confidence=0.4, min_plate_width=32, confirm_min_reads=4)
    for frame in range(4):
        stabilizer.observe("cam1", 7, text, 0.2, frame, plate_width=50)
    assert stabilizer.confirmed_text("cam1", 7) is None
    for frame in range(4, 8):
        stabilizer.observe("cam1", 7, text, confidence, frame, plate_width=50)
    assert stabilizer.confirmed_text("cam1", 7) == text
