#!/usr/bin/env python3
"""Native plate-output comparison using identical recording crops.

Run inside the image with PYTHONPATH including services/worker. Video CPU
decoding here is fixture preparation, not the production decoder path.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import sys

sys.path.insert(0, "/opt/traffic-pilot/services/worker")

import cv2
import numpy as np

from pipeline.dls_secondary import acquire_dls_inferencer
from detectors.backends.openvino_detector import letterbox


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--device", choices=("GPU", "NPU"), default="GPU")
    args = parser.parse_args()
    capture = cv2.VideoCapture(args.video)
    images = []
    for position in [0, 20, 40, 60, 80, 100, 120, 140]:
        capture.set(cv2.CAP_PROP_POS_FRAMES, position)
        ok, frame = capture.read()
        if not ok:
            raise RuntimeError(f"failed fixture frame {position}")
        h, w = frame.shape[:2]
        for crop in [frame, frame[h//3:, :w//2], frame[h//3:, w//2:]]:
            images.append(letterbox(crop, 224)[0])
    capture.release()
    handles = [acquire_dls_inferencer(
        "/models/traffic/openvino/license_plate.xml",
        "/opt/traffic-pilot/config/plate_raw_model_proc.json",
        width=224, height=224, device=args.device, name="plate-detector",
        expected_elements=1800, owner_id=f"fixture-{i}",
    ) for i in range(2)]
    results = [None] * len(images)
    started = time.perf_counter()

    def run_owner(owner):
        for index in range(owner, len(images), 2):
            results[index] = handles[owner].infer(images[index])

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_owner, owner) for owner in range(2)]
            for future in futures:
                future.result(timeout=90)
        np.savez(args.output, outputs=np.stack(results), inputs=np.stack(images))
        print(json.dumps({"samples": len(images), "device": args.device,
                          "seconds": time.perf_counter()-started}))
    finally:
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
