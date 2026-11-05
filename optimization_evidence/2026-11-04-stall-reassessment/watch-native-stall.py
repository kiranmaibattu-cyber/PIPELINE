import subprocess,json,time
from pathlib import Path
out=Path('/home/admin1/Documents/sentry_cv/optimization_evidence/2026-11-04-stall-reassessment');out.mkdir(exist_ok=True)
end=time.monotonic()+240
while time.monotonic()<end:
 ids=subprocess.check_output(['podman','ps','--filter','ancestor=localhost/sentry-v8-opt66-telemetry','--format','{{.ID}}'],text=True).split()
 if not ids:time.sleep(2);continue
 cid=ids[0]
 log=subprocess.check_output(['podman','logs','--tail','350',cid],text=True,stderr=subprocess.STDOUT)
 rows=[]
 for line in log.splitlines():
  try:d=json.loads(line)
  except ValueError:continue
  if d.get('event')=='native_pipeline_progress' and d.get('phase_age_seconds',0)>4 and d.get('camera_phase')=='plate_detection':rows.append(d)
 if rows:
  top=subprocess.check_output(['podman','top',cid,'hpid','pid','args'],text=True)
  match=[x.split()[0] for x in top.splitlines()[1:] if len(x.split())>1 and x.split()[1]==str(rows[-1]['pid'])]
  if not match:raise RuntimeError(top)
  pid=match[0]; (out/'trigger-opt66.json').write_text(json.dumps(rows,indent=2));(out/'top.txt').write_text(top)
  with (out/'native-stack-opt66.txt').open('w') as f:
   r=subprocess.run(['sudo','-n','gdb','-nx','-batch','-ex','set pagination off','-ex',f'set sysroot /proc/{pid}/root','-ex',f'attach {pid}','-ex','thread apply all bt 16','-ex','detach'],stdout=f,stderr=subprocess.STDOUT,timeout=35)
  print('stack captured',pid,r.returncode,flush=True);break
 time.sleep(2)
else: print('no qualifying stall captured',flush=True)
