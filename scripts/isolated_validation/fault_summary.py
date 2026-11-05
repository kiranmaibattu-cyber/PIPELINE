"""Extract actual injected actions and recovery observations without guessing PASS."""
from pathlib import Path
import json, sys

root = Path(sys.argv[1]); results = []
def compact(h):
    t = h.get('telemetry', {})
    return {'ready': h.get('ready'), 'worker_running': h.get('worker_running'),
            'revision': h.get('revision'), 'observed_at': h.get('observed_at'),
            'handoff': t.get('handoff_scheduler', {}),
            'cameras': [{key: c.get(key) for key in ['camera_id', 'pid', 'started_at']} |
                        {'processing': {key: c.get('processing', {}).get(key) for key in ['target_fps', 'decoded_fps', 'inference_fps', 'quiet_fps']},
                         'decoder': c.get('decoder', {})} for c in t.get('cameras', [])]}
for row in json.loads((root / 'ledger.json').read_text()):
    path = root / 'artifacts' / row['case']; reportpath = path / 'report.json'
    result = {'case': row['case'], 'abort': row.get('abort'), 'events': []}
    actions = path / 'actions.jsonl'
    if actions.exists():
        for line in actions.read_text().splitlines():
            event = json.loads(line)
            if 'health' in event:
                event['health'] = compact(event['health'])
            result['events'].append(event)
    if reportpath.exists():
        report = json.loads(reportpath.read_text())
        if report.get('isolation', {}).get('requested'):
            result['source_outage_isolation'] = report['isolation']
        result['handoff_end_sample'] = report.get('end_health', {}).get('handoff')
        result['fatal_lines'] = report.get('fatal_lines')
        result['final_ready'] = report.get('final', {}).get('ready')
    if result['events'] or result.get('source_outage_isolation') or 'agent-fault-' in row['case']:
        results.append(result)
(root / 'fault-summary.json').write_text(json.dumps({'scope': 'Observed injections and snapshots, not automatic classification. '
    'Producer pauses and source outage samples are not native single-model deadlock tests. '
    'No downstream Management behavior is tested.', 'cases': results}, indent=2))
print(json.dumps({'cases': len(results)}))
