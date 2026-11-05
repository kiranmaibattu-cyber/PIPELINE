"""Finite local QAFT qualification; no model, gate or host-setting changes.

Continue only after explicit user authorization. Uses frozen images and the
existing native harness; all cases and failed attempts are retained.
"""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from analyze_opt80 import describe

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'localhost/sentry-v8-qaft-int8-model-candidate:2026.11.05'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    args = p.parse_args()
    root = args.root.resolve()
    recording = ROOT / 'optimization_evidence/2026-11-05-mog-partition/fixtures/marked-crowd-1080p30.mp4'
    ledger = json.loads((root / 'ledger.json').read_text()) if (root / 'ledger.json').exists() else []

    def checkpoint(phase, **details):
        (root / 'progress.json').write_text(json.dumps({'phase': phase,
            'updated_utc': datetime.now(timezone.utc).isoformat(), **details}, indent=2))
        (root / 'ledger.json').write_text(json.dumps(ledger, indent=2))

    def preserve(case):
        path = case / 'artifact-directory.txt'
        if path.exists() and not (case / 'raw-artifacts').exists():
            def copy(a, b):
                try:
                    os.link(a, b)
                except OSError:
                    shutil.copy2(a, b)
            shutil.copytree(path.read_text().strip(), case / 'raw-artifacts', copy_function=copy)

    def run(name, cams=1, fps=8, seconds=90, extra=(), source=None, image=IMAGE):
        existing = next((row for row in ledger if row['case'] == name), None)
        if existing is not None:
            return existing['marked_busy_rate_and_freshness_passed']
        case = root / name
        started = time.time()
        command = [sys.executable, str(ROOT / 'scripts/isolated_validation/opt80_compare.py'),
                   '--root', str(root), '--case', name, '--image', image,
                   '--cameras', str(cams), '--fps', str(fps), '--seconds', str(seconds),
                   '--startup-cpus', '4']
        command += list(source or ['--recording', str(recording)])
        command += list(extra)
        checkpoint('running-case', case=name, command=command)
        code = None
        adopted = (case / 'report.json').exists()
        if not (case / 'report.json').exists():
            if case.exists():
                raise RuntimeError(f'Incomplete case exists; inspect rather than overwrite: {case}')
            with (root / (name + '.log')).open('w') as log:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                         timeout=seconds + 300)
                code = process.returncode
        preserve(case)
        reportpath = case / 'report.json'
        report = json.loads(reportpath.read_text()) if reportpath.exists() else None
        summary = None
        if report and len([s for s in report['samples'] if s.get('sample_phase') == 'steady']) >= 2:
            summary = describe(case)
            (case / 'summary.json').write_text(json.dumps(summary, indent=2))
        good = bool(summary and not summary['fatal_lines']
                    and report['end_health'].get('camera_states', {}).get('healthy') == cams
                    and report['full_usecases_minimum_outputs_passed'])
        if summary:
            good = bool(good and len(summary['cameras']) == cams and
                        all(x['selected_fps'] >= .95 * fps and
                            x['completed_fps'] >= .95 * fps and
                            x['minimum_interval_selected_fps'] >= .90 * fps and
                            x['minimum_interval_completed_fps'] >= .90 * fps
                            for x in summary['cameras'].values()))
            # Quiet frames are completed policy work, not model inference.
            # Record each separately; do not mislabel a gated frame as a miss
            # or present a quiet pass as full model throughput.
            freshness = summary['scene_source_to_api_ms']
            good = bool(good and len(freshness) == cams and
                        all(x['samples'] >= 10 and x['p95'] <= 2000 for x in freshness.values()))
        good = bool(good and not (case / 'resource-abort.json').exists())
        row = {'case': name, 'camera_count': cams, 'fps': fps, 'seconds': seconds,
               'image': image, 'command': command, 'driver_returncode': code,
               'adopted_existing_result': adopted,
               'started_epoch': None if adopted else started,
               'finished_epoch': None if adopted else time.time(),
               'marked_busy_rate_and_freshness_passed': good,
               'raw_harness_passed': report.get('passed') if report else None,
               'report_present': report is not None}
        ledger.append(row)
        checkpoint('completed-case', case=name, result=row)
        print(json.dumps(row), flush=True)
        if (case / 'resource-abort.json').exists():
            raise RuntimeError('Resource guard aborted this test; stop the campaign')
        return good

    # Adopt already completed manual cases without rerunning them.
    for name, n in [('local-1x8-qaft-native-smoke', 1), ('local-4x8-qaft', 4),
                    ('local-8x8-qaft', 8), ('local-6x8-qaft', 6)]:
        if (root / name / 'report.json').exists():
            run(name, n, 8, 60 if n == 1 else 90)
    run('local-5x8-qaft', 5)
    four_at_ten = run('local-4x10-qaft', 4, 10)
    if four_at_ten:
        if run('local-6x10-qaft', 6, 10):
            run('local-8x10-qaft', 8, 10)
        else:
            run('local-5x10-qaft', 5, 10)
    else:
        if not run('local-3x10-qaft-startupcap4', 3, 10):
            run('local-2x10-qaft', 2, 10)
    run('local-4x8-opt79-control', 4, image='localhost/sentry-v8-opt79-shared-gait-background:latest')
    run('local-anpr-qaft', source=['--recording', '/home/admin1/Downloads/Usecase-Catalog-main/videos/ANPR.mp4'], extra=['--unmarked'])
    run('local-fire-clear-qaft', source=['--recording', str(root / 'fixtures/fire-clear.mp4')], extra=['--unmarked'])
    run('local-motion-transition-qaft', source=['--recording', str(root / 'fixtures/motion-transition.mp4')], extra=['--unmarked'])
    run('local-empty-qaft', seconds=60, source=['--recording', str(root / 'fixtures/empty1080.mp4')], extra=['--unmarked'])
    run('local-reconnect-qaft', 2, seconds=75,
        source=['--recording', str(ROOT / 'optimization_evidence/2026-11-05-mog-partition/fixtures/crowd-1080p.mp4')],
        extra=['--unmarked', '--outage-camera', '1', '--outage-after-seconds', '35', '--outage-seconds', '12'])
    run('local-agent503-qaft', 2, seconds=75, extra=['--agent-fault', '503', '--fault-start', '35', '--fault-end', '45'])
    run('local-agent-drop-ack-qaft', 2, seconds=75, extra=['--agent-fault', 'drop_ack', '--fault-start', '35', '--fault-end', '45'])
    urls = ['rtsps://gpu-box.burmilla-bass.ts.net:443/ch9',
            'rtsps://gpu-box.burmilla-bass.ts.net:443/traffic1',
            'rtsp://192.168.1.95:8554/traffic2']
    run('local-mixed-live-qaft', 3, seconds=90, source=['--source-urls-json', json.dumps(urls)])
    run('local-live-rtsps-h264-qaft', seconds=60,
        source=['--source-url', urls[1]], extra=['--startup-cpus', '2'])
    run('local-live-rtsp-hevc-qaft', seconds=60,
        source=['--source-url', urls[2]], extra=['--startup-cpus', '2'])
    run('local-live-rtsps-hevc1440-qaft', seconds=60,
        source=['--source-url', urls[0]], extra=['--startup-cpus', '2'])
    run('local-soak-2x8-qaft', 2, seconds=600)
    checkpoint('complete', cases=len(ledger), scope='Local shared host; no isolated hardware maximum claim')


if __name__ == '__main__':
    main()
