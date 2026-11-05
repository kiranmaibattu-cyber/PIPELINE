"""One-off driver checkpoint between stock and explicitly power-capped tests."""
import os,signal,json,time,subprocess
from pathlib import Path
r=Path('/home/admin1/Documents/sentry-validation')
while json.loads((r/'CURRENT.json').read_text()).get('id')!='idle':time.sleep(2)
for name in ['campaign.pid','extended.pid']:
 try:os.kill(int((r/name).read_text()),signal.SIGKILL)
 except ProcessLookupError:pass
p=r/'artifacts/a3-pl2-65w-isolated-all-1080-2x8/power-transition.json'
p.write_text(json.dumps({'invalid_for_capacity':True,'reason':'CPU turbo setting changed during run; old parent paused awaiting checkpoint. Raw report retained.'}))
for script,label,pidfile in [('campaign.py','campaign-turbo-off.log','campaign.pid'),('extended_campaign.py','extended-turbo-off.log','extended.pid')]:
 f=(r/'logs'/label).open('w')
 child=subprocess.Popen(['python3',str(r/'repo/scripts/isolated_validation'/script)],stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
 (r/pidfile).write_text(str(child.pid));print(script,child.pid,flush=True)
