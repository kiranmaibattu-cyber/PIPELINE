"""Archive a factual local QAFT qualification report from completed evidence."""
from collections import Counter
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import shutil
import statistics
import sys

import numpy as np
from analyze_opt80 import describe

root = Path(sys.argv[1]).resolve()
repo = Path(__file__).resolve().parents[2]


def stats(values):
    return {'samples': len(values), **{k: float(np.percentile(values, q)) if values else None
        for k, q in [('min', 0), ('p50', 50), ('p95', 95), ('max', 100)]}}


cases = {}
for case in sorted(root.glob('local-*')):
    path = case / 'report.json'
    if not case.is_dir() or not path.exists():
        continue
    report = json.loads(path.read_text())
    steady = [s for s in report['samples'] if s.get('sample_phase') == 'steady']
    summary = describe(case) if len(steady) >= 2 and (case / 'latency.json').exists() else None
    item = {'report': report, 'summary': summary, 'resource_aborted': (case / 'resource-abort.json').exists()}
    rows = []
    monitor = case / 'resource-monitor.jsonl.gz'
    if monitor.exists():
        with gzip.open(monitor, 'rt') as f:
            rows = [json.loads(line) for line in f]
    first = steady[0]['sample_monotonic_seconds'] if steady else float('inf')
    last = steady[-1]['sample_monotonic_seconds'] if steady else -float('inf')
    selected = [r for r in rows if first <= r['monotonic'] <= last]
    temperatures = [max(x['current'] for x in r.get('temperatures', {}).get('coretemp', []))
                    for r in selected if r.get('temperatures', {}).get('coretemp')]
    alltemperatures = [max(x['current'] for x in r.get('temperatures', {}).get('coretemp', []))
                       for r in rows if r.get('temperatures', {}).get('coretemp')]
    memories = [(r['monotonic'], (((r.get('health') or {}).get('telemetry') or {}).get('system') or {}).get('container_cgroup', {}).get('memory_current_mb')) for r in selected]
    # The legacy *_mb field divides by1024²; these values are already MiB.
    memories = [(t, float(v)) for t, v in memories if v is not None]
    item['resource'] = {'steady_max_core_temperature_c': stats(temperatures),
        'including_startup_max_core_temperature_c': stats(alltemperatures),
        'steady_memory_mib': stats([v for _, v in memories])}
    item['resource']['npu_node_busy_percent'] = stats([
        npu['hardware_utilization_pct'] for r in selected
        for npu in [(((r.get('health') or {}).get('telemetry') or {}).get('accelerators') or {}).get('NPU', {})]
        if npu.get('hardware_measurement_status') == 'measured' and npu.get('hardware_utilization_pct') is not None])
    if memories:
        late = [(t, v) for t, v in memories if t >= memories[-1][0] - 300]
        item['resource'].update(memory_first_mib=memories[0][1], memory_last_mib=memories[-1][1],
            memory_last_five_minute_slope_mib_per_minute=float(np.polyfit(
                [t - late[0][0] for t, v in late], [v for t, v in late], 1)[0] * 60) if len(late) > 2 else None)
    def cpu_stat(r):
        return dict((a, int(b)) for a, b in (s.split() for s in r.get('container_cpu_stat', '').splitlines()))
    cpu = [r for r in selected if r.get('container_cpu_stat')]
    if len(cpu) >= 2:
        a, b = cpu_stat(cpu[0]), cpu_stat(cpu[-1])
        item['resource']['steady_cgroup_mean_cpu_cores'] = (b['usage_usec'] - a['usage_usec']) / 1e6 / (cpu[-1]['monotonic'] - cpu[0]['monotonic'])
        item['resource']['steady_throttled_periods_delta'] = b.get('nr_throttled', 0) - a.get('nr_throttled', 0)
    signed, output_counts = [], Counter()
    packages = case / 'raw-artifacts/packages.jsonl'
    if packages.exists():
        for line in packages.open():
            p = json.loads(line)
            output_counts[p['event_type']] += 1
            if p['event_type'] == 'scene_sample' and 'accepted_at' in p:
                signed.append((datetime.fromisoformat(p['accepted_at']).timestamp() -
                               datetime.fromisoformat(p['observed_at'].replace('Z', '+00:00')).timestamp()) * 1000)
    item['signed_scene_receipt_minus_observed_ms'] = stats(signed)
    item['accepted_event_types'] = dict(output_counts)
    cases[case.name] = item

# Node-wide GPU measurements aligned approximately to host camera snapshots.
gpu = []
data = (root / 'gpu-engines.json').read_text()
decoder = json.JSONDecoder(); offset = 0; elapsed = 0
anchor = json.loads((root / 'gpu-monitor-start.json').read_text()).get('process_created_epoch')
while offset < len(data):
    while offset < len(data) and (data[offset].isspace() or data[offset] in ',[]'):
        offset += 1
    if offset == len(data):break
    try:item, offset = decoder.raw_decode(data, offset)
    except json.JSONDecodeError:break
    elapsed += item['period']['duration'] / 1000
    gpu.append({'utc_epoch': anchor + elapsed, 'engines': {k: x['busy'] for k, x in item.get('engines', {}).items()}})
(root / 'gpu-engine-timeline.json').write_text(json.dumps(gpu, separators=(',', ':')))
for name, item in cases.items():
    samples = root / name / 'health-samples.jsonl'
    steady = [s for s in item['report']['samples'] if s.get('sample_phase') == 'steady']
    if not steady or not samples.exists():continue
    snapshots = [json.loads(line) for line in samples.open()]
    a = min(snapshots, key=lambda r: abs(r['monotonic'] - steady[0]['sample_monotonic_seconds']))
    b = min(snapshots, key=lambda r: abs(r['monotonic'] - steady[-1]['sample_monotonic_seconds']))
    def wall(r):
        return statistics.mean(datetime.fromisoformat(c['observed_at'].replace('Z', '+00:00')).timestamp()
                               for c in r['health']['telemetry']['cameras'])
    start, end = wall(a), wall(b)
    selected = [r for r in gpu if start <= r['utc_epoch'] <= end]
    item['gpu_node_engines'] = {k: stats([r['engines'][k] for r in selected if k in r['engines']])
                               for k in sorted({k for r in selected for k in r['engines']})}

result = {'scope': 'Local nonisolated frozen-image qualification, 2026-11-05. No maximum hardware-capacity/accuracy claim.',
          'image': 'localhost/sentry-v8-qaft-int8-model-candidate:2026.11.05',
          'image_id': 'd035cb44df82a66cbcdaa29e3ca22177f7bfdbf080009d6a27bf6633e5eb2127',
          'cases': cases, 'qualification': 'NOT PRODUCTION QUALIFIED: GPU/export reference mismatch, dynamic GPU failure, inherited UTC clock defect, thermal and capacity limits'}
(root / 'results.json').write_text(json.dumps(result, indent=2))

lines = ['# QAFT INT8 image validation — 2026-11-05', '',
    '**Decision: the model is baked in and the native pipeline runs, but this candidate is not production-qualified.** GPU/export-reference fidelity and the inherited timestamp defect remain unresolved. Testing did not change the image, V8 contract or production gates.', '',
    f"Image: `{result['image']}`. ID: `{result['image_id']}`. Local only; no registry publication.", '',
    '## Scope and test conditions', '',
    f'{len(cases)} native cases recorded, including intentional faults and excluded thermal attempts. 93 applicable runtime/policy tests passed; one Opt80-only test was deliberately deselected because this image is based on Opt79. The unpromoted Opt80 gait experiment is absent.', '',
    'Intel Core Ultra 9 285H, 16 logical CPUs. Other host programs remained running. Remote192.168.1.64 was not used for tests. Early cases used the existing stock setup. Later cases cap only the test container to four CPU cores during loading/warmup (two for individual live sources), then restore all sixteen cores before steady measurement and verify the actual kernel quota. No host power/turbo/fan/service changes. Thermal guards stopped only test containers. These are conditional local results, not isolated machine capacity.', '',
    '## Model and GPU findings', '',
    '- Source QAFT INT8/mixed-precision checkpoint and trained quantizers were retained. Dynamic and static-one weight BIN hashes are identical; 86 FakeQuantize nodes remain. Class order, source-coordinate preprocessing/postprocessing and detector confidence gates were not changed.',
    '- The active native graph uses GPU fixed batch one, eight requests and VA-surface sharing. Models supporting batch dimensions do not automatically make this graph a cross-camera batch scheduler.',
    '- GPU fixed batches 1, 2, 3, 4 and 8 produced mutually consistent per-image detections within the recorded tolerances. This checks batching consistency, not agreement with the source model on another backend.',
    '- Fully dynamic GPU execution compiled but produced zero accepted detections where fixed-one and CPU/F32 found objects. It failed parity at tested batches1/2/3/4/8. Do not enable the experimental fully dynamic GPU route.',
    '- Offline conversion diagnostic: the default fixed-one GPU path differed from CPU/F32 using the supplied exporter settings, by up to about0.199 confidence; counts differed on two of eight frames. GPU/F32 floating precision reduced the maximum confidence discrepancy to about0.053 but still changed detections and failed the predefined fidelity checks. Both GPU configurations retained quantized execution nodes. The CPU comparison was a separate diagnostic after performance tests, never a runtime fallback. It uses image OpenVINO2026.2; the supplied exporter documented2026.0. No labelled accuracy claim is made.',
    '- No precision hint, model recalibration, GPU/CPU fallback or rebuild was applied to the tested image. GPU fidelity needs exporter/device validation; do not loosen event gates to conceal it.', '',
    'Further isolation used the exact same float input on GPU/F32 and CPU/F32 and still failed the chosen confidence/detection tolerance. CPU/F32 fixed-one versus CPU/F32 dynamic-one also differed (maximum matched confidence delta about0.069). The issue is therefore not established as GPU-only or image preprocessing. Shape/backend numerical fidelity needs investigation; no corrupt weight-file claim is justified. The source IR reshaped in memory and serialized fixed-one were already consistent on GPU.', '',
    '## Capacity on the marked1080p crowd workload', '',
    'All principal cases enable all use cases and scene sampling every two seconds for every camera. The table separates source-frame selection from actual primary inference. Quiet-mode policy work is accounted separately. Counter rates use each camera snapshot time, not just a final rolling FPS sample. Steady holds are90seconds except the600second soak; measured first-to-last counter spans exclude one sample interval.', '',
    '| Case | Selected FPS/camera | Primary inference FPS/camera | Worst scene p95 | Result |',
    '|---|---:|---:|---:|---|']
labels = {'local-4x8-qaft': 'Short pass at4×8', 'local-5x8-qaft': 'Marginal; window dips',
          'local-6x8-qaft': 'Fails throughput/freshness', 'local-8x8-qaft': 'Fails throughput/freshness',
          'local-4x10-qaft': 'Marginal; window dips', 'local-3x10-qaft-startupcap4': 'Short pass at3×10',
          'local-soak-2x8-qaft': 'Ten-minute pass at2×8'}
for name, label in labels.items():
    x = cases[name]['summary']; c = list(x['cameras'].values()); sf = [a['selected_fps'] for a in c]; inf = [a['inference_fps'] for a in c]
    latency = max(a['p95'] for a in x['scene_source_to_api_ms'].values()) / 1000
    lines.append(f'| {name.removeprefix("local-")} | {min(sf):.2f}–{max(sf):.2f} | {min(inf):.2f}–{max(inf):.2f} | {latency:.3f}s | {label} |')
lines += ['', 'Practical reading: four cameras at8FPS and three at10FPS passed short local tests; five at8 and four at10 were marginal despite near-target averages. Six/eight at8 clearly failed. Only two at8 were held for ten minutes. These findings do not establish a universal camera maximum, continuous24/7 rating or the earlier goal of15–20 busy cameras. Input object density, enabled analytics, motion admission and available host resources affect work.', '',
    'An original three-camera startup, a fresh Opt79 four-camera control and the mixed three-live-camera attempt hit the thermal guard. They are excluded from qualification. The Opt79 abort means this campaign did not obtain a clean fresh baseline comparison, so do not attribute all throughput differences to INT8.', '',
    '## Sustained resources (two cameras at8FPS)', '']
soak = cases['local-soak-2x8-qaft']
resource = soak['resource']
fps_text = ', '.join(format(c['selected_fps'], '.3f') for c in soak['summary']['cameras'].values())
lines += [f"Matched counter duration: {soak['summary']['steady_seconds']:.1f}seconds within the600second hold. Selected FPS {fps_text}; inference and quiet frames are separate in results.json."]
lines += [f"Container memory: {resource.get('memory_first_mib', 0):.1f}→{resource.get('memory_last_mib', 0):.1f}MiB; last-five-minute slope {resource.get('memory_last_five_minute_slope_mib_per_minute', 0):.2f}MiB/min. A short run does not establish a leak-free long deployment.",
    f"Mean CPU from kernel cgroup deltas: {resource.get('steady_cgroup_mean_cpu_cores', 0):.3f}cores. Steady throttled-period delta: {resource.get('steady_throttled_periods_delta')}. Maximum core temperature p95/max: {resource['steady_max_core_temperature_c']['p95']}/{resource['steady_max_core_temperature_c']['max']}°C.",
    f"Node NPU busy p50/p95: {resource['npu_node_busy_percent']['p50']}/{resource['npu_node_busy_percent']['p95']}%. GPU engine p50/p95: " + '; '.join(f"{k} {v['p50']:.2f}/{v['p95']:.2f}%" for k, v in soak.get('gpu_node_engines', {}).items() if v['p50'] is not None), '',
    '**The soak passed rates and marked freshness, but was not thermally clean:** temperature reached the critical range in intermittent bursts, although the guard did not see five consecutive critical samples. Memory also grew. These measurements do not qualify sustained production operation. See [resource plots](validation/soak-resources.png).', '',
    '## Functional and delivery results', '',
    '- ANPR recording:76plate reads,85explicit outcomes and40line-cross observations, with evidence. This proves paths execute, not correct plate text. Its raw FPS harness flag failed a short rolling window despite mean selection near7.92FPS.',
    '- Fire followed by normal footage:two suspected episodes andtwo clear transitions. This is model-observation execution, not proof of a real incident or safety.',
    '- Motion transition:647quiet and264inference frames in the captured run. Quiet/active operation was exercised. The empty-frame fixture stayed active because low-score raw smoke detections can hold inference active even below the final hazard alert gate; do not equate visual quiet with zero model detections.',
    '- Positive zone coverage: a final central-band desired-state polygon on the ANPR recording produced13zone-entry and39zone-exit events, plus presence/episode/count records. More exits than entries is possible because subjects first observed already inside establish an unknown-entry episode; track loss is not automatically an exit. This was a configuration fixture, not a gate change or capacity test. No separate dwell-milestone event is claimed; dwell support is through zone episodes and their observed-inside timestamps.',
    '- Twelve-second TCP camera outage:target interrupted and recovered, with its peer healthy. This tests source-outage isolation, not recovery from every native GPU crash.',
    '- Lost acknowledgements:88duplicate acknowledgements, zero exhausted retries/protocol errors at sampled cutoff. Accepted package IDs and payload bytes were reused.',
    '- Ten-second503fault:four packages exhausted the contract-defined six-attempt budget;99backpressure andtwo coalescing counts. The V8 contract explicitly permits reported exhaustion and memory release without claiming production. This does not mean previously accepted snapshots were lost; it does mean not all observations are guaranteed delivery while the agent is unavailable.',
    '- /healthz200, /metrics200 and /healthy404 verified. Decode remained VAAPI/DLStreamer; primary/scene/fire/face detection used GPU, face embedding/pose/body/gait/plate used NPU, OCR was configured MULTI:GPU,NPU. CPU tracking and preprocessing remain normal work. No runtime model/decode CPU fallback was introduced.', '',
    '## Live inputs', '', '| Source | Selected FPS | Observed result |', '|---|---:|---|']
for name, source in [('local-live-rtsps-h264-qaft', 'RTSPS traffic1,1080p H.264'),
                     ('local-live-rtsp-hevc-qaft', 'RTSP traffic2,1080p HEVC'),
                     ('local-live-rtsps-hevc1440-qaft', 'RTSPS ch9,1440p HEVC')]:
    x = cases[name]['summary']; sf = next(iter(x['cameras'].values()))['selected_fps']
    lines.append(f'| {source} | {sf:.2f} | Frames, events and evidence received; raw harness {"PASS" if x["harness_passed"] else "FAIL"} |')
lines += ['', 'Live sources were unmarked, so source-to-API freshness was not independently established. ch9 exceeded the nominal selection target during its short window; startup catch-up/source timing was not resolved. A later compressed-packet-only test measured approximately18packets/s for both traffic feeds and25forch9, versus advertised25FPS. This was not simultaneous with CV and cannot by itself attribute the earlier shortfall to the source or inference. The first FFmpeg progress diagnostic had no frame-count field and yielded no usable rate; the corrected FFprobe packet diagnostic is archived. ExternalTCP192.168.1.92:9001timed out during preflight; local independent TCP replays worked.', '',
    '## Timestamps, artifacts and remaining restrictions', '',
    'Independent frame markers measure first-video-PES submission to the agent API, excluding physical camera capture/encoding and work after the API. They show fresh images in passing marked cases. Absolute observed UTC still has the inherited mapping defect: one-camera scenes were dated about5.77seconds after receipt. Runtime zero-clamped source-age values must not be presented as correct end-to-end latency. Correct frame associations do not repair absolute timestamps.', '',
    'The API boundary receiver validates schemas, multipart hashes/sizes, promised IDs and associations. It is not the production agent database and does not test downstream Management retrieval. The saved-gallery audit separately verifies JPEG dimensions/source bounds, embedding dimensions/L2 normalization and parent/frame/presence links. See qaft-only-gallery-audit.json; the all-case audit also includes the old-model control and must not be mislabeled QAFT-only.', '',
    'GPU engine counters are node-wide and overlap; do not sum them or attribute all activity to this container. Their alignment is approximate. NPU busy metrics are node-scoped. Parent/child stage wall times overlap and are not additive kernel timings. Full per-case resource, GPU, source clock, output and stage data is in results.json.', '',
    'Do not promote this image as fully validated. Remaining issues include GPU conversion fidelity, fully dynamic GPU execution, absolute source UTC, marginal higher camera loads, thermal restrictions and inherited admission/Secret-rotation/HTTP-native/telemetry issues already documented in ISOLATED_VALIDATION_REPORT.md. No models, event gates or contract fields were changed to improve test scores.', '',
    '## Evidence', '',
    f'This directory contains{len(cases)}case records, raw artifacts, exact invocations, health snapshots, one-second resource traces, source epochs, marker latencies, schema/policy test log, model/probe comparisons and GPU engine data. Cold-start quota verification and excluded attempts are retained. Models/image IDs are unchanged from update.json. No registry push was requested or performed.', '']
out = root.parent / 'VALIDATION_REPORT.md'; out.write_text('\n'.join(lines))
print(json.dumps({'cases': len(cases), 'report': str(out), 'results': str(root / 'results.json')}))
