"""Standalone figures from completed evidence. Never runs inference or changes gates."""
from pathlib import Path
import datetime, json, sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

root = Path(sys.argv[1]); plots = root / 'plots'; plots.mkdir(exist_ok=True)
plt.rcParams.update({'font.size': 9, 'axes.spines.top': False, 'axes.spines.right': False,
                     'figure.dpi': 140, 'savefig.bbox': 'tight'})
def save(fig, name):
    fig.savefig(plots / (name + '.png')); fig.savefig(plots / (name + '.svg')); plt.close(fig)

analysis = json.loads((root / 'analysis.json').read_text())['results']
windows = {x['case']: x for x in json.loads((root / 'window-rate-audit.json').read_text())['cases']}
valid = [r for r in analysis if r['case'].startswith('a4-turbo-off-') and not any(r.get(k) for k in
         ['invalid_test_configuration', 'invalid_test_fixture', 'invalid_power_transition', 'abort'])]
capacity = [r for r in valid if any(s in r['case'] for s in ['isolated-all-', '-scale-', 'mixed-roles-', 'vehicle-lower-boundary']) and r.get('rates')]
fig, axes = plt.subplots(2, 2, figsize=(12, 8))
groups = [('All uses, crowd 1080p', lambda r: r['configuration']['profile'] == 'all' and '1080' in (r['configuration'].get('fixture') or '')),
          ('All uses, crowd 720p / 1440p', lambda r: r['configuration']['profile'] == 'all' and any(s in (r['configuration'].get('fixture') or '') for s in ['720','1440'])),
          ('Scene on every camera', lambda r: r['configuration']['profile'] == 'scene'),
          ('Identity / vehicle / mixed + scene', lambda r: r['configuration']['profile'] not in ['all','scene'])]
for ax, (title, predicate) in zip(axes.flat, groups):
    role_rows = []
    for r in capacity:
        if not predicate(r): continue
        cfg = r['configuration']; w = windows.get(r['case'], {}); passed = w.get('all_active_windows_passed')
        corrected = [v['cumulative_fps']['inference_frames'] for v in w.get('cameras',{}).values() if 'cumulative_fps' in v]
        rates = corrected if len(corrected)==cfg['cameras'] else [v['inference_frames'] for v in r['rates'].values()]
        resolution = (cfg.get('fixture') or '').replace('crowd-','').replace('.mp4','')
        label = f"{cfg['fps']} FPS" if title.endswith('1080p') or title == 'Scene on every camera' else f"{resolution}, {cfg['fps']} FPS"
        color = '#176b4d' if passed is True else '#b4522e' if passed is False else '#777777'
        if title == 'Identity / vehicle / mixed + scene':
            role_rows.append((f"{cfg['profile']}: {cfg['cameras']} cams", min(rates), color))
            continue
        ax.scatter(cfg['cameras'], min(rates), marker='o' if passed is True else 'x' if passed is False else '^', color=color, s=45)
        ax.annotate(label, (cfg['cameras'], min(rates)), xytext=(4, 3), textcoords='offset points', fontsize=6)
    if role_rows:
        ys = np.arange(len(role_rows)); ax.barh(ys, [v[1] for v in role_rows], color=[v[2] for v in role_rows])
        ax.set_yticks(ys, [v[0] for v in role_rows], fontsize=7); ax.axvline(8, color='#999999', linewidth=.6, linestyle='--')
        ax.set(title=title, xlabel='Lowest camera cumulative inference FPS'); ax.invert_yaxis()
    else:
        ax.axhline(8, color='#999999', linewidth=.6, linestyle='--'); ax.axhline(10, color='#cccccc', linewidth=.6, linestyle='--')
        ax.set(title=title, xlabel='Logical cameras', ylabel='Lowest camera cumulative inference FPS')
    ax.grid(alpha=.15)
fig.suptitle('Opt79 isolated replay: workload-dependent rates\nTurbo OFF, PL1 45 W, PL2 65 W. Green: matched ~10 s windows >=90%; crosses: misses; grey: incomplete timing.\nMarkers alone do not qualify freshness, scene cadence or deployment headroom.', fontsize=10)
fig.tight_layout(rect=(0, 0, 1, .89)); save(fig, 'capacity')

hours = [r for r in valid if r['configuration'].get('steady_seconds', 0) >= 3600 and 'yuv420' in r['case']]
for r in hours:
    case = root / 'artifacts' / r['case']; trace = pd.read_csv(case / 'trace.csv')
    trace['minute'] = (trace['monotonic'] - trace['monotonic'].iloc[0]) / 60
    report = json.loads((case / 'report.json').read_text()); steady = [s for s in report['samples'] if s.get('sample_phase') == 'steady']
    base = trace['monotonic'].iloc[0]; cams = list(steady[0]['per_camera']); fig, ax = plt.subplots(7, 1, figsize=(13, 15), sharex=True)
    for cam in cams:
        intervals=windows.get(r['case'],{}).get('cameras',{}).get(cam,{}).get('intervals',[])
        times=[(v['at_monotonic']-base)/60 for v in intervals];rates=[v['inference_frames']for v in intervals]
        ax[0].plot(times,rates,label=cam,linewidth=.85)
    ax[0].axhline(8,color='black',linewidth=.6,linestyle='--');ax[0].set(ylabel='Inference FPS / cam');ax[0].legend(ncol=3)
    for col,label in [('container_memory_MiB','CV cgroup memory')]:
        if col in trace:ax[1].plot(trace['minute'],trace[col],label=label)
    ax[1].set(ylabel='MiB');ax[1].legend()
    ax[2].plot(trace['minute'],trace['container_cpu_cores'],label='CV cgroup CPU cores');ax[2].set(ylabel='CPU cores');ax[2].legend()
    ax[3].plot(trace['minute'],trace['CPU_package_C'],label='CPU package °C',color='#ad4d34')
    ax[3].plot(trace['minute'],trace['package_W'],label='Package watts',color='#3272a5');ax[3].set(ylabel='°C / W');ax[3].legend(ncol=2)
    for col,label in [('GPU_node_busy_pct','GPU non-RC6 activity'),('NPU_node_busy_pct','NPU busy')]:
        if col in trace:ax[4].plot(trace['minute'],trace[col],label=label,linewidth=.6)
    ax[4].set(ylabel='Node activity %');ax[4].legend(ncol=2)
    for col,label in [('handoff_retained','Pending packages'),('handoff_exhausted','Exhausted total'),('handoff_protocol_errors','Protocol errors total')]:
        if col in trace:ax[5].plot(trace['minute'],trace[col],label=label,linewidth=.8)
    ax[5].set(ylabel='Packages');ax[5].legend(ncol=3)
    latency = json.loads((case / 'independent-latency.json').read_text())
    for cam in cams:
        pts=[x for x in latency['records'] if x['camera_id']==cam and x['steady']]
        ax[6].scatter([(x['accepted_mono']-base)/60 for x in pts],[x['transport_to_api_ms'] for x in pts],s=2,alpha=.5,label=cam)
    ax[6].set(ylabel='PES → API ms',xlabel='Minutes since case observer start');ax[6].legend(ncol=3)
    for a in ax:a.grid(alpha=.15)
    fig.suptitle('Genuine 60-minute steady test, 2 cameras × 8 FPS, all uses + scene\nMarked crowd 1080p30 and vehicle 720p10; unchanged Opt79. Turbo OFF / PL1 45 W / PL2 65 W.\nGPU activity is not GPU compute utilization; stage timers and physical capture latency are different measurements.',fontsize=11)
    fig.tight_layout(rect=(0,0,1,.94));save(fig,'hour-stress')
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    axes[0].plot(trace['minute'], trace['container_memory_MiB']); axes[0].set(ylabel='CV cgroup MiB', ylim=(3100, 3460))
    for cam in cams:
        for suffix, axis, scale, label in [('gait_retained_presences', axes[1], 1, 'retained presences'),
                                         ('gait_silhouette_payload_bytes', axes[2], 1024**2, 'silhouette payload')]:
            col = cam + '_gauge_' + suffix
            if col in trace: axis.plot(trace['minute'], trace[col] / scale, label=cam + ': ' + label)
    axes[1].set(ylabel='Presence histories'); axes[2].set(ylabel='Payload MiB', xlabel='Minutes since observer start')
    for a in axes: a.grid(alpha=.15); a.legend() if a is not axes[0] else None
    fig.suptitle('Hour-run retained state and memory detail\nSilhouette payload accounts for only part of cgroup memory growth; allocator/model state also contributes.', fontsize=11)
    fig.tight_layout(rect=(0,0,1,.92)); save(fig, 'hour-state-retention')
    # Engine telemetry is independently collected and approximately wall-time aligned.
    gp = root / 'gpu-engine-timeline.json'
    if gp.exists():
        gpu=json.loads(gp.read_text());begin=pd.Timestamp(trace['utc'].iloc[0]).timestamp();end=pd.Timestamp(trace['utc'].iloc[-1]).timestamp()
        selected=[x for x in gpu if begin<=x['utc_epoch']<=end];fig,a=plt.subplots(figsize=(12,4))
        for name in sorted({k for x in selected for k in x['engines']}):
            a.plot([(x['utc_epoch']-begin)/60 for x in selected],[x['engines'].get(name,np.nan) for x in selected],label=name,linewidth=.6)
        a.set(xlabel='Minutes',ylabel='Engine busy %',title='Independent intel_gpu_top: node engines, approximate ±2 s alignment');a.legend(ncol=5);a.grid(alpha=.15);save(fig,'hour-gpu-engines')

fig, axes = plt.subplots(1, 2, figsize=(12, 4), sharey=True)
for resolution, ax in zip(['1080p30', '1440p'], axes):
    p = root / 'artifacts' / ('a4-turbo-off-edge-latency-' + resolution) / 'independent-latency.json'
    d = json.loads(p.read_text()); pts = [x for x in d['records'] if x['steady']]; base = min(x['accepted_mono'] for x in pts)
    for cam in sorted({x['camera_id'] for x in pts}):
        v = [x for x in pts if x['camera_id'] == cam]
        ax.scatter([x['accepted_mono'] - base for x in v], [x['transport_to_api_ms'] / 1000 for x in v], label=cam, s=12)
    ax.axhline(2, color='grey', linestyle='--', linewidth=.7); ax.set(title=resolution + ': 4 cameras × 8 FPS, all uses', xlabel='Seconds in steady sample'); ax.grid(alpha=.15); ax.legend(fontsize=7)
axes[0].set(ylabel='Source first PES → API seconds'); fig.suptitle('Freshness exposes backlog that average FPS can hide; excludes capture/encoder and post-API work', fontsize=10)
fig.tight_layout(rect=(0,0,1,.9)); save(fig, 'freshness-resolution')
print(json.dumps({'plots': sorted(p.name for p in plots.iterdir())}))
