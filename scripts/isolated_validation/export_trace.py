"""Export compact1Hz evidence for plotting; raw JSON remains authoritative."""
from pathlib import Path
import csv,gzip,json,sys
case=Path(sys.argv[1]);rows=[]
with gzip.open(case/'monitor.jsonl.gz','rt')as f:
 for line in f:
  x=json.loads(line);h=x.get('health',{});t=h.get('telemetry',{});cg=t.get('system',{}).get('container_cgroup',{});scheduler=t.get('handoff_scheduler',{});counters=scheduler.get('counters',{})
  row={'utc':x['utc'],'monotonic':x['monotonic'],'host_cpu_pct':x['host_cpu_pct'],'host_available_GiB':x['memory']['available']/1024**3,'container_cpu_cores':cg.get('cpu_cores_used'),'container_memory_MiB':cg.get('memory_current_mb'),'container_pids':cg.get('pids_current'),'ready':h.get('ready'),'worker_running':h.get('worker_running'),'handoff_retained':scheduler.get('retained_packages'),'handoff_bytes':scheduler.get('retained_bytes')}
  for k,v in counters.items():row['handoff_'+k]=v
  for device in ['GPU','NPU']:
   a=x.get('accelerators',{}).get(device,{});row[device+'_node_busy_pct']=a.get('hardware_utilization_pct');row[device+'_frequency_mhz']=a.get('current_frequency_mhz')
  row['CPU_package_C']=next((v['current']for v in x.get('temperatures',{}).get('coretemp',[])if v['label']=='Package id 0'),None)
  row['package_W']=next((v['watts']for v in x.get('power',{}).values()if v.get('name')=='package-0'),None)
  frequencies=[v['current']for v in x.get('cpu_frequency_mhz',[])];row['CPU_max_frequency_mhz']=max(frequencies)if frequencies else None
  for c in t.get('cameras',[]):
   prefix=c['camera_id']+'_';p=c.get('processing',{});d=c.get('decoder',{})
   row[prefix+'epoch']=c.get('started_at');row[prefix+'pid']=c.get('pid');row[prefix+'telemetry_at']=c.get('observed_at');row[prefix+'uptime']=c.get('uptime_seconds')
   for k in ['target_fps','decoded_fps','inference_fps','quiet_fps']:row[prefix+k]=p.get(k)
   for k,v in p.get('totals',{}).items():row[prefix+'total_'+k]=v
   for k,v in p.get('gauges',{}).items():row[prefix+'gauge_'+k]=v
   for k in ['state','last_frame_age_seconds','decoder_errors','reconnects','stream_session_id']:row[prefix+'decoder_'+k]=d.get(k)
   for stage,v in c.get('use_case_processing',{}).items():
    row[prefix+stage+'_calls']=v.get('calls_total');row[prefix+stage+'_rolling_mean_ms']=v.get('avg_ms');row[prefix+stage+'_rolling_max_ms']=v.get('max_ms')
  rows.append(row)
fields=sorted({k for row in rows for k in row});out=case/'trace.csv'
with out.open('w')as f:
 w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
print(json.dumps({'rows':len(rows),'columns':len(fields),'output':str(out)}))
