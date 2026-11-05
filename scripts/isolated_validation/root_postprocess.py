"""Root-only post-test tasks; waits for all measurement cases to finish first."""
from pathlib import Path
import json,os,signal,subprocess,time,datetime
import psutil
root=Path('/home/admin1/Documents/sentry-validation');tools=root/'repo/scripts/isolated_validation';image='localhost/sentry-v8-opt79-shared-gait-background';results=[]
def stamp():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def record(name,**fields):
 results.append({'case':name,'utc':stamp(),**fields});(root/'root-postprocess.json').write_text(json.dumps(results,indent=2));print(name,fields,flush=True)
def current(name,port=18080):
 p=root/'CURRENT.new';p.write_text(json.dumps({'id':name,'port':port}));p.replace(root/'CURRENT.json')
def execute(name,args,output,timeout=600):
 try:
  with output.open('w')as f:r=subprocess.run(args,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
  record(name,returncode=r.returncode,output=str(output));return r.returncode
 except Exception as e:record(name,error=type(e).__name__+': '+str(e),output=str(output));return -1
def frozen_python(script,*args):return ['docker','run','--rm','--user','0','-v',str(root)+':/campaign:ro','-v',str(tools)+':/tools:ro','--entrypoint','python3',image,'/tools/'+script,*map(str,args)]
while True:
 try:state=json.loads((root/'STATUS.json').read_text())
 except Exception:state={}
 if state.get('phase')=='extended-complete':break
 if state.get('phase')=='thermal-blocked':record('load-testing-blocked',reason=state);raise SystemExit(2)
 time.sleep(10)
holds=[]
for row in json.loads((root/'ledger.json').read_text()):
 if row['config'].get('steady_seconds',0)<3600:continue
 path=root/'artifacts'/row['case']/'report.json'
 if not path.exists():continue
 report=json.loads(path.read_text());steady=[s for s in report.get('samples',[])if s.get('sample_phase')=='steady']
 if len(steady)>1 and steady[-1]['sample_monotonic_seconds']-steady[0]['sample_monotonic_seconds']>=3580:holds.append(row['case'])
if not holds:
 record('hour-still-required',reason='No actual60-minute observation window; startup failures do not count')
 (root/'STATUS.json').write_text(json.dumps({'phase':'hour-required-before-postprocessing'}));raise SystemExit(2)
record('measurement-cases-finished',completed_hour_windows=holds)
current('kubernetes',18081)
execute('kubernetes-integration',['python3',str(tools/'kubernetes_check.py')],root/'logs/kubernetes-check.log',timeout=1000)
# A failed cluster helper must not leave its own isolated server running.
pidfile=root/'kubernetes-server.pid'
if pidfile.exists():
 try:
  p=psutil.Process(int(pidfile.read_text()))
  if any(str(root/'k3s-disposable')==a for a in p.cmdline()):
   children=p.children(recursive=True)
   for child in children:
    try:child.terminate()
    except psutil.Error:pass
   p.terminate();gone,alive=psutil.wait_procs([p,*children],timeout=10)
   for child in alive:
    try:child.kill()
    except psutil.Error:pass
 except psutil.Error:pass
current('offline-audits')
execute('gallery-audit',frozen_python('gallery_audit.py','/campaign/artifacts'),root/'artifacts/gallery-audit.json')
ledger=json.loads((root/'ledger.json').read_text())
for row in ledger:
 case=root/'artifacts'/row['case'];epochs=case/'sender-epochs'
 if not epochs.exists()and 'edge-latency-'in row['case']:
  suffix=row['case'].split('edge-latency-',1)[1];epochs=root/'artifacts'/('edge-latency-'+suffix)/'sender-epochs'
 if epochs.exists()and(case/'report.json').exists()and(case/'gallery').exists():
  relative=lambda p:'/campaign/'+str(p.relative_to(root))
  execute('marker-analysis-'+row['case'],frozen_python('analyze_markers.py',relative(case/'gallery'),relative(epochs)),case/'independent-latency.json',timeout=300)
current('idle-post-test');time.sleep(15)
(root/'STOP_MONITOR').touch()
try:psutil.Process(int((root/'monitor.pid').read_text())).wait(10)
except (psutil.Error,psutil.TimeoutExpired):pass
for pidfile,expected in [('gpu-monitor.pid','intel_gpu_top'),('turbostat.pid','turbostat'),('thermal-guard.pid','thermal_guard.py')]:
 try:
  p=psutil.Process(int((root/pidfile).read_text()))
  if any(expected in x for x in p.cmdline()):p.send_signal(signal.SIGINT if expected!='thermal_guard.py'else signal.SIGTERM)
 except psutil.Error:pass
record('monitors-stopped')
execute('analysis',['python3',str(tools/'analyze_artifacts.py'),str(root)],root/'logs/analysis.log')
execute('tables',['python3',str(tools/'render_report_tables.py'),str(root)],root/'logs/tables.log')
execute('final-image-identity',['docker','image','inspect',image,'--format','{{.Id}}'],root/'inventory/final-image-id.txt')
# Restore only our temporary hardware/access settings; leave competing apps
# stopped, as requested, and provide explicit restoration instructions later.
for filename in ['power-limit-change.json','turbo-diagnostic.json']:
 try:
  data=json.loads((root/'inventory'/filename).read_text());p=Path(data['path']);p.write_text(str(data['old_value']));record('restore-'+filename,path=str(p),readback=p.read_text().strip())
 except Exception as e:record('restore-'+filename,error=str(e))
r=subprocess.run(['setfacl','-x','u:admin1','/var/run/docker.sock'],capture_output=True,text=True);record('remove-temporary-docker-acl',returncode=r.returncode)
current('idle')
(root/'STATUS.json').write_text(json.dumps({'phase':'measurement-complete-report-pending','utc':stamp(),'completed':len(ledger),'next':'Reconcile expected fault outcomes, generate plots/final report and handoff; no production-ready claim'},indent=2))
record('report-reconciliation-pending')
