"""Summarize paired measurements without treating rolling FPS as capacity."""
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def describe(case):
    report = json.loads((case / 'report.json').read_text())
    snapshots = [json.loads(line) for line in (case / 'health-samples.jsonl').read_text().splitlines()]
    steady = [s for s in report['samples'] if s.get('sample_phase') == 'steady']
    beginning, end = steady[0]['sample_monotonic_seconds'], steady[-1]['sample_monotonic_seconds']
    # Health is captured inside the HTTP call, immediately before the summary.
    selected = [min(snapshots, key=lambda s: abs(s['monotonic'] - x['sample_monotonic_seconds'])) for x in steady]
    cams = {}
    for initial in selected[0]['health']['telemetry']['cameras']:
        key = initial['camera_id']
        sequence = [next(c for c in s['health']['telemetry']['cameras'] if c['camera_id'] == key) for s in selected]
        first, last = sequence[0], sequence[-1]
        elapsed = stamp(last['observed_at']) - stamp(first['observed_at'])
        rates = {}
        for field in ['decoded_frames', 'inference_frames', 'quiet_frames']:
            total = lambda c: c['processing']['totals'].get(field, 0)
            rates[field] = (total(last) - total(first)) / elapsed
        stages = {}
        for stage, final in last['use_case_processing'].items():
            start = first['use_case_processing'].get(stage, {})
            n = final['calls_total'] - start.get('calls_total', 0)
            if n <= 0:
                continue
            milliseconds = final['calls_total'] * final['lifetime_avg_ms'] - start.get('calls_total', 0) * start.get('lifetime_avg_ms', 0)
            stages[stage] = {'calls': n, 'mean_wall_ms_per_call': milliseconds / n,
                             'wall_ms_per_observed_second': milliseconds / elapsed}
        windows, selected_windows, completed_windows = [], [], []
        for a, b in zip(sequence, sequence[1:]):
            dt = stamp(b['observed_at']) - stamp(a['observed_at'])
            if dt > 0:
                windows.append((b['processing']['totals'].get('inference_frames', 0) - a['processing']['totals'].get('inference_frames', 0)) / dt)
                at, bt = a['processing']['totals'], b['processing']['totals']
                selected_windows.append((bt.get('decoded_frames', 0) - at.get('decoded_frames', 0)) / dt)
                completed_windows.append(((bt.get('inference_frames', 0) + bt.get('quiet_frames', 0)) -
                                          (at.get('inference_frames', 0) + at.get('quiet_frames', 0))) / dt)
        cams[key] = {'elapsed_snapshot_seconds': elapsed, 'selected_fps': rates['decoded_frames'],
                     'inference_fps': rates['inference_frames'],
                     'quiet_fps': rates['quiet_frames'],
                     'completed_fps': rates['inference_frames'] + rates['quiet_frames'],
                     'minimum_interval_selected_fps': min(selected_windows) if selected_windows else None,
                     'minimum_interval_completed_fps': min(completed_windows) if completed_windows else None,
                     'minimum_interval_inference_fps': min(windows) if windows else None,
                     'stages': stages}
    samples = [s['health']['telemetry'].get('resources', {}) for s in selected]
    latency = json.loads((case / 'latency.json').read_text())
    return {'image': report['image'], 'target_each': report['fps_per_camera'],
            'scope': 'Paired local nonisolated short runs; not a maximum hardware-capacity qualification.',
            'steady_seconds': end - beginning, 'cameras': cams,
            'harness_passed': report['passed'], 'fatal_lines': report['fatal_lines'],
            'handoff_final': report.get('end_health', {}).get('handoff'),
            'scene_source_to_api_ms': latency['steady_per_camera_ms'],
            'marker_checks': latency['counts'],
            'mean_container_cpu_cores': statistics.mean(s['container_cpu_cores'] for s in steady if s.get('container_cpu_cores') is not None),
            'mean_container_memory_mib': statistics.mean(s['container_memory_mb'] for s in steady if s.get('container_memory_mb') is not None),
            'package_summary': report['package_summary']}


if __name__ == '__main__':
    root = Path(sys.argv[1])
    result = {p.name: describe(p) for p in root.glob('local-*') if p.is_dir() and (p / 'latency.json').is_file()}
    (root / 'comparison.json').write_text(json.dumps(result, indent=2))
    for name, row in result.items():
        print(name, 'rates', [round(c['inference_fps'], 3) for c in row['cameras'].values()],
              'MOG ms', [round(c['stages'].get('person_identity.gait_background_update', {}).get('mean_wall_ms_per_call', 0), 2) for c in row['cameras'].values()],
              'scene p95 ms', [round(c['p95'], 1) if c['p95'] is not None else None for c in row['scene_source_to_api_ms'].values()],
              'CPU cores', round(row['mean_container_cpu_cores'], 3),
              'RAM MiB', round(row['mean_container_memory_mib'], 1))
