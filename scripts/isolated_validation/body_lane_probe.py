"""Exact NPU body-vector parity and bounded executor throughput, not capacity."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np
sys.path.insert(0, '/opt/traffic-pilot/services/worker')
from pipeline.dls_secondary import DlsBodyExtractor


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--video', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    cv2.setNumThreads(1)
    capture = cv2.VideoCapture(args.video)
    crops = []
    for i in range(8):
        capture.set(cv2.CAP_PROP_POS_FRAMES, i * 19)
        ok, frame = capture.read()
        assert ok
        h, w = frame.shape[:2]
        crops.extend([frame[h//5:, :w//2].copy(), frame[h//5:, w//2:].copy()])
    capture.release()
    reference = None
    rows = []
    for lanes in [1, 2, 2, 1]:
        os.environ['SENTINEL_DLS_BODY_LANES'] = str(lanes)
        handles = [DlsBodyExtractor('/models/surveillance/transreid_ssl_int8.xml',
                    '/opt/traffic-pilot/config/body_reid_model_proc.json', owner_id=f'camera-{i}') for i in range(2)]
        try:
            for handle in handles:
                handle.warmup()
            elapsed = []
            with ThreadPoolExecutor(max_workers=2) as pool:
                for repetition in range(10):
                    start = time.perf_counter()
                    jobs = [pool.submit(handle.embed, crops[i::2]) for i, handle in enumerate(handles)]
                    outputs = [job.result(timeout=30) for job in jobs]
                    elapsed.append((time.perf_counter() - start) * 1000)
                    vectors = np.stack(outputs)
                    if reference is None:
                        reference = vectors.copy()
                    np.testing.assert_array_equal(reference, vectors)
            rows.append({'lanes': lanes, 'vectors_per_repetition': len(crops),
                         'median_ms': float(np.median(elapsed[2:])),
                         'p95_ms': float(np.percentile(elapsed[2:], 95)),
                         'all_wall_ms': elapsed,
                         'output_sha256': hashlib.sha256(vectors.tobytes()).hexdigest()})
        finally:
            for handle in handles:
                handle.close()
    report = {'passed': True, 'scope': 'Two-owner shared DL Streamer NPU body execution only; CPU video decode prepares fixture crops, not production fallback.',
              'input_sha256': hashlib.sha256(b''.join(c.tobytes() for c in crops)).hexdigest(),
              'body_prefetch': os.getenv('SENTINEL_DLS_BODY_PREFETCH', '1'),
              'comparisons': rows}
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
