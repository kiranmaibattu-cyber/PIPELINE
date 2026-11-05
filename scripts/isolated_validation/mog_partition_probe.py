"""CPU-only feasibility probe: identical MOG2 updates on independent row bands.

Not a runtime implementation or a capacity benchmark. Run inside the frozen
image to use its exact OpenCV build. No detector/embedding inference is run.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from pathlib import Path
import time

import cv2
import numpy as np


class PartitionedMog:
    def __init__(self, executor, parts=4):
        self.executor, self.parts = executor, parts
        self.shape, self.models = None, []

    def apply(self, frame, learning_rate=-1):
        if self.shape != (frame.shape, frame.dtype.str):
            self.shape = (frame.shape, frame.dtype.str)
            self.edges = np.linspace(0, len(frame), min(self.parts, len(frame)) + 1).astype(int)
            self.models = [cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)
                           for _ in range(len(self.edges) - 1)]
        result = np.empty(frame.shape[:2], np.uint8)
        jobs = [self.executor.submit(model.apply, frame[a:b], result[a:b], learning_rate)
                for model, a, b in zip(self.models, self.edges, self.edges[1:])]
        for job in jobs:
            job.result()
        return result

    def background(self):
        return np.concatenate([model.getBackgroundImage() for model in self.models])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--frames', type=int, default=60)
    p.add_argument('--recording', type=Path)
    args = p.parse_args()
    cv2.setNumThreads(1)
    rng = np.random.default_rng(20261105)
    report = {'scope': 'MOG2 CPU per-call feasibility only; no inference or camera-capacity claim',
              'opencv': cv2.__version__, 'opencv_threads': cv2.getNumThreads(), 'workers': 4,
              'input': str(args.recording) if args.recording else 'synthetic varied learning rates', 'cases': []}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for height, width in [(720, 1280), (1080, 1920), (1440, 2560), (123, 197)]:
            native = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)
            candidate = PartitionedMog(pool)
            texture = rng.integers(0, 160, (height, width, 3), np.uint8)
            times = {'native': [], 'partitioned': []}
            failures = []
            capture = cv2.VideoCapture(str(args.recording)) if args.recording else None
            for i in range(args.frames):
                frame = texture.copy()
                left = i * max(1, width // 41) % width
                cv2.rectangle(frame, (left, 0), (min(width - 1, left + width // 5), height - 1),
                              (30 + i % 200, 190, 240), -1)
                rate = [0, .02, -1, 1][i % 4] if i % 11 == 0 else -1
                if capture is not None:
                    for _ in range(3):
                        ok, source = capture.read()
                        if not ok:
                            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            ok, source = capture.read()
                        assert ok, 'recording did not produce a frame'
                    frame = cv2.resize(source, (width, height))
                    rate = -1
                values = {}
                for kind in (['native', 'partitioned'] if i % 2 else ['partitioned', 'native']):
                    start = time.perf_counter()
                    values[kind] = (native.apply(frame, learningRate=rate) if kind == 'native'
                                    else candidate.apply(frame, rate))
                    elapsed = time.perf_counter() - start
                    if i >= 10:
                        times[kind].append(elapsed * 1000)
                if not np.array_equal(values['native'], values['partitioned']):
                    failures.append({'frame': i, 'kind': 'foreground',
                                     'pixels': int(np.count_nonzero(values['native'] != values['partitioned']))})
                if not np.array_equal(native.getBackgroundImage(), candidate.background()):
                    failures.append({'frame': i, 'kind': 'background'})
            report['cases'].append({'width': width, 'height': height, 'frames': args.frames,
                'failures': failures, 'timing_ms': {k: {'median': float(np.median(v)),
                'p95': float(np.percentile(v, 95))} for k, v in times.items()}})
            if capture is not None:
                capture.release()
    report['passed'] = not any(x['failures'] for x in report['cases'])
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
