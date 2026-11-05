#!/usr/bin/env python3
"""Independent host observer. Device scope and missing sensors are explicit."""
import argparse,datetime,gzip,json,os,sys,time,urllib.request,importlib.util
from pathlib import Path
import psutil
ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,required=True);args=ap.parse_args();root=args.root
module=root/'repo/edge_runtime/solution_packs/sporada_secure/runtime_v8/services/worker/pipeline/resource_metrics.py'
spec=importlib.util.spec_from_file_location('resource_metrics',module);rm=importlib.util.module_from_spec(spec);spec.loader.exec_module(rm);accelerators=rm.AcceleratorSampler()
stop=root/'STOP_MONITOR';fh=None;last_case=None
psutil.cpu_percent(None);power_previous={};last_t=time.monotonic()
def value(p):
 try:return float(p.read_text().strip())
 except Exception:return None
while not stop.exists():
 t=time.monotonic();wall=datetime.datetime.now(datetime.timezone.utc).isoformat();record={'utc':wall,'monotonic':t,'host_cpu_pct':psutil.cpu_percent(None),'host_cpu_percpu_pct':psutil.cpu_percent(None,percpu=True),'memory':psutil.virtual_memory()._asdict(),'swap':psutil.swap_memory()._asdict(),'accelerators':accelerators.sample(),'load':os.getloadavg(),'observer_rss':psutil.Process().memory_info().rss}
 try:record['temperatures']={k:[x._asdict() for x in v] for k,v in psutil.sensors_temperatures().items()}
 except Exception as e:record['temperature_status']=type(e).__name__
 record['thermal_throttle']={str(p):value(p) for p in Path('/sys/devices/system/cpu').glob('cpu*/thermal_throttle/*_throttle_count')}
 record['power']={}
 for p in Path('/sys/class/powercap').glob('intel-rapl*/energy_uj'):
  v=value(p);prev=power_previous.get(str(p));maximum=value(p.parent/'max_energy_range_uj')
  if v is not None and prev is not None:
   delta=v-prev
   if delta<0 and maximum:delta+=maximum
   record['power'][str(p)]={'watts':delta/1e6/max(t-last_t,1e-6),'energy_uj':v,'name':(p.parent/'name').read_text().strip()}
  if v is not None:power_previous[str(p)]=v
 last_t=t
 try:record['cpu_frequency_mhz']=[x._asdict() for x in psutil.cpu_freq(percpu=True)]
 except Exception:pass
 try:case=json.loads((root/'CURRENT.json').read_text())
 except Exception:case={'id':'idle','port':18080}
 record['case']=case['id']
 if record['case']!=last_case:
  if fh:fh.close()
  dest=root/'artifacts'/record['case'];dest.mkdir(parents=True,exist_ok=True);os.chown(dest,1000,1000)
  fh=gzip.open(dest/'monitor.jsonl.gz','at');last_case=record['case']
 if case['id']!='idle':
  try:
   with urllib.request.urlopen('http://127.0.0.1:'+str(case.get('port',18080))+'/healthz',timeout=.7) as r:record['health']=json.load(r)
  except Exception as e:record['health_error']=type(e).__name__
 fh.write(json.dumps(record,separators=(',',':'),allow_nan=False)+'\n');fh.flush()
 time.sleep(max(0,1-(time.monotonic()-t)))
if fh:fh.close()
