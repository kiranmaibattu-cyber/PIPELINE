"""GPU-only QAFT static/dynamic batch and source-coordinate diagnostics.

Video decoding here prepares identical fixture inputs. Production decode is
qualified separately through the native VA pipeline. No device fallback.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, '/opt/traffic-pilot/services/worker')
from detectors.backends.openvino_detector import OpenVINOYOLODetector, letterbox, yolo_detections

NAMES = dict(enumerate(['bus', 'car', 'minivan', 'motorcycle', 'rickshaw', 'truck', 'bicycle', 'pedestrian', 'others']))


def comparison(a, b):
    left = a[a[:, 4] >= .3]
    right = b[b[:, 4] >= .3]
    used = set()
    matches = []
    for item in left:
        choices = []
        for index, other in enumerate(right):
            if index in used or int(item[5]) != int(other[5]):
                continue
            lo, hi = np.maximum(item[:2], other[:2]), np.minimum(item[2:4], other[2:4])
            intersection = float(np.maximum(0, hi - lo).prod())
            area = float(np.maximum(0, item[2:4] - item[:2]).prod() + np.maximum(0, other[2:4] - other[:2]).prod())
            iou = intersection / max(1e-9, area - intersection)
            choices.append((iou, index))
        if choices:
            iou, index = max(choices)
            if iou >= .95:
                used.add(index)
                matches.append({'iou': iou, 'confidence_delta': float(abs(item[4] - right[index, 4])),
                                'input_bbox_delta_px': float(np.max(np.abs(item[:4] - right[index, :4])))})
    passed = (len(matches) == len(left) == len(right)
              and all(x['confidence_delta'] <= .01 and x['input_bbox_delta_px'] <= 2 for x in matches))
    return {'passed': passed, 'reference_detections': len(left), 'detections': len(right),
            'matched': len(matches), 'matches': matches,
            'raw_max_abs_difference': float(np.max(np.abs(a - b)))}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--video', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--batch-mode', choices=['dynamic', 'fixed'], default='dynamic')
    args = p.parse_args()
    cv2.setNumThreads(1)
    report = {'scope': 'GPU model execution only; not native pipeline capacity or labelled accuracy',
              'batch_mode': args.batch_mode,
              'thresholds': {'detection_confidence': .3, 'match_iou': .95, 'max_confidence_delta': .01, 'max_input_bbox_delta_px': 2},
              'cases': []}
    capture = cv2.VideoCapture(args.video)
    canvases, transforms = [], []
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if frame_count < 8:
        raise RuntimeError(f'fixture contains only {frame_count} frames')
    positions = np.linspace(0, frame_count - 2, 8).round().astype(int)
    for position in positions:
        capture.set(cv2.CAP_PROP_POS_FRAMES, position)
        ok, frame = capture.read()
        if not ok:
            raise RuntimeError(f'missing fixture frame {position}')
        canvas, scale, px, py = letterbox(frame, 640)
        canvases.append(canvas)
        transforms.append((frame.shape[:2], scale, px, py))
    capture.release()
    report['input_canvases'] = len(canvases)
    settings = dict(model_name='vehicle', device='GPU', imgsz=640, confidence=.3,
                    class_ids=set(range(8)), class_names=NAMES)
    prefix = '/models/traffic/openvino/vehicle_person_qaft_int8/'
    try:
        fixed = OpenVINOYOLODetector(prefix + 'model_static_b1.xml', batch_size=1, **settings)
        fixed.warmup()
        report['static_execution_devices'] = fixed.exec_devices
        if 'CPU' in fixed.exec_devices or 'GPU' not in fixed.exec_devices:
            raise RuntimeError('unexpected primary execution device')
        reference = [fixed.infer_letterboxed(c)[0] for c in canvases]
        report['reference_detection_counts'] = [int((x[:, 4] >= .3).sum()) for x in reference]
        dynamic = (OpenVINOYOLODetector(prefix + 'model.xml', batch_size=None, **settings)
                   if args.batch_mode == 'dynamic' else None)
        for batch in [1, 2, 3, 4, 8]:
            executor = (dynamic if dynamic is not None else
                        OpenVINOYOLODetector(prefix + 'model.xml', batch_size=batch, **settings))
            if 'CPU' in executor.exec_devices or 'GPU' not in executor.exec_devices:
                raise RuntimeError('unexpected execution device')
            precision = Counter()
            for node in executor._compiled.get_runtime_model().get_ops():
                info = node.get_rt_info()
                if 'runtimePrecision' in info:
                    precision[str(info['runtimePrecision'].value)] += 1
            inputs = np.stack(canvases[:batch])
            times = []
            result = None
            for repetition in range(4):
                start = time.perf_counter()
                result = executor.infer_letterboxed_batch(inputs)
                if repetition:
                    times.append((time.perf_counter() - start) * 1000)
            if result.shape != (batch, 300, 6) or not np.isfinite(result).all():
                raise RuntimeError(f'invalid batch output {batch}: {result.shape}')
            rows = [comparison(reference[i], result[i]) for i in range(batch)]
            geometry = []
            for i in range(batch):
                shape, scale, px, py = transforms[i]
                detections = yolo_detections(result[i:i+1], image_shape=shape, scale=scale,
                    pad_x=px, pad_y=py, confidence=.3, class_ids=set(range(8)), class_names=NAMES, model_name='vehicle')
                assert all(0 <= d.bbox[0] < d.bbox[2] <= shape[1] and 0 <= d.bbox[1] < d.bbox[3] <= shape[0] for d in detections)
                geometry.append({'source_shape': list(shape), 'detections': len(detections),
                                 'class_counts': dict(Counter(d.class_name for d in detections))})
            report['cases'].append({'batch': batch, 'shape': list(result.shape),
                'execution_devices': executor.exec_devices,
                'compiled_runtime_precision_nodes': dict(precision),
                'median_inference_ms': float(np.median(times)), 'sample_ms': times,
                'comparisons': rows, 'source_geometry': geometry,
                'passed': all(r['passed'] for r in rows)})
            Path(args.output).write_text(json.dumps(report, indent=2))
        report['passed'] = all(x['passed'] for x in report['cases'])
    except Exception as error:
        report['passed'] = False
        report['error'] = {'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        Path(args.output).write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
