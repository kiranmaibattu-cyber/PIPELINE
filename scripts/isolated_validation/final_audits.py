"""Root standby: compact offline evidence after all measured work and restoration.

This does not declare the user task complete; manual conclusions/report remain.
"""
from pathlib import Path
import datetime, hashlib, json, os, subprocess, time, zipfile

r=Path('/home/admin1/Documents/sentry-validation');t=r/'repo/scripts/isolated_validation';results=[]
def record(case,**fields):
    results.append({'case':case,'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),**fields})
    (r/'final-audits.json').write_text(json.dumps(results,indent=2));print(case,fields,flush=True)
def run(name,argv,output,timeout=600):
    try:
        with output.open('w')as f:p=subprocess.run(argv,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
        record(name,returncode=p.returncode,output=str(output));return p.returncode
    except Exception as e:record(name,error=type(e).__name__+': '+str(e));return -1
while True:
    try:s=json.loads((r/'STATUS.json').read_text())
    except Exception:s={}
    if s.get('phase')=='measurement-complete-report-pending':break
    if s.get('phase')in ['priority-hour-error','thermal-blocked','hour-required-before-postprocessing']:
        record('blocked-by-measurement-failure',status=s);raise SystemExit(2)
    time.sleep(10)
for name,script in [('matched-counter-rates','window_rate_audit.py'),('gpu-engines','gpu_engine_analysis.py'),('fault-summary','fault_summary.py')]:
    run(name,['python3',str(t/script),str(r)],r/'logs'/(name+'.log'))
ledger=json.loads((r/'ledger.json').read_text())
for row in ledger:
    if row['config'].get('steady_seconds',0)>=3600 and 'yuv420'in row['case']:
        p=r/'artifacts'/row['case']
        if(p/'report.json').exists():run('hour-trace',['python3',str(t/'export_trace.py'),str(p)],r/'logs/hour-trace.log')
run('corrected-tables',['python3',str(t/'render_report_tables.py'),str(r)],r/'logs/corrected-tables.log')
run('later-source-rate-diagnostic',['python3',str(t/'source_rate_probe.py'),str(r/'artifacts/later-source-rates.json')],r/'logs/later-source-rates.log',90)
start=datetime.datetime.fromtimestamp((r/'gpu-monitor.pid').stat().st_mtime-600,datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
run('kernel-during-tests',['journalctl','-k','--since',start,'--no-pager'],r/'inventory/kernel-final.log')
# Archive the ACTUAL patched test tools, not only recipes, without any Secret,
# kubeconfig, token, node password or machine credential.
files=list(t.glob('*.py'))+[t/'podman',r/'repo/scripts/v8_capacity_probe.py',r/'repo/run/v8-live/mock_delivery_agent.py']
files+=list((r/'repo/newdetails/sentry-v8').glob('*'))
with zipfile.ZipFile(r/'reproduction-tools.zip','w',compression=zipfile.ZIP_DEFLATED)as z:
    hashes={}
    for p in files:
        if not p.is_file():continue
        name=str(p.relative_to(r/'repo'));z.write(p,name);hashes[name]=hashlib.sha256(p.read_bytes()).hexdigest()
(r/'reproduction-tools-sha256.json').write_text(json.dumps(hashes,indent=2));record('reproduction-archive',files=len(hashes))
restored={}
for name in ['power-limit-change.json','turbo-diagnostic.json']:
    d=json.loads((r/'inventory'/name).read_text());actual=Path(d['path']).read_text().strip();restored[name]={'actual':actual,'expected':str(d['old_value']),'restored':actual==str(d['old_value'])}
original=json.loads((r/'inventory/original-node-password-fingerprint.json').read_text());node=Path('/etc/rancher/node/password')
preserved=(node.exists()and hashlib.sha256(node.read_bytes()).hexdigest()==original['sha256'])if original['exists']else not node.exists()
restored['original_node_credential_preserved']=preserved
if preserved:(r/'inventory/original-node-password.private-backup').unlink(missing_ok=True)
(r/'inventory/restoration-final.json').write_text(json.dumps(restored,indent=2));record('restoration-verified',results=restored)
# No automatic app restart: the user requested an isolated box. Record actual
# stopped services that were active before testing for explicit restoration.
units=[]
for line in(r/'inventory/services-before.txt').read_text().splitlines():
    words=line.split()
    if len(words)>=4 and words[0].endswith('.service')and words[2]=='active':units.append(words[0])
states={}
for unit in units:
    p=subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True);states[unit]=p.stdout.strip()
(r/'inventory/services-after.json').write_text(json.dumps(states,indent=2))
record('manual-report-required',completed_cases=len(ledger),note='Inspect JSON verdicts, plots, package examples and cleanup before final response; no production-ready claim')
(r/'STATUS.json').write_text(json.dumps({'phase':'post-audits-complete-report-pending','completed':len(ledger),'utc':datetime.datetime.now(datetime.timezone.utc).isoformat()},indent=2))
