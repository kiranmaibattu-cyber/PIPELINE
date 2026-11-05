"""Offline CPU/F32 export reference versus deployed GPU fixed-one path.

This is a separate model-conversion diagnostic after performance tests, never
an inference fallback or part of the running CV worker.
"""
import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import openvino as ov

sys.path.insert(0, '/opt/traffic-pilot/services/worker')
sys.path.insert(0, '/checks')
from detectors.backends.openvino_detector import OpenVINOYOLODetector, letterbox
import detectors.backends.openvino_detector as detector_module
from qaft_gpu_probe import comparison, NAMES

p = argparse.ArgumentParser()
p.add_argument('--video', required=True)
p.add_argument('--output', required=True)
p.add_argument('--gpu-precision', choices=['default', 'f32'], default='default')
p.add_argument('--raw-gpu-input', action='store_true', help='Bypass GPU preprocessing; submit exact same float tensor as CPU')
args = p.parse_args()
cv2.setNumThreads(1)
cap = cv2.VideoCapture(args.video)
n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
canvases = []
for position in np.linspace(0, n - 2, 8).round().astype(int):
    cap.set(cv2.CAP_PROP_POS_FRAMES, position)
    ok, image = cap.read()
    assert ok
    canvases.append(letterbox(image, 640)[0])
cap.release()
prefix = '/models/traffic/openvino/vehicle_person_qaft_int8/'
core = ov.Core()
reference = core.compile_model(prefix + 'model.xml', 'CPU', {
    'INFERENCE_PRECISION_HINT': 'f32', 'NUM_STREAMS': '1', 'INFERENCE_NUM_THREADS': 2})
if args.gpu_precision == 'f32':
    actual_core = detector_module.shared_core()
    class PrecisionProbe:
        def __getattr__(self, name):
            return getattr(actual_core, name)
        def compile_model(self, model, device, options):
            return actual_core.compile_model(model, device, {**options, 'INFERENCE_PRECISION_HINT': 'f32'})
    detector_module.shared_core = lambda *a, **kw: PrecisionProbe()
gpu = OpenVINOYOLODetector(prefix + 'model_static_b1.xml', 'vehicle', device='GPU',
                         class_names=NAMES, batch_size=1)
raw_gpu = None
if args.raw_gpu_input:
    options = {'PERFORMANCE_HINT': 'LATENCY', 'NUM_STREAMS': '1'}
    if args.gpu_precision == 'f32':
        options['INFERENCE_PRECISION_HINT'] = 'f32'
    raw_gpu = core.compile_model(prefix + 'model_static_b1.xml', 'GPU', options)
rows = []
for canvas in canvases:
    tensor = np.ascontiguousarray(canvas[:, :, ::-1].transpose(2, 0, 1), dtype=np.float32)[None] / 255
    cpu = np.asarray(reference([tensor])[reference.output(0)])[0].copy()
    actual = (np.asarray(raw_gpu([tensor])[raw_gpu.output(0)])[0].copy()
              if raw_gpu is not None else gpu.infer_letterboxed(canvas)[0])
    rows.append(comparison(cpu, actual))
result = {'scope': 'Offline export reference only; no CV runtime CPU fallback. Run after capacity tests.',
          'gpu_floating_precision_hint': args.gpu_precision,
          'raw_gpu_input': args.raw_gpu_input,
          'reference_execution_devices': reference.get_property('EXECUTION_DEVICES'),
          'reference_floating_precision': 'f32 per supplied exporter', 'reference_cpu_threads': 2,
          'candidate_execution_devices': gpu.exec_devices,
          'comparisons': rows, 'passed': all(x['passed'] for x in rows)}
from collections import Counter
precisions = Counter()
for node in gpu._compiled.get_runtime_model().get_ops():
    info = node.get_rt_info()
    if 'runtimePrecision' in info:
        precisions[str(info['runtimePrecision'].value)] += 1
result['gpu_compiled_runtime_precision_nodes'] = dict(precisions)
Path(args.output).write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
raise SystemExit(0 if result['passed'] else 2)
