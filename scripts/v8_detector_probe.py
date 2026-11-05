#!/usr/bin/env python3
"""Run the baked V8 vehicle/person detector on deterministic video frames."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cv2


CLASS_NAMES = {
    0: "bus",
    1: "car",
    2: "minivan",
    3: "motorcycle",
    4: "rickshaw",
    5: "truck",
    6: "bicycle",
    7: "pedestrian",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-every", type=int, default=5)
    parser.add_argument("--maximum-samples", type=int, default=40)
    parser.add_argument("--confidence", type=float, default=0.3)
    parser.add_argument(
        "--model",
        default="vehicle_yolo26n_fp16_dynamic",
        help="model key or absolute OpenVINO IR path",
    )
    args = parser.parse_args()

    from detectors.backends.openvino_detector import OpenVINOYOLODetector
    from detectors.base import openvino_model_path

    detector = OpenVINOYOLODetector(
        (
            args.model
            if os.path.isabs(args.model)
            else openvino_model_path(args.model)
        ),
        "vehicle",
        device=os.getenv("VEHICLE_DEVICE", "GPU"),
        imgsz=640,
        confidence=args.confidence,
        class_ids=set(CLASS_NAMES),
        class_names=CLASS_NAMES,
    )
    detector.warmup()
    capture = cv2.VideoCapture(args.video)
    if not capture.isOpened():
        raise RuntimeError(f"could not open video: {args.video}")
    records = []
    frame_index = 0
    try:
        while len(records) < args.maximum_samples:
            ok, frame = capture.read()
            if not ok:
                break
            frame_index += 1
            if (frame_index - 1) % max(1, args.sample_every):
                continue
            detections = detector.detect(frame)
            records.append({
                "frame_index": frame_index,
                "width": int(frame.shape[1]),
                "height": int(frame.shape[0]),
                "detections": [
                    {
                        "class_name": item.class_name,
                        "confidence": float(item.confidence),
                        "bbox": [float(value) for value in item.bbox],
                    }
                    for item in detections
                ],
            })
    finally:
        capture.release()
    payload = {
        "video": Path(args.video).name,
        "sample_every": max(1, args.sample_every),
        "sample_count": len(records),
        "frames": records,
    }
    Path(args.output).write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
