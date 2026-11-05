"""Offline intel_gpu_top engine statistics, with explicit approximate alignment."""
from pathlib import Path
import datetime, json, sys
import numpy as np

root = Path(sys.argv[1])
data = (root / 'artifacts/gpu-engines.json').read_text()
decoder = json.JSONDecoder()
offset = 0
elapsed = 0.0
anchor = (root / 'gpu-monitor.pid').stat().st_mtime
records = []
while offset < len(data):
    # intel_gpu_top emits concatenated objects and, when client output changes,
    # comma-separated objects. A separator is not a truncated sample.
    while offset < len(data) and (data[offset].isspace() or data[offset] == ','):
        offset += 1
    if offset == len(data):
        break
    try:
        item, offset = decoder.raw_decode(data, offset)
    except json.JSONDecodeError:
        # An interrupted final object is unavailable, never a zero sample.
        break
    elapsed += item['period']['duration'] / 1000.0
    records.append({'utc_epoch': anchor + elapsed,
                    'engines': {k: v['busy'] for k, v in item.get('engines', {}).items()}})

def stats(values):
    return {'samples': len(values), **{k: float(np.percentile(values, q)) if values else None
                                      for k, q in [('p50', 50), ('p95', 95), ('max', 100)]}}

out = {'scope': 'Node-wide engine busy percentages, not isolated container attribution. '
                'Engines overlap and must not be summed. Alignment uses monitor PID-file '
                'mtime plus cumulative sample durations, approximately +/-2 seconds.',
       'anchor_utc': datetime.datetime.fromtimestamp(anchor, datetime.timezone.utc).isoformat(),
       'samples': len(records), 'cases': {}}
for row in json.loads((root / 'ledger.json').read_text()):
    cfg = row['config']
    start = datetime.datetime.fromisoformat(cfg['started_utc']).timestamp()
    duration = row.get('elapsed_seconds')
    if duration is None:
        continue
    selected = [r for r in records if start <= r['utc_epoch'] <= start + duration]
    names = sorted({k for r in selected for k in r['engines']})
    out['cases'][row['case']] = {name: stats([r['engines'][name] for r in selected if name in r['engines']])
                                for name in names}
(root / 'gpu-engine-analysis.json').write_text(json.dumps(out, indent=2))
# A small timeline is easier to archive than the entire clients-heavy raw log.
(root / 'gpu-engine-timeline.json').write_text(json.dumps(records, separators=(',', ':')))
print(json.dumps({'samples': len(records), 'cases': len(out['cases'])}))
