"""Offline evidence inventory. Window statistics are not per-inference percentiles."""
from pathlib import Path
import collections,datetime,gzip,json,math,statistics,sys
root=Path(sys.argv[1]);results=[]
def pct(a,p):
 a=sorted(x for x in a if isinstance(x,(int,float)) and math.isfinite(x))
 return a[min(len(a)-1,round((len(a)-1)*p))] if a else None
def stats(a):return {'samples':len(a),'min':min(a) if a else None,'p50':pct(a,.5),'p95':pct(a,.95),'p99':pct(a,.99),'max':max(a) if a else None}
for row in json.loads((root/'ledger.json').read_text()):
 p=root/'artifacts'/row['case'];out={'case':row['case'],'configuration':row['config'],'abort':row.get('abort'),'rate_candidate':row.get('candidate_passing_point',False)}
 if (p/'invalid-test-configuration.json').exists():out['invalid_test_configuration']=json.loads((p/'invalid-test-configuration.json').read_text())
 for filename,key in [('invalid-test-fixture.json','invalid_test_fixture'),('power-transition.json','invalid_power_transition')]:
  if (p/filename).exists():out[key]=json.loads((p/filename).read_text())
 path=p/'report.json'
 if not path.exists():results.append(out);continue
 r=json.loads(path.read_text());out.update(source_mode=r.get('source_mode'),startup_seconds=r.get('startup_seconds'),steady_seconds=r.get('steady_seconds'),rates=row.get('per_camera_rates'),classification=row.get('classification'),packages=r.get('package_summary'),handoff=row.get('handoff'),fatal_lines=r.get('fatal_lines'))
 samples=[s for s in r.get('samples',[])if s.get('sample_phase')=='steady'];out['container_memory_mb']=stats([s['container_memory_mb']for s in samples if s.get('container_memory_mb')is not None]);out['container_cpu_cores']=stats([s['container_cpu_cores']for s in samples if s.get('container_cpu_cores')is not None]);out['scene_cadence']={};out['source_receive_age_seconds']={}
 for cam in r.get('final',{}).get('per_camera',{}):
  out['source_receive_age_seconds'][cam]=stats([s['per_camera'][cam]['last_frame_age_seconds']for s in samples if cam in s.get('per_camera',{}) and s['per_camera'][cam].get('last_frame_age_seconds')is not None])
 events=[];packages=p/'packages.jsonl.gz'
 if packages.exists():
  with gzip.open(packages,'rt')as f:
   for line in f:
    try:events.append(json.loads(line))
    except ValueError:pass
 out['api_accepted_unique_ids']=len({e['package_id']for e in events})
 by_type=collections.defaultdict(list)
 for e in events:
  if e.get('accepted_at')and e.get('observed_at'):
   try:
    elapsed=(datetime.datetime.fromisoformat(e['accepted_at'].replace('Z','+00:00'))-datetime.datetime.fromisoformat(e['observed_at'].replace('Z','+00:00'))).total_seconds()*1000
    by_type[e['event_type']].append(elapsed)
   except ValueError:pass
 out['observed_to_api_signed_ms']={k:{**stats(v),'negative_samples':sum(x<0 for x in v)}for k,v in by_type.items()}
 out['observed_to_api_note']='Uses runtime source-PTS-to-UTC timestamp estimates, not independent physical capture time. Includes source observation/candidate age and handoff. Negative values are retained, never zero-clamped; independent PES-marker scene latency is the stronger freshness measurement.'
 scenes=collections.defaultdict(list)
 for e in events:
  if e.get('event_type')=='scene_sample':scenes[e['camera_id']].append(e)
 for cam,evs in scenes.items():
  ordered=sorted(e['accepted_mono']for e in evs if 'accepted_mono'in e)
  source_times=sorted(datetime.datetime.fromisoformat(e['observed_at'].replace('Z','+00:00')).timestamp() for e in evs)
  source_gaps=[b-a for a,b in zip(source_times,source_times[1:])]
  out['scene_cadence'][cam]={'source_interarrival_seconds':stats(source_gaps),'source_gaps_above_2p5s':sum(x>2.5 for x in source_gaps),'api_samples':len(ordered),'api_interarrival_seconds':stats([b-a for a,b in zip(ordered,ordered[1:])]),'note':'Includes startup/shutdown and fault windows; scene source timestamp cadence analyzed separately from sampled gallery.'}
 final_health=json.loads((p/'healthz-final.json').read_text()) if (p/'healthz-final.json').exists()else{}
 out['stages_final_lifetime_averages_ms']={c['camera_id']:{k:{'mean_ms':v.get('lifetime_avg_ms'),'calls':v.get('calls_total'),'measurement_type':v.get('measurement_type'),'includes_children':v.get('includes_child_stages')}for k,v in c.get('use_case_processing',{}).items()}for c in final_health.get('telemetry',{}).get('cameras',[])}
 monitor=p/'monitor.jsonl.gz';temps=[];power=[];gpu=[];npu=[];hostcpu=[];avail=[];throttle=[];ms=[]
 if monitor.exists():
  try:
   with gzip.open(monitor,'rt')as f:
    for line in f:
     x=json.loads(line);ms.append(x)
     temps.extend(t['current'] for t in x.get('temperatures',{}).get('coretemp',[])if t['label']=='Package id 0')
     power.extend(t['watts'] for t in x.get('power',{}).values() if t.get('name')=='package-0')
     for key,target in [('GPU',gpu),('NPU',npu)]:
      v=x.get('accelerators',{}).get(key,{}).get('hardware_utilization_pct')
      if isinstance(v,(int,float)):target.append(v)
     hostcpu.append(x['host_cpu_pct']);avail.append(x['memory']['available']/1024**3);throttle.append(x.get('thermal_throttle',{}))
  except (EOFError,OSError,json.JSONDecodeError)as e:out['monitor_open_or_error']=type(e).__name__
 epochs=collections.defaultdict(set)
 for x in ms:
  for camera in x.get('health',{}).get('telemetry',{}).get('cameras',[]):
   if camera.get('started_at'):epochs[camera['camera_id']].add((camera.get('pid'),camera['started_at']))
 out['camera_metric_epochs']={k:len(v)for k,v in epochs.items()}
 out['counter_reset_detected']=any(len(v)>1 for v in epochs.values())or any(v.get('inference_frames',0)<0 for v in (out.get('rates')or{}).values())
 if out['counter_reset_detected']:
  out['rates']={};out['rates_note']='Whole-case cumulative FPS suppressed because a worker/container metric epoch reset. Use phase-specific time series; do not report a negative or mixed-epoch FPS.'
 out['derived_raw_decoded_fps']={}
 if len(samples)>1 and not out['counter_reset_detected']:
  active=[x for x in ms if samples[0]['sample_monotonic_seconds']<=x['monotonic']<=samples[-1]['sample_monotonic_seconds'] and x.get('health')]
  if len(active)>1:
   duration=active[-1]['monotonic']-active[0]['monotonic'];begin={c['camera_id']:c for c in active[0]['health'].get('telemetry',{}).get('cameras',[])}
   for cam in active[-1]['health'].get('telemetry',{}).get('cameras',[]):
    name=cam['camera_id'];a=begin.get(name,{}).get('processing',{}).get('gauges',{});b=cam.get('processing',{}).get('gauges',{})
    if all(k in a and k in b for k in ['dls_rate_gate_accepted_frames','dls_rate_gate_dropped_frames']):
     delta=sum(b[k]-a[k]for k in ['dls_rate_gate_accepted_frames','dls_rate_gate_dropped_frames'])
     if delta>=0:out['derived_raw_decoded_fps'][name]=delta/duration
 out['derived_raw_decode_note']='Accepted plus rejected counters at pre-primary PTS selection gate; not the selected-FPS health field. Invalid if counters reset.'
 out['resources']={'cpu_package_C':stats(temps),'package_W':stats(power),'gpu_nonRC6_node_pct':stats(gpu),'npu_busy_node_pct':stats(npu),'host_cpu_pct_16cores':stats(hostcpu),'host_available_GiB':stats(avail),'scope':'Whole run including startup, CPU compile and shutdown; not isolated steady-state statistics.'}
 if throttle:out['thermal_throttle_counter_deltas']={k:throttle[-1].get(k,0)-v for k,v in throttle[0].items()if v is not None and throttle[-1].get(k)is not None}
 if len(samples)>1:
  memory=[(s['sample_monotonic_seconds'],s.get('container_memory_mb'))for s in samples if s.get('container_memory_mb')is not None]
  if len(memory)>1:
   t0=memory[0][0];xs=[(a-t0)/60 for a,b in memory];ys=[b for a,b in memory];xm=statistics.mean(xs);ym=statistics.mean(ys);den=sum((x-xm)**2 for x in xs)
   out['memory_linear_slope_MB_per_minute']=sum((x-xm)*(y-ym)for x,y in zip(xs,ys))/den if den else None
   out['memory_slope_note']='Includes allocator/model warmup; slope alone does not establish a leak.'
 results.append(out)
(root/'analysis.json').write_text(json.dumps({'generated_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'results':results},indent=2))
print(json.dumps({'cases':len(results),'output':str(root/'analysis.json')}))
