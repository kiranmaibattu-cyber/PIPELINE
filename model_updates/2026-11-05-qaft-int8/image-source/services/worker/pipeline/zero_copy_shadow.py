"""Non-authoritative detector comparator used to qualify zero-copy inference.

This module deliberately has no tracker, policy, evidence, or delivery imports.
It compares a shadow detector with the existing Python OpenVINO detector by
source PTS. The comparison path can therefore be removed or fail without
changing any production observation. ``attach`` remains available for an
external DL Streamer probe, while V8 currently submits native VA RemoteTensor
results directly.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
import threading
from typing import Any, Iterable


log = logging.getLogger(__name__)

TRAFFIC_LABEL_IDS = {
    "bus": 0,
    "car": 1,
    "minivan": 2,
    "motorcycle": 3,
    "rickshaw": 4,
    "truck": 5,
    "bicycle": 6,
    "person": 7,
    "pedestrian": 7,
}
TRAFFIC_CLASS_NAMES = {
    class_id: ("pedestrian" if label == "person" else label)
    for label, class_id in TRAFFIC_LABEL_IDS.items()
}


@dataclass(frozen=True)
class ShadowDetection:
    bbox: tuple[int, int, int, int]
    class_id: int
    class_name: str
    confidence: float


def _primitive_detection(value: Any) -> ShadowDetection:
    """Detach a detection from frame/model objects before retaining it."""
    if isinstance(value, ShadowDetection):
        return value
    bbox = tuple(int(item) for item in value.bbox)
    return ShadowDetection(
        bbox=bbox,
        class_id=int(value.class_id),
        class_name=str(value.class_name),
        confidence=float(value.confidence),
    )


def bbox_iou(left: Iterable[int], right: Iterable[int]) -> float:
    ax1, ay1, ax2, ay2 = (int(value) for value in left)
    bx1, by1, bx2, by2 = (int(value) for value in right)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    intersection = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if not intersection:
        return 0.0
    left_area = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    right_area = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def compare_detection_sets(
    authoritative: Iterable[Any],
    shadow: Iterable[Any],
    *,
    minimum_iou: float = 0.5,
) -> dict[str, float | int]:
    """Greedily match same-class boxes without mutating either input list."""
    expected = [_primitive_detection(value) for value in authoritative]
    observed = [_primitive_detection(value) for value in shadow]
    remaining = set(range(len(observed)))
    matched = 0
    iou_total = 0.0
    confidence_delta_total = 0.0
    for reference in expected:
        candidates = [
            (bbox_iou(reference.bbox, observed[index].bbox), index)
            for index in remaining
            if observed[index].class_id == reference.class_id
        ]
        if not candidates:
            continue
        iou, index = max(candidates)
        if iou < minimum_iou:
            continue
        remaining.remove(index)
        matched += 1
        iou_total += iou
        confidence_delta_total += abs(
            reference.confidence - observed[index].confidence
        )
    return {
        "authoritative": len(expected),
        "shadow": len(observed),
        "matched": matched,
        "missing": len(expected) - matched,
        "extra": len(observed) - matched,
        "mean_iou": iou_total / matched if matched else 0.0,
        "mean_confidence_delta": (
            confidence_delta_total / matched if matched else 0.0
        ),
    }


class ZeroCopyShadowComparator:
    """Correlate detector outputs by PTS without blocking the camera worker."""

    def __init__(self, camera_id: str, *, telemetry=None, capacity: int = 128):
        self.camera_id = str(camera_id)
        self.telemetry = telemetry
        self.capacity = max(8, int(capacity))
        self._authoritative: OrderedDict[int, list[ShadowDetection]] = OrderedDict()
        self._shadow: OrderedDict[int, list[ShadowDetection]] = OrderedDict()
        self._lock = threading.RLock()
        self._probe = None
        self._probe_id = None
        self._video_frame = None
        self.frames_compared = 0
        self.frames_evicted = 0
        self.matched = 0
        self.missing = 0
        self.extra = 0
        self.iou_total = 0.0
        self.confidence_delta_total = 0.0

    def attach(self, pipeline, Gst) -> None:
        try:
            from gstgva import VideoFrame
        except ImportError as exc:
            raise RuntimeError("DL Streamer gstgva bindings are unavailable") from exc
        detector = pipeline.get_by_name("zero_copy_shadow_detector")
        pad = detector.get_static_pad("src") if detector is not None else None
        if pad is None:
            raise RuntimeError("zero-copy shadow detector output pad is unavailable")
        self._video_frame = VideoFrame
        self._probe = pad
        self._probe_id = pad.add_probe(
            Gst.PadProbeType.BUFFER, self._on_shadow_buffer, Gst
        )

    def _on_shadow_buffer(self, pad, info, Gst):
        buffer = info.get_buffer()
        caps = pad.get_current_caps()
        if buffer is None or caps is None:
            return Gst.PadProbeReturn.OK
        pts = int(buffer.pts)
        if pts == int(Gst.CLOCK_TIME_NONE):
            return Gst.PadProbeReturn.OK
        frame = self._video_frame(buffer, caps=caps)
        detections = []
        for region in frame.regions():
            source_label = str(region.label() or "").strip().lower()
            class_id = TRAFFIC_LABEL_IDS.get(source_label)
            if class_id is None:
                continue
            rect = region.rect()
            detections.append(ShadowDetection(
                bbox=(
                    int(rect.x), int(rect.y),
                    int(rect.x + rect.w), int(rect.y + rect.h),
                ),
                class_id=class_id,
                class_name=TRAFFIC_CLASS_NAMES[class_id],
                confidence=float(region.confidence()),
            ))
        self.submit_shadow(pts, detections)
        return Gst.PadProbeReturn.OK

    def submit_authoritative(self, pts: int, detections: Iterable[Any]) -> None:
        self._submit(self._authoritative, int(pts), detections)

    def submit_shadow(self, pts: int, detections: Iterable[Any]) -> None:
        self._submit(self._shadow, int(pts), detections)

    def _submit(self, target, pts: int, detections: Iterable[Any]) -> None:
        detached = [_primitive_detection(value) for value in detections]
        with self._lock:
            target[pts] = detached
            target.move_to_end(pts)
            self._compare_ready_locked(pts)
            self._trim_locked(self._authoritative)
            self._trim_locked(self._shadow)

    def _compare_ready_locked(self, pts: int) -> None:
        if pts not in self._authoritative or pts not in self._shadow:
            return
        result = compare_detection_sets(
            self._authoritative.pop(pts), self._shadow.pop(pts)
        )
        self.frames_compared += 1
        self.matched += int(result["matched"])
        self.missing += int(result["missing"])
        self.extra += int(result["extra"])
        self.iou_total += float(result["mean_iou"]) * int(result["matched"])
        self.confidence_delta_total += (
            float(result["mean_confidence_delta"]) * int(result["matched"])
        )
        if self.telemetry is not None:
            self.telemetry.count("zero_copy_shadow_frames_compared")
            self.telemetry.count("zero_copy_shadow_matched", int(result["matched"]))
            self.telemetry.count("zero_copy_shadow_missing", int(result["missing"]))
            self.telemetry.count("zero_copy_shadow_extra", int(result["extra"]))
        if self.frames_compared % 50 == 0:
            log.info("cam[%s] zero-copy shadow parity %s", self.camera_id, self.snapshot())

    def _trim_locked(self, values: OrderedDict) -> None:
        while len(values) > self.capacity:
            values.popitem(last=False)
            self.frames_evicted += 1
            if self.telemetry is not None:
                self.telemetry.count("zero_copy_shadow_unpaired_evictions")

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            matched = self.matched
            return {
                "frames_compared": self.frames_compared,
                "unpaired_evictions": self.frames_evicted,
                "matched": matched,
                "missing": self.missing,
                "extra": self.extra,
                "mean_iou": self.iou_total / matched if matched else 0.0,
                "mean_confidence_delta": (
                    self.confidence_delta_total / matched if matched else 0.0
                ),
                "pending_authoritative": len(self._authoritative),
                "pending_shadow": len(self._shadow),
            }

    def close(self) -> dict[str, float | int]:
        if self._probe is not None and self._probe_id is not None:
            self._probe.remove_probe(self._probe_id)
        self._probe = self._probe_id = None
        result = self.snapshot()
        log.info("cam[%s] zero-copy shadow final parity %s", self.camera_id, result)
        return result
