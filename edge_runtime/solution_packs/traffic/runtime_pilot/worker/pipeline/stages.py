from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Protocol, Sequence

import numpy as np

from .types import Detection, FramePacket


class BatchDetector(Protocol):
    def detect(self, images: Sequence[np.ndarray]) -> List[List[Detection]]:
        ...


class InferenceStage:
    def process(self, packets: Sequence[FramePacket]) -> None:
        raise NotImplementedError


class FrameBatchDetectionStage(InferenceStage):
    """Runs one model directly on full frames using a single batched call."""

    def __init__(
        self,
        name: str,
        detector: BatchDetector,
        processing_interval: int = 0,
    ):
        self.name = name
        self.detector = detector
        self.processing_interval = max(0, int(processing_interval))
        self._frame_counts: dict[str, int] = {}

    def process(self, packets: Sequence[FramePacket]) -> None:
        targets = []
        for packet in packets:
            if packet.frame is None:
                continue
            count = self._frame_counts.get(packet.name, 0)
            self._frame_counts[packet.name] = count + 1
            should_process = count % (self.processing_interval + 1) == 0
            if should_process:
                targets.append(packet)
            else:
                skipped = packet.analytics_state.setdefault("_skipped_detection_models", [])
                if self.name not in skipped:
                    skipped.append(self.name)
        if not targets:
            return

        detections_batch = self.detector.detect([packet.frame for packet in targets])
        for packet, detections in zip(targets, detections_batch):
            packet.detections.extend(detections)


class DetectionResultStage(InferenceStage):
    """Runs logic that consumes current detections without cropping the frame."""

    def __init__(self, name: str, handler: Callable[[FramePacket], None]):
        self.name = name
        self.handler = handler

    def process(self, packets: Sequence[FramePacket]) -> None:
        for packet in packets:
            self.handler(packet)


class CropDetectionStage(InferenceStage):
    """Runs a detector on crops produced from earlier detections."""

    def __init__(
        self,
        name: str,
        detector: BatchDetector,
        parent_model: Optional[str] = None,
        parent_classes: Optional[Iterable[str]] = None,
        parent_filter: Optional[Callable[[FramePacket, Detection], bool]] = None,
        batch_size: Optional[int] = None,
    ):
        self.name = name
        self.detector = detector
        self.parent_model = parent_model
        self.parent_classes = set(parent_classes) if parent_classes else None
        self.parent_filter = parent_filter
        self.batch_size = batch_size

    def process(self, packets: Sequence[FramePacket]) -> None:
        crop_jobs = []
        for packet in packets:
            for detection_index, detection in enumerate(packet.detections):
                if self.parent_model and detection.model_name != self.parent_model:
                    continue
                if detection.metadata.get("predicted"):
                    continue
                if self.parent_classes and detection.class_name not in self.parent_classes:
                    continue
                if self.parent_filter and not self.parent_filter(packet, detection):
                    continue

                x1, y1, x2, y2 = self._clip_bbox(detection.bbox, packet.frame)
                if x2 <= x1 or y2 <= y1:
                    continue

                parent_id = detection.metadata.get("track_id", detection_index)
                crop_jobs.append(
                    {
                        "packet": packet,
                        "parent_id": parent_id,
                        "origin": (x1, y1),
                        "crop": packet.frame[y1:y2, x1:x2],
                    }
                )

        if not crop_jobs:
            return

        batch_size = self.batch_size or len(crop_jobs)
        for start in range(0, len(crop_jobs), batch_size):
            batch_jobs = crop_jobs[start : start + batch_size]
            detections_batch = self.detector.detect(
                [job["crop"] for job in batch_jobs]
            )
            for job, crop_detections in zip(batch_jobs, detections_batch):
                packet = job["packet"]
                dx, dy = job["origin"]
                parent_id = job["parent_id"]
                for crop_detection in crop_detections:
                    packet.detections.append(crop_detection.shifted(dx, dy, parent_id))

    @staticmethod
    def _clip_bbox(bbox: List[int], frame: np.ndarray) -> List[int]:
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = bbox
        return [
            max(0, min(width, x1)),
            max(0, min(height, y1)),
            max(0, min(width, x2)),
            max(0, min(height, y2)),
        ]
