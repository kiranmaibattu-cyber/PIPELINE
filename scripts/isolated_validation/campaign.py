#!/usr/bin/env python3
"""Sequential isolated-host campaign; all runtime cases keep scene on every camera."""
from pathlib import Path
import datetime,gzip,json,os,shutil,signal,subprocess,sys,time
ROOT=Path('/home/admin1/Documents/sentry-validation');REPO=ROOT/'repo';FIX=ROOT/'fixtures';IMAGE='localhost/sentry-v8-opt79-shared-gait-background';RESULTS=json.loads((ROOT/"ledger.json").read_text()) if (ROOT/"ledger.json").exists() else []
ENV=dict(os.environ);ENV['PATH']=str(REPO/'scripts/isolated_validation')+':'+ENV.get('PATH','')
LEDGER=ROOT/'ledger.json';STATUS=ROOT/'STATUS.json'
def write(p,d):
 t=p.with_suffix('.new');t.write_text(json.dumps(d,indent=2));t.replace(p)
def log(message):print(datetime.datetime.now(datetime.timezone.utc).isoformat(),message,flush=True)
def metric_report(r):
 samples=[s for s in r.get('samples',[]) if s.get('sample_phase')=='steady'];out={}
 if len(samples)>1:
  a,b=samples[0],samples[-1];dt=b['sample_monotonic_seconds']-a['sample_monotonic_seconds']
  if dt>0:
   for cam,v in a.get('per_camera',{}).items():
    totals=b['per_camera'][cam]['totals'];out[cam]={k:(totals.get(k,0)-v['totals'].get(k,0))/dt for k in ['decoded_frames','inference_frames','quiet_frames']}
 return out

def _run_new(case,n=1,fps=8,profile='all',fixture='crowd-1080p.mp4',steady=90,extra=None,sources=None,recordings=None,fault=None,env_override=None,capacity=True):
 path=ROOT/'artifacts'/case;path.mkdir(parents=True,exist_ok=True)
 cfg={'id':case,'cameras':n,'fps':fps,'profile':profile,'fixture':fixture,'steady_seconds':steady,'fault':fault,'extra':extra,'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
 cfg['host_no_turbo']=Path('/sys/devices/system/cpu/intel_pstate/no_turbo').read_text().strip()
 cfg['host_power_limits_uw']={p.name:p.read_text().strip() for p in Path('/sys/class/powercap/intel-rapl:0').glob('constraint_*_power_limit_uw')}
 write(path/'case.json',cfg);write(STATUS,{'phase':'running','case':cfg,'completed':len(RESULTS)})
 cmd=['python3',str(REPO/'scripts/isolated_validation/probe_case.py'),'--case',case,'--profile',profile,'--root',str(ROOT),'--image',IMAGE,'--cameras',str(n),'--fps',str(fps),'--port','18080','--startup-timeout','150','--warmup-seconds','15','--steady-seconds',str(steady),'--sample-seconds','10','--minimum-decode-ratio','0.90','--full-usecases']
 if fixture:cmd+=['--recording',str(FIX/fixture)]
 if sources:cmd+=['--sources-json',json.dumps(sources)]
 if recordings:cmd+=['--recordings',json.dumps([str(FIX/x) for x in recordings])]
 if extra:cmd+=extra
 env=dict(ENV)
 if fault:env['SENTRY_TEST_AGENT_POLICY']=json.dumps(fault)
 if env_override:env.update(env_override)
 write(path/'command.json',{'argv':cmd,'overrides':{k:v for k,v in env.items() if k.startswith('SENTRY_TEST_')}})
 log('START '+case);started=time.monotonic();abort=None
 with (ROOT/'logs'/(case+'.log')).open('w') as output:
  process=subprocess.Popen(cmd,stdout=output,stderr=subprocess.STDOUT,env=env,start_new_session=True)
  while process.poll() is None:
   # Guardrails use host available memory and disk. They abort our test, never unrelated processes.
   available=0
   for line in Path('/proc/meminfo').read_text().splitlines():
    if line.startswith('MemAvailable:'):available=int(line.split()[1])*1024
   if available<2*1024**3:abort='host_available_memory_below_2GiB'
   if shutil.disk_usage(ROOT).free<3*1024**3:abort='artifact_disk_free_below_3GiB'
   if time.monotonic()-started>steady+240:abort='driver_timeout'
   if (ROOT/'THERMAL_ABORT.json').exists():abort='thermal_guardrail'
   if abort:
    subprocess.run(['docker','ps','--filter','name=sentinel-v8-capacity','--format','{{.Names}}'],capture_output=True,text=True).stdout
    for name in subprocess.check_output(['docker','ps','--filter','name=sentinel-v8-capacity','--format','{{.Names}}'],text=True).splitlines():subprocess.run(['docker','stop','--time','5',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    os.killpg(process.pid,signal.SIGTERM)
    try:process.wait(timeout=20)
    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
    break
   time.sleep(2)
 report=None
 for line in (ROOT/'logs'/(case+'.log')).read_text().splitlines():
  if line.startswith('{'):
   try:
    item=json.loads(line)
    if 'artifact_directory' in item:report=item
   except Exception:pass
 row={'case':case,'config':cfg,'returncode':process.returncode,'abort':abort,'elapsed_seconds':round(time.monotonic()-started,2)}
 if report:
  origin=Path(report['artifact_directory']);row['raw_artifact_directory']=str(origin)
  for name in ['report.json','healthz-final.json','agent-status.json']:
   if (origin/name).exists():shutil.copy2(origin/name,path/name)
  for name in ['config','source-logs','gallery']:
   if (origin/name).exists():shutil.copytree(origin/name,path/name,dirs_exist_ok=True)
  for name in ['packages.jsonl','container.log','agent.log']:
   if (origin/name).exists():
    with (origin/name).open('rb') as src,gzip.open(path/(name+'.gz'),'wb') as dst:shutil.copyfileobj(src,dst)
  row.update({'harness_passed':report['passed'],'per_camera_rates':metric_report(report),'package_summary':report['package_summary'],'fatal_lines':report['fatal_lines'],'handoff':report.get('end_health',{}).get('handoff',{})})
  if fixture and report.get('source_mode')!='local_independent_tcp_replays':
   row['invalid_source_mapping']=True
   row['harness_passed']=False
  strict_rates=all(v.get('inference_frames',0)>=.95*fps for v in row['per_camera_rates'].values()) if row['per_camera_rates'] else False
  row['active_inference_rate_passed']=strict_rates
  row['candidate_passing_point']=bool(report['passed'] and (strict_rates or profile=='scene'))
  if abort:row['candidate_passing_point']=False
 else:row['candidate_passing_point']=False
 RESULTS.append(row);write(LEDGER,RESULTS);log('END '+case+' '+str(row.get('candidate_passing_point')))
 # Remove only our stopped harness leftovers before next run. Preserve all files.
 for name in subprocess.check_output(['docker','ps','-a','--filter','name=sentinel-v8-capacity','--format','{{.Names}}'],text=True).splitlines():subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 return row

def score(row):
 path=ROOT/'artifacts'/row['case']/'report.json'
 if not path.exists():return row
 report=json.loads(path.read_text());rates=metric_report(report);fps=row['config']['fps']
 row['per_camera_rates']=rates
 row['active_inference_rate_passed']=bool(rates) and all(v['inference_frames']>=.95*fps for v in rates.values())
 row['quiet_duty_fraction']={k:v.get('quiet_frames',0)/max(v.get('decoded_frames',0),1e-9) for k,v in rates.items()}
 row['selected_frame_coverage_passed']=bool(rates) and all(v['decoded_frames']>=.95*fps and v['inference_frames']+v.get('quiet_frames',0)>=.95*fps for v in rates.values())
 row['classification']='continuous_active_rate' if row['active_inference_rate_passed'] else 'motion_gated_workload_rate'
 # This is a workload-specific candidate, not full-active capacity or freshness qualification.
 row['candidate_passing_point']=bool(row.get('harness_passed') and row['selected_frame_coverage_passed'] and not row.get('abort') and not row.get('invalid_source_mapping'))
 row['freshness_qualification']='pending independent marker and scene coverage analysis'
 return row

def run(case,*args,**kwargs):
 case=os.environ.get('SENTRY_TEST_SERIES_PREFIX','a4-turbo-off-')+case
 # A thermal event fails its active case; later cases wait for cooldown rather
 # than being falsely recorded as executed failures.
 safe_since=None
 while (ROOT/'THERMAL_ABORT.json').exists():
  import psutil
  safe=all(t.current<t.critical-10 for rows in psutil.sensors_temperatures().values() for t in rows if t.critical and 0<t.critical<150)
  safe_since=(safe_since or time.monotonic()) if safe else None
  write(STATUS,{'phase':'thermal-cooldown','next_case':case,'completed':len(RESULTS)})
  if safe_since and time.monotonic()-safe_since>=30:
   dest=ROOT/'inventory'/('thermal-event-'+str(int(time.time()))+'.json')
   (ROOT/'THERMAL_ABORT.json').replace(dest);break
  time.sleep(2)
 for existing in RESULTS:
  if existing['case']==case:
   score(existing);write(LEDGER,RESULTS);log('RESUME existing '+case);return existing
 logpath=ROOT/'logs'/(case+'.log');dest=ROOT/'artifacts'/case
 if logpath.exists() and (dest/'case.json').exists():
  for line in reversed(logpath.read_text().splitlines()):
   if not line.startswith('{'):continue
   try:report=json.loads(line)
   except Exception:continue
   if 'artifact_directory' not in report:continue
   source=Path(report['artifact_directory']);cfg=json.loads((dest/'case.json').read_text())
   for name in ['report.json','healthz-final.json','agent-status.json']:
    if (source/name).exists():shutil.copy2(source/name,dest/name)
   for name in ['config','source-logs','gallery']:
    if (source/name).exists():shutil.copytree(source/name,dest/name,dirs_exist_ok=True)
   for name in ['packages.jsonl','container.log','agent.log']:
    if (source/name).exists():
     with (source/name).open('rb') as i,gzip.open(dest/(name+'.gz'),'wb') as o:shutil.copyfileobj(i,o)
   row={'case':case,'config':cfg,'harness_passed':report['passed'],'raw_artifact_directory':str(source),'package_summary':report['package_summary'],'fatal_lines':report['fatal_lines'],'handoff':report.get('end_health',{}).get('handoff',{}),'recovered_driver_checkpoint':True}
   RESULTS.append(score(row));write(LEDGER,RESULTS);log('RECOVER completed '+case);return row
 row=_run_new(case,*args,**kwargs);score(row);write(LEDGER,RESULTS);return row

def main():
    # Foundational points and mandatory-scene use-case mixes.
    foundation=run('isolated-all-1080-1x8',steady=60)
    if foundation.get('abort')=='thermal_guardrail':
     write(STATUS,{'phase':'thermal-blocked','reason':'Single-camera cold start aborted even with turbo disabled; no further load escalation','completed':len(RESULTS)})
     return
    run('isolated-all-1080-2x8',n=2,steady=60)
    first=run('isolated-all-1080-4x8',n=4,steady=90)
    run('isolated-all-1080-2x10',n=2,fps=10,steady=75)
    run('isolated-all-1080-4x10',n=4,fps=10,steady=90)
    run('isolated-all-1440-2x8',n=2,fixture='crowd-1440p.mp4',steady=90)
    run('isolated-all-1440-4x8',n=4,fixture='crowd-1440p.mp4',steady=90)
    run('isolated-all-1440-4x10',n=4,fps=10,fixture='crowd-1440p.mp4',steady=90)
    # Progressively bracket a boundary; a failed branch is not escalated blindly.
    for profile,fixture,counts in [('all','crowd-1080p.mp4',[6,8,12,16]),('all','crowd-720p.mp4',[4,8,12,16]),('scene','crowd-1080p.mp4',[4,8,12,16,20,24]),('identity','crowd-1080p.mp4',[4,8,12]),('vehicle','ANPR.mp4',[4,8,12]),('mixed0.5','crowd-1080p.mp4',[4,8,12])]:
     for n in counts:
      recordings=(['crowd-1080p.mp4']*(n//2)+['ANPR.mp4']*(n-n//2)) if profile.startswith('mixed') else None
      r=run('scale-'+profile+'-'+str(n)+'-'+fixture.split('.')[0],n=n,profile=profile,fixture=fixture,recordings=recordings,steady=75)
      if not r['candidate_passing_point']:break
    for n in [8,12,16]:
     r=run('scale-all-1080-'+str(n)+'x10',n=n,fps=10,steady=75)
     if not r['candidate_passing_point']:break
    # Features/quiet behavior and hostile delivery. All retain scene search.
    for profile,fixture in [('presence','ANPR.mp4'),('presence','crowd-1080p.mp4'),('hazard','fire_smoke.mp4'),('all','ANPR.mp4'),('all','parking.mp4'),('all','loitering.mp4')]:
     run('functional-'+profile+'-'+fixture.split('.')[0],profile=profile,fixture=fixture,steady=75,capacity=False)
    for fraction in [.25,.75]:
     n=4;k=round(n*fraction)
     run('mixed-roles-'+str(fraction),n=n,profile='mixed'+str(fraction),recordings=['crowd-1080p.mp4']*k+['ANPR.mp4']*(n-k),steady=75)
    for mode in ['503','429','400','401','slow','drop_ack']:
     run('agent-fault-'+mode,n=2,steady=75,fault={'mode':mode,'start':30,'end':45,'delay':2},capacity=False)
    for outage in [5,15,30]:
     run('source-outage-'+str(outage),n=2,steady=90 if outage<30 else 120,extra=['--outage-camera','1','--outage-after-seconds','20','--outage-seconds',str(outage)],capacity=False)
    # Real sources: all supplied RTSP/RTSPS addresses, and reachable TCP addresses.
    source_data=json.loads((ROOT/'artifacts/preflight/sources.json').read_text())
    for item in source_data:
     if item.get('returncode')!=0:continue
     url=item['url'];suffix=url.rsplit('/',1)[-1].replace(':','-')
     scheme=url.split(':')[0]
     run('live-'+scheme+'-'+suffix,n=1,fixture=None,sources=[url],steady=60,capacity=False)
    # Repeatable default operating point. An hour of real full-pipeline busy work is mandatory.
    passing=[r for r in RESULTS if r['case'].startswith(os.environ.get('SENTRY_TEST_SERIES_PREFIX','a4-turbo-off-')) and r.get('candidate_passing_point') and r['config']['profile']=='all' and r['config']['fixture']=='crowd-1080p.mp4' and r['config']['fps']==8]
    count=min(4,max([r['config']['cameras'] for r in passing] or [1]))
    # Confirm mixed activity at chosen count before the one-hour hold.
    patterns=['crowd-1080p.mp4' if i%2==0 else 'ANPR.mp4' for i in range(count)]
    check=run('pre-stress-mixed-'+str(count),n=count,recordings=patterns,steady=90)
    if not check.get('candidate_passing_point'):
     count=1;patterns=['crowd-1080p.mp4']
    # A thermally aborted hold is not an hour. Retry a smaller declared load.
    while True:
     held=run('stress-60min-all-'+str(count)+'x8',n=count,recordings=patterns,steady=3600,capacity=False)
     if not held.get('abort') or count==1:break
     count=max(1,count//2);patterns=['crowd-1080p.mp4' if i%2==0 else 'ANPR.mp4' for i in range(count)]
    write(STATUS,{'phase':'initial-campaign-complete','completed':len(RESULTS),'next':'extended edge-case audit and final reconciliation'});log('INITIAL CAMPAIGN COMPLETE')

if __name__=="__main__":main()
