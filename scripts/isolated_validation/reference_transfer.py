"""Local standby: stream frozen baseline to Docker only at the requested idle phase."""
from pathlib import Path
import json, subprocess, time, sys

root='/home/admin1/Documents/sentry-validation'
ssh=['ssh','-o','ControlPath=/tmp/sentry-validation-ssh-%C','admin1@192.168.1.64']
archive=Path('/dev/shm/sentry-validation-opt76.docker.tar')
evidence=Path('/home/admin1/Documents/sentry_cv/optimization_evidence/2026-11-05-isolated-host')
reference='localhost/sentry-v8-opt76-body-prefetch'
original=json.loads(subprocess.check_output(['podman','image','inspect',reference]))[0]
while True:
    check=subprocess.run(ssh+['test -f '+root+'/REFERENCE_IMPORT_REQUEST.json'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if check.returncode==0:break
    time.sleep(10)
print('Reference import requested at idle boundary',flush=True)
with archive.open('rb')as src,(evidence/'reference-import.log').open('w')as out:
    loaded=subprocess.run(ssh+['docker load'],stdin=src,stdout=out,stderr=subprocess.STDOUT,timeout=300)
if loaded.returncode:raise SystemExit('Reference import failed; see log')
remote=json.loads(subprocess.check_output(ssh+['docker image inspect '+reference]))[0]
equivalent=original['RootFS']['Layers']==remote['RootFS']['Layers'] and all(original['Config'].get(k)==remote['Config'].get(k)for k in ['Env','Entrypoint','Cmd','User','WorkingDir'])
result={'equivalent':equivalent,'original_id':original['Id'],'docker_id':remote['Id'],'original_layers':original['RootFS']['Layers'],'docker_layers':remote['RootFS']['Layers'],'scope':'Frozen opt76 export/import, not a rebuild. All rootfs diffIDs and critical runtime config compared.'}
(evidence/'reference-image-equivalence.json').write_text(json.dumps(result,indent=2))
if not equivalent:raise SystemExit('Reference image failed equivalence check')
subprocess.run(ssh+['cat > '+root+'/REFERENCE_IMPORT_READY.json'],input=json.dumps(result),text=True,check=True)
print('Reference frozen image verified; comparison may proceed',flush=True)
