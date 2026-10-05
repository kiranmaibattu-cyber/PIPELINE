from __future__ import annotations

from collections import OrderedDict
from typing import Sequence

import numpy as np

from .types import Detection, FramePacket


class CentroidTracker:
    def __init__(
        self,
        max_disappeared=50,
        max_distance=160.0,
        bbox_smoothing=0.35,
        min_iou=0.02,
        class_switch_cost=0.15,
    ):
        self.next_object_id = 1
        self.objects = OrderedDict()
        self.origin_rects = OrderedDict()
        self.class_names = OrderedDict()
        self.confidences = OrderedDict()
        self.velocities = OrderedDict()
        self.bbox_velocities = OrderedDict()
        self.disappeared = OrderedDict()
        self.hits = OrderedDict()
        self.ages = OrderedDict()
        self.max_disappeared = max_disappeared
        self.max_distance = max_distance
        self.bbox_smoothing = bbox_smoothing
        self.min_iou = min_iou
        self.class_switch_cost = class_switch_cost

    def register(self, centroid, rect, class_name, confidence=0.0):
        object_id = self.next_object_id
        self.objects[object_id] = np.array(centroid, dtype=np.float32)
        self.origin_rects[object_id] = [float(value) for value in rect]
        self.class_names[object_id] = class_name
        self.confidences[object_id] = float(confidence)
        self.velocities[object_id] = np.array([0.0, 0.0], dtype=np.float32)
        self.bbox_velocities[object_id] = np.zeros(4, dtype=np.float32)
        self.disappeared[object_id] = 0
        self.hits[object_id] = 1
        self.ages[object_id] = 1
        self.next_object_id += 1
        return object_id

    def deregister(self, object_id):
        del self.objects[object_id]
        del self.origin_rects[object_id]
        del self.class_names[object_id]
        del self.confidences[object_id]
        del self.velocities[object_id]
        del self.bbox_velocities[object_id]
        del self.disappeared[object_id]
        del self.hits[object_id]
        del self.ages[object_id]

    def update(self, detections: Sequence[Detection], class_aware=True):
        rects = [detection.bbox for detection in detections]
        class_names = [detection.class_name for detection in detections]

        if len(rects) == 0:
            for object_id in list(self.disappeared.keys()):
                self.disappeared[object_id] += 1
                self.ages[object_id] += 1
                if self.disappeared[object_id] > self.max_disappeared:
                    self.deregister(object_id)
            return {}

        input_centroids = np.array(
            [self._centroid(rect) for rect in rects],
            dtype=np.float32,
        )

        if len(self.objects) == 0:
            return {
                detection_index: self.register(
                    input_centroids[detection_index],
                    rects[detection_index],
                    class_names[detection_index],
                    detections[detection_index].confidence,
                )
                for detection_index in range(len(detections))
            }

        object_ids = list(self.objects.keys())
        predicted_rects = [self._predicted_rect(object_id) for object_id in object_ids]
        predicted_centroids = np.array(
            [self._centroid(rect) for rect in predicted_rects],
            dtype=np.float32,
        )
        distances = self._distance_matrix(predicted_centroids, input_centroids)
        ious = self._iou_matrix(predicted_rects, rects)
        motion_costs = self._motion_cost_matrix(object_ids, input_centroids)
        costs = (
            (distances / max(self.max_distance, 1.0)) * 0.45
            + (1.0 - ious) * 0.45
            + motion_costs * 0.10
        )

        if class_aware:
            for row, object_id in enumerate(object_ids):
                for col, class_name in enumerate(class_names):
                    if self.class_names[object_id] != class_name:
                        costs[row, col] = np.inf
        else:
            for row, object_id in enumerate(object_ids):
                for col, class_name in enumerate(class_names):
                    if self.class_names[object_id] != class_name:
                        costs[row, col] += self.class_switch_cost

        candidates = [
            (float(costs[row, col]), row, col)
            for row in range(costs.shape[0])
            for col in range(costs.shape[1])
            if np.isfinite(costs[row, col])
        ]
        candidates.sort(key=lambda item: item[0])

        used_rows = set()
        used_cols = set()
        matches = {}

        for cost, row, col in candidates:
            if row in used_rows or col in used_cols:
                continue
            distance = distances[row, col]
            iou = ious[row, col]
            if distance > self.max_distance and iou < self.min_iou:
                continue
            if cost > 1.25 and iou < self.min_iou:
                continue

            object_id = object_ids[row]
            self._update_track(
                object_id,
                input_centroids[col],
                rects[col],
                class_names[col],
                detections[col].confidence,
            )
            matches[col] = object_id
            used_rows.add(row)
            used_cols.add(col)

        unused_rows = set(range(distances.shape[0])).difference(used_rows)
        unused_cols = set(range(distances.shape[1])).difference(used_cols)

        for row in unused_rows:
            object_id = object_ids[row]
            self.disappeared[object_id] += 1
            self.ages[object_id] += 1
            if self.disappeared[object_id] > self.max_disappeared:
                self.deregister(object_id)

        for col in unused_cols:
            matches[col] = self.register(
                input_centroids[col],
                rects[col],
                class_names[col],
                detections[col].confidence,
            )

        return matches

    def predict(self):
        predictions = []
        for object_id in list(self.objects.keys()):
            bbox_velocity = self.bbox_velocities.get(object_id)
            if bbox_velocity is None:
                bbox_velocity = np.zeros(4, dtype=np.float32)
            self.origin_rects[object_id] = self._predicted_rect(object_id)
            self.objects[object_id] = self._centroid(self.origin_rects[object_id])
            self.disappeared[object_id] = self.disappeared.get(object_id, 0) + 1
            self.ages[object_id] = self.ages.get(object_id, 0) + 1
            if self.disappeared[object_id] > self.max_disappeared:
                self.deregister(object_id)
                continue
            predictions.append(object_id)
        return predictions

    @staticmethod
    def _centroid(rect):
        start_x, start_y, end_x, end_y = rect
        return np.array(
            [int((start_x + end_x) / 2.0), int((start_y + end_y) / 2.0)],
            dtype=np.float32,
        )

    def _smooth_rect(self, previous, current):
        alpha = self.bbox_smoothing
        return [
            float(previous[index]) * alpha + float(current[index]) * (1.0 - alpha)
            for index in range(4)
        ]

    def _update_track(self, object_id, centroid, rect, class_name, confidence):
        previous_centroid = self.objects[object_id]
        previous_rect = np.array(self.origin_rects[object_id], dtype=np.float32)
        smoothed_rect = np.array(
            self._smooth_rect(previous_rect, rect),
            dtype=np.float32,
        )
        self.objects[object_id] = self._centroid(smoothed_rect)
        self.velocities[object_id] = np.asarray(centroid, dtype=np.float32) - previous_centroid
        self.bbox_velocities[object_id] = smoothed_rect - previous_rect
        if float(confidence) >= self.confidences.get(object_id, 0.0) or self.disappeared.get(object_id, 0) > 0:
            self.class_names[object_id] = class_name
        self.confidences[object_id] = float(confidence)
        self.origin_rects[object_id] = smoothed_rect.tolist()
        self.disappeared[object_id] = 0
        self.hits[object_id] += 1
        self.ages[object_id] += 1

    def _predicted_rect(self, object_id):
        rect = np.array(self.origin_rects[object_id], dtype=np.float32)
        velocity = self.bbox_velocities.get(object_id)
        if velocity is None:
            return rect.tolist()
        missed = min(self.disappeared.get(object_id, 0) + 1, 3)
        return (rect + np.asarray(velocity, dtype=np.float32) * missed).tolist()

    def _motion_cost_matrix(self, object_ids, input_centroids):
        costs = np.zeros((len(object_ids), len(input_centroids)), dtype=np.float32)
        for row, object_id in enumerate(object_ids):
            velocity = self.velocities.get(object_id)
            if velocity is None:
                continue
            velocity_norm = float(np.linalg.norm(velocity))
            if velocity_norm < 1.0:
                continue
            previous = np.asarray(self.objects[object_id], dtype=np.float32)
            deltas = input_centroids - previous
            delta_norms = np.linalg.norm(deltas, axis=1)
            valid = delta_norms > 1.0
            if not np.any(valid):
                continue
            cosine = np.zeros(len(input_centroids), dtype=np.float32)
            cosine[valid] = np.clip(
                np.dot(deltas[valid], velocity) / (delta_norms[valid] * velocity_norm),
                -1.0,
                1.0,
            )
            costs[row] = (1.0 - cosine) * 0.5
        return costs

    @staticmethod
    def _distance_matrix(a, b):
        diff = a[:, None, :] - b[None, :, :]
        return np.sqrt(np.sum(diff * diff, axis=2))

    @staticmethod
    def _iou_matrix(a_rects, b_rects):
        if not a_rects or not b_rects:
            return np.zeros((len(a_rects), len(b_rects)), dtype=np.float32)
        a = np.array(a_rects, dtype=np.float32)
        b = np.array(b_rects, dtype=np.float32)
        x_left = np.maximum(a[:, None, 0], b[None, :, 0])
        y_top = np.maximum(a[:, None, 1], b[None, :, 1])
        x_right = np.minimum(a[:, None, 2], b[None, :, 2])
        y_bottom = np.minimum(a[:, None, 3], b[None, :, 3])
        inter_w = np.maximum(0.0, x_right - x_left)
        inter_h = np.maximum(0.0, y_bottom - y_top)
        intersection = inter_w * inter_h
        a_area = np.maximum(0.0, (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1]))
        b_area = np.maximum(0.0, (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1]))
        union = a_area[:, None] + b_area[None, :] - intersection
        return np.where(union > 0, intersection / union, 0.0).astype(np.float32)


class DetectionTrackerStage:
    def __init__(
        self,
        model_name: str = "vehicle",
        metadata_key: str = "track_id",
        max_distance: float = 160.0,
        max_disappeared: int = 50,
        bbox_smoothing: float = 0.65,
        min_iou: float = 0.02,
        class_switch_cost: float = 0.15,
        class_aware: bool = True,
        draw_predictions: bool = True,
    ):
        self.model_name = model_name
        self.metadata_key = metadata_key
        self.class_aware = class_aware
        self.draw_predictions = draw_predictions
        self._trackers_by_source: dict[str, CentroidTracker] = {}
        self.max_distance = max_distance
        self.max_disappeared = max_disappeared
        self.bbox_smoothing = bbox_smoothing
        self.min_iou = min_iou
        self.class_switch_cost = class_switch_cost

    def process(self, packets: Sequence[FramePacket]) -> None:
        for packet in packets:
            detections = [
                detection
                for detection in packet.detections
                if detection.model_name == self.model_name
            ]
            tracker = self._trackers_by_source.setdefault(
                packet.name,
                CentroidTracker(
                    max_disappeared=self.max_disappeared,
                    max_distance=self.max_distance,
                    bbox_smoothing=self.bbox_smoothing,
                    min_iou=self.min_iou,
                    class_switch_cost=self.class_switch_cost,
                ),
            )
            skipped_models = packet.analytics_state.get("_skipped_detection_models") or []
            if self.model_name in skipped_models:
                for object_id in tracker.predict():
                    bbox = self._clip_bbox(list(tracker.origin_rects[object_id]), packet.frame)
                    if bbox is None:
                        continue
                    tracker.origin_rects[object_id] = bbox
                    detection = Detection(
                        bbox=bbox,
                        class_id=-1,
                        class_name=tracker.class_names.get(object_id, self.model_name),
                        confidence=float(tracker.confidences.get(object_id, 0.0)),
                        model_name=self.model_name,
                        draw=self.draw_predictions,
                        metadata={
                            self.metadata_key: object_id,
                            "track_hits": tracker.hits.get(object_id, 1),
                            "track_age": tracker.ages.get(object_id, 1),
                            "track_missed": tracker.disappeared.get(object_id, 0),
                            "track_centroid": tuple(int(value) for value in tracker.objects[object_id]),
                            "track_velocity": tuple(
                                float(value) for value in tracker.velocities.get(object_id, (0.0, 0.0))
                            ),
                            "predicted": True,
                        },
                    )
                    packet.detections.append(detection)
                continue
            matches = tracker.update(detections, class_aware=self.class_aware)
            for detection_index, object_id in matches.items():
                detection = detections[detection_index]
                detection.metadata[self.metadata_key] = object_id
                detection.metadata["track_hits"] = tracker.hits.get(object_id, 1)
                detection.metadata["track_age"] = tracker.ages.get(object_id, 1)
                detection.metadata["track_missed"] = tracker.disappeared.get(object_id, 0)
                detection.metadata["track_centroid"] = tuple(
                    int(value) for value in tracker.objects[object_id]
                )
                detection.metadata["track_velocity"] = tuple(
                    float(value) for value in tracker.velocities.get(object_id, (0.0, 0.0))
                )
                detection.metadata["raw_bbox"] = list(detection.bbox)
                clipped_bbox = self._clip_bbox(list(tracker.origin_rects[object_id]), packet.frame)
                if clipped_bbox is None:
                    continue
                detection.bbox = clipped_bbox

    @staticmethod
    def _clip_bbox(bbox, frame):
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = bbox
        x1 = max(0, min(width, int(x1)))
        y1 = max(0, min(height, int(y1)))
        x2 = max(0, min(width, int(x2)))
        y2 = max(0, min(height, int(y2)))
        if x2 <= x1 or y2 <= y1:
            return None
        return [x1, y1, x2, y2]
