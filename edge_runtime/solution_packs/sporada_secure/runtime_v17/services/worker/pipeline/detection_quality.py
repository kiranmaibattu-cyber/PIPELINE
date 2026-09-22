"""Internal detection quality gates shared by the traffic worker backends."""
from __future__ import annotations

from typing import Iterable, Optional

import cv2


ROAD_VEHICLE_CLASSES = {"bicycle", "car", "motorcycle", "bus", "truck", "rickshaw", "minivan"}


def _intersection(a, b) -> float:
    width = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return width * height


def _area(box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def overlap_metrics(a, b) -> tuple[float, float]:
    """Return intersection-over-union and intersection-over-smaller-area."""
    intersection = _intersection(a, b)
    area_a = _area(a)
    area_b = _area(b)
    union = area_a + area_b - intersection
    smaller = min(area_a, area_b)
    return (
        intersection / union if union > 0.0 else 0.0,
        intersection / smaller if smaller > 0.0 else 0.0,
    )


def deduplicate_vehicles(
    detections: Iterable,
    *,
    iou_threshold: float = 0.55,
    containment_threshold: float = 0.85,
) -> list:
    """Suppress duplicate road-vehicle boxes even when classes differ.

    Person detections are excluded because a pedestrian may legitimately overlap a
    motorcycle or car. Output order follows the detector's original order.
    """
    detections = list(detections)
    candidates = [
        (index, detection)
        for index, detection in enumerate(detections)
        if detection.model_name == "vehicle" and detection.class_name in ROAD_VEHICLE_CLASSES
    ]
    candidates.sort(key=lambda item: (-float(item[1].confidence), item[0]))
    kept: list[tuple[int, object]] = []
    suppressed: set[int] = set()
    for index, detection in candidates:
        if any(
            iou >= iou_threshold or containment >= containment_threshold
            for _, accepted in kept
            for iou, containment in [overlap_metrics(detection.bbox, accepted.bbox)]
        ):
            suppressed.add(index)
        else:
            kept.append((index, detection))
    return [detection for index, detection in enumerate(detections) if index not in suppressed]


def best_parent_vehicle(plate, vehicles: Iterable) -> Optional[object]:
    """Select the best geometric parent rather than the first containing vehicle."""
    plate_area = _area(plate.bbox)
    if plate_area <= 0.0:
        return None
    candidates = []
    for index, vehicle in enumerate(vehicles):
        covered = _intersection(plate.bbox, vehicle.bbox) / plate_area
        if covered < 0.8:
            continue
        candidates.append((covered, float(vehicle.confidence), -_area(vehicle.bbox), -index, vehicle))
    return max(candidates, default=(None, None, None, None, None), key=lambda item: item[:4])[4]


def plate_crop_is_usable(
    crop,
    *,
    width: float,
    height: float,
    min_width: int,
    min_height: int,
    min_sharpness: float,
    min_aspect_ratio: float,
    max_aspect_ratio: float,
) -> bool:
    """Reject plate candidates that cannot support a defensible OCR reading."""
    if crop is None or getattr(crop, "size", 0) == 0:
        return False
    if width < min_width or height < min_height:
        return False
    aspect_ratio = width / max(height, 1.0)
    if not min_aspect_ratio <= aspect_ratio <= max_aspect_ratio:
        return False
    if min_sharpness <= 0.0:
        return True
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float(cv2.Laplacian(gray, cv2.CV_64F).var()) >= min_sharpness
