"""Recompute true inter-snapshot rates, rather than percentile of rolling FPS.

The original harness's pass flag is retained separately. Its last rolling
samples do not replace the campaign's all-10-second-window criterion.
"""
from pathlib import Path
import collections, datetime, gzip, json, sys
import numpy as np

root = Path(sys.argv[1])
out = []
for row in json.loads((root / 'ledger.json').read_text()):
    path = root / 'artifacts' / row['case']
    reportpath = path / 'report.json'
    if not reportpath.exists():
        continue
    report = json.loads(reportpath.read_text())
    samples = [s for s in report['samples'] if s.get('sample_phase') == 'steady']
    if len(samples) < 2:
        continue
    targets = {k: row['config']['fps'] for k in samples[0]['per_camera']}
    configpath = path / 'config' / 'desired_state.json'
    if configpath.exists():
        desired = json.loads(configpath.read_text())
        targets = {c['camera_id']: c.get('fps', 8) for c in desired['cameras']}
    # A cached counter snapshot can be 2 seconds older than its HTTP poll.
    # Match the exact counter tuple to independent monitor records and use
    # the camera's own snapshot timestamp as the denominator.
    keys = ['decoded_frames', 'inference_frames', 'quiet_frames']
    lookup = collections.defaultdict(list)
    monitor = path / 'monitor.jsonl.gz'
    if monitor.exists():
        with gzip.open(monitor, 'rt') as f:
            for line in f:
                m = json.loads(line)
                for c in m.get('health', {}).get('telemetry', {}).get('cameras', []):
                    totals = c.get('processing', {}).get('totals', {})
                    if c.get('observed_at'):
                        key = (c['camera_id'], *(totals.get(k, 0) for k in keys))
                        lookup[key].append((m['monotonic'], c['observed_at'], c.get('started_at')))
    intervals = collections.defaultdict(list); unavailable = collections.Counter()
    def match(s, cam):
        totals = s['per_camera'][cam]['totals']
        key = (cam, *(totals.get(k, 0) for k in keys))
        matches = lookup.get(key, [])
        if not matches: return None
        item = min(matches, key=lambda x: abs(x[0]-s['sample_monotonic_seconds']))
        if abs(item[0]-s['sample_monotonic_seconds']) > 3: return None
        return item
    for a, b in zip(samples, samples[1:]):
        for cam in a['per_camera'].keys() & b['per_camera'].keys():
            first, last = match(a, cam), match(b, cam)
            if first is None or last is None:
                unavailable[cam] += 1; continue
            if first[2] != last[2]: unavailable[cam] += 1; continue
            dt = (datetime.datetime.fromisoformat(last[1].replace('Z','+00:00'))-
                  datetime.datetime.fromisoformat(first[1].replace('Z','+00:00'))).total_seconds()
            if not 8 <= dt <= 12:
                unavailable[cam] += 1; continue
            av, bv = a['per_camera'][cam]['totals'], b['per_camera'][cam]['totals']
            changes = {key: bv.get(key, 0) - av.get(key, 0) for key in keys}
            if any(v < 0 for v in changes.values()):
                unavailable[cam] += 1; continue
            intervals[cam].append({'at_monotonic': b['sample_monotonic_seconds'], 'seconds': dt,
                                   **{key: value / dt for key, value in changes.items()}})
    cameras = {}
    for cam, values in intervals.items():
        target = targets.get(cam, row['config']['fps'])
        cameras[cam] = {'target_fps': target, 'windows': len(values), 'intervals': values, 'unavailable_windows': unavailable[cam],
                        'snapshot_interval_seconds': [min(v['seconds'] for v in values), max(v['seconds'] for v in values)]}
        for key in ['decoded_frames', 'inference_frames', 'quiet_frames']:
            rates = [v[key] for v in values]
            cameras[cam][key] = {name: float(np.percentile(rates, q)) for name, q in [('min', 0), ('p50', 50), ('p95', 95), ('max', 100)]}
        cameras[cam]['active_windows_below_90pct'] = sum(v['inference_frames'] < .9 * target for v in values)
        cameras[cam]['selected_windows_below_90pct'] = sum(v['decoded_frames'] < .9 * target for v in values)
        cameras[cam]['motion_accounted_windows_below_90pct'] = sum(v['inference_frames'] + v['quiet_frames'] < .9 * target for v in values)
        first,last=match(samples[0],cam),match(samples[-1],cam)
        if first is not None and last is not None and first[2]==last[2]:
            seconds=(datetime.datetime.fromisoformat(last[1].replace('Z','+00:00'))-datetime.datetime.fromisoformat(first[1].replace('Z','+00:00'))).total_seconds()
            delta={k:samples[-1]['per_camera'][cam]['totals'].get(k,0)-samples[0]['per_camera'][cam]['totals'].get(k,0)for k in keys}
            if seconds>0 and all(v>=0 for v in delta.values()):
                cameras[cam]['cumulative_fps']={k:v/seconds for k,v in delta.items()}
                cameras[cam]['cumulative_interval_seconds']=seconds
                cameras[cam]['cumulative_active_95pct_passed']=delta['inference_frames']/seconds>=.95*target
    invalid = [name for name in ['invalid-test-configuration.json', 'invalid-test-fixture.json', 'power-transition.json'] if (path / name).exists()]
    complete = bool(cameras) and set(cameras) == set(samples[0]['per_camera']) and not any(unavailable.values())
    out.append({'case': row['case'], 'harness_passed': row.get('harness_passed'),
                'invalid_markers': invalid, 'cameras': cameras,
                'unavailable_windows': dict(unavailable),
                'all_active_windows_passed': all(c['active_windows_below_90pct'] == 0 for c in cameras.values()) if complete else None,
                'all_motion_accounted_windows_passed': all(c['motion_accounted_windows_below_90pct'] == 0 for c in cameras.values()) if complete else None})
(root / 'window-rate-audit.json').write_text(json.dumps({'definition': 'Counter deltas across steady camera snapshots 8–12 seconds apart, '
    'matched by exact counter tuple to 1Hz independent records. Denominator uses camera observed_at, not HTTP poll time. '
    'Unavailable/reset/too-wide intervals remain unqualified rather than fabricated FAIL or PASS. '
    'Quiet frames are intentional policy skips, separate from active inference. '
    'Counter-reset intervals are excluded. Fault cases are not capacity qualification.', 'cases': out}, indent=2))
print(json.dumps({'cases': len(out), 'output': str(root / 'window-rate-audit.json')}))
