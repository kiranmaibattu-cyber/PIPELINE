"""Local paired native tests; unchanged use cases and emission policy.

Uses a frozen copy of the completed validation harness/receiver, with only the
machine-specific fixture mount removed. Other host programs remain running;
these results must not be advertised as isolated hardware capacity.
"""
import argparse
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
from analyze_markers import analyze


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    sources = p.add_mutually_exclusive_group(required=True)
    sources.add_argument('--recording', type=Path)
    sources.add_argument('--source-url')
    sources.add_argument('--source-urls-json')
    p.add_argument('--case', required=True)
    p.add_argument('--image', required=True)
    p.add_argument('--cameras', type=int, required=True)
    p.add_argument('--fps', type=int, required=True)
    p.add_argument('--seconds', type=int, default=90)
    p.add_argument('--unmarked', action='store_true')
    p.add_argument('--outage-camera', type=int)
    p.add_argument('--outage-after-seconds', type=int, default=20)
    p.add_argument('--outage-seconds', type=int, default=12)
    p.add_argument('--agent-fault', choices=['503', '429', 'drop_ack', 'slow'])
    p.add_argument('--fault-start', type=int, default=25)
    p.add_argument('--fault-end', type=int, default=35)
    p.add_argument('--startup-cpus', type=float, default=0,
                   help='Test-container CPU quota through warmup only; removed before steady sampling')
    p.add_argument('--zone-rect', help='Test desired-state polygon as normalized left,top,right,bottom')
    args = p.parse_args()
    support = args.root / 'test-support'
    case = args.root / args.case
    case.mkdir(parents=True, exist_ok=False)
    harness = (support / 'capacity_probe.py').read_text()
    harness = harness.replace('    run.extend(["-v", "/home/admin1/Documents/sentry-validation/fixtures:/fixtures:ro"])\n', '')
    local = case / 'capacity_probe.py'
    local.write_text(harness)
    spec = importlib.util.spec_from_file_location('paired_probe', local)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    probe.ROOT = ROOT
    probe.SCHEMAS = ROOT / 'newdetails/sentry-v8'
    probe.AGENT = support / 'receiver.py'
    if args.zone_rect:
        left, top, right, bottom = map(float, args.zone_rect.split(','))
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            p.error('invalid zone rectangle')
        base_desired = probe.desired_state
        def desired(*a, **kw):
            state = base_desired(*a, **kw)
            for camera in state['cameras']:
                for zone in camera.get('geometry', {}).get('zones', []):
                    zone['poly'] = [[left, top], [right, top], [right, bottom], [left, bottom]]
            return state
        probe.desired_state = desired
    if args.source_urls_json:
        probe.SOURCE_URLS = json.loads(args.source_urls_json)
        if len(probe.SOURCE_URLS) != args.cameras:
            p.error('source URL count must equal camera count')
    epochs = case / 'sender-epochs'
    epochs.mkdir()
    marked = not args.unmarked and args.recording is not None
    if marked:
        os.environ['SENTRY_TEST_MARKER_REPLAY'] = str(epochs)
    else:
        os.environ.pop('SENTRY_TEST_MARKER_REPLAY', None)
    if args.agent_fault:
        os.environ['SENTRY_TEST_AGENT_POLICY'] = json.dumps({
            'mode': args.agent_fault, 'start': args.fault_start,
            'end': args.fault_end, 'delay': .5})
    os.environ['SENTRY_TEST_SCENE_GALLERY_LIMIT'] = '5000'
    os.environ['SENTRY_TEST_GALLERY_LIMIT'] = '2'
    original_health = probe.health
    def health(port):
        payload = original_health(port)
        if payload:
            with (case / 'health-samples.jsonl').open('a') as f:
                f.write(json.dumps({'monotonic': time.monotonic(), 'health': payload}) + '\n')
        return payload
    probe.health = health
    invocation = dict(image=args.image, cameras=args.cameras, fps=args.fps,
                      omit_fps=False, port=18180, startup_timeout=180,
                      steady_seconds=args.seconds, warmup_seconds=15,
                      sample_seconds=10, minimum_decode_ratio=.95,
                      recording=args.recording, container_env=[], source_url=args.source_url,
                      full_usecases=True, source_port_base=19001,
                      outage_camera=args.outage_camera, outage_after_seconds=args.outage_after_seconds,
                      outage_seconds=args.outage_seconds, gva_nireq=None)
    cpu_group_path = None
    if args.startup_cpus:
        original_run, original_sleep = subprocess.run, time.sleep
        container_name = f'sentinel-v8-capacity-{args.cameras}-{os.getpid()}'
        def run(command, *a, **kw):
            if isinstance(command, list) and command[:2] == ['podman', 'run']:
                command = command[:-1] + ['--cpus', str(args.startup_cpus), command[-1]]
            return original_run(command, *a, **kw)
        released = False
        def sleep(seconds):
            nonlocal released, cpu_group_path
            original_sleep(seconds)
            if seconds == invocation['warmup_seconds'] and not released:
                steady_cores = len(os.sched_getaffinity(0))
                result = original_run(['podman', 'update', '--cpus', str(steady_cores), container_name],
                                      capture_output=True, text=True, timeout=15)
                result.check_returncode()
                inspected = json.loads(original_run(['podman', 'inspect', container_name],
                                                   capture_output=True, text=True, check=True).stdout)[0]
                pid = inspected['State']['Pid']
                relative = Path(f'/proc/{pid}/cgroup').read_text().strip().split('::', 1)[1]
                cpu_group_path = Path('/sys/fs/cgroup') / relative.lstrip('/')
                cpu_max = (cpu_group_path / 'cpu.max').read_text().strip()
                (case / 'startup-quota-release.json').write_text(json.dumps({
                    'initial_cpu_quota_cores': args.startup_cpus,
                    'released_monotonic': time.monotonic(), 'returncode': result.returncode,
                    'steady_cpu_budget_cores': steady_cores, 'cpu_max': cpu_max,
                    'cpu_stat': (cpu_group_path / 'cpu.stat').read_text(),
                    'output': result.stdout + result.stderr}, indent=2))
                quota, period = cpu_max.split()
                if quota != 'max' and int(quota) / int(period) < steady_cores:
                    raise RuntimeError('startup CPU quota was not restored to full CPU capacity')
                released = True
        probe.subprocess.run, probe.time.sleep = run, sleep
    (case / 'invocation.json').write_text(json.dumps(invocation, default=str, indent=2))
    # Back-to-back compilations can begin while the previous workload's heat
    # remains. Cool only by waiting; never change host fans, power or services.
    import psutil
    cooldown_start = time.monotonic()
    initial_temperature = None
    while True:
        readings = psutil.sensors_temperatures().get('coretemp', [])
        temperature = max((x.current for x in readings), default=0)
        if initial_temperature is None:
            initial_temperature = temperature
        if temperature <= 75:
            break
        if time.monotonic() - cooldown_start >= 90:
            if temperature >= 90:
                (case / 'resource-abort.json').write_text(json.dumps({
                    'reason': 'Host remained too hot before test startup',
                    'temperature': temperature}, indent=2))
                raise RuntimeError('host did not cool before startup')
            break
        # original sleep avoids the startup quota's warmup-completion hook.
        threading.Event().wait(2)
    (case / 'cooldown.json').write_text(json.dumps({
        'initial_temperature': initial_temperature, 'final_temperature': temperature,
        'seconds': time.monotonic() - cooldown_start}, indent=2))
    observer_stop = threading.Event()
    def observe():
        import psutil
        psutil.cpu_percent(None)
        critical_samples = 0
        with gzip.open(case / 'resource-monitor.jsonl.gz', 'at') as log:
            while not observer_stop.is_set():
                start = time.monotonic()
                memory = psutil.virtual_memory()
                temperatures = {k: [x._asdict() for x in v] for k, v in psutil.sensors_temperatures().items()}
                record = {'monotonic': start, 'utc_epoch': time.time(),
                          'host_cpu_percent': psutil.cpu_percent(None),
                          'host_memory': memory._asdict(), 'temperatures': temperatures,
                          'health': original_health(invocation['port'])}
                if cpu_group_path is not None:
                    try:
                        record['container_cpu_stat'] = (cpu_group_path / 'cpu.stat').read_text()
                    except OSError:
                        pass
                critical = any(0 < x['critical'] < 150 and x['current'] >= x['critical'] - 2
                               for values in temperatures.values() for x in values if x['critical'])
                critical_samples = critical_samples + 1 if critical or memory.available < 2 * 1024**3 else 0
                log.write(json.dumps(record) + '\n')
                log.flush()
                if critical_samples >= 5:
                    (case / 'resource-abort.json').write_text(json.dumps(record, indent=2))
                    # Only our exact test container; never another host workload.
                    subprocess.run(['podman', 'stop', '--time', '3',
                                    f'sentinel-v8-capacity-{args.cameras}-{os.getpid()}'],
                                   capture_output=True, timeout=15)
                    return
                observer_stop.wait(max(.1, 1 - (time.monotonic() - start)))
    observer = threading.Thread(target=observe, name='qualification-observer', daemon=True)
    observer.start()
    try:
        report, output = probe.run_probe(argparse.Namespace(**invocation))
    finally:
        observer_stop.set()
        observer.join(timeout=5)
    if args.source_urls_json:
        report['source_mode'] = 'explicit_mixed_live_sources'
        (output / 'report.json').write_text(json.dumps(report, indent=2))
    if (case / 'resource-abort.json').exists():
        report['raw_harness_passed_before_resource_abort'] = report['passed']
        report['passed'] = report['capacity_qualified'] = False
        report['qualification_abort'] = 'resource guard stopped this test; not a capacity result'
    (case / 'artifact-directory.txt').write_text(str(output))
    (case / 'report.json').write_text(json.dumps(report, indent=2))
    with gzip.open(output / 'packages.jsonl.gz', 'wb') as f:
        packages = output / 'packages.jsonl'
        f.write(packages.read_bytes() if packages.exists() else b'')
    steady = [s for s in report['samples'] if s.get('sample_phase') == 'steady']
    latency = (analyze(output / 'gallery', epochs) if marked and len(steady) >= 2 else
               {'steady_per_camera_ms': {}, 'counts': {'not_measured': True},
                'reason': 'Unmarked/live input or insufficient steady snapshots'})
    (case / 'latency.json').write_text(json.dumps(latency, indent=2))
    (case / 'report.json').write_text(json.dumps(report, indent=2))
    (case / 'environment.json').write_text(json.dumps({
        'scope': 'Local paired comparison; other host programs remain running. Not isolated maximum-capacity qualification.',
        'source': str(args.recording), 'contract_and_gates': 'unchanged',
        'scene_interval_seconds': 2,
    }, indent=2))
    print(json.dumps({'case': args.case, 'passed': report['passed'],
                      'rates': report['steady_throughput'],
                      'freshness': latency['steady_per_camera_ms']}, indent=2), flush=True)


if __name__ == '__main__':
    main()
