import time,json,subprocess
from pathlib import Path
import psutil
root=Path('/home/admin1/Documents/sentry-validation');hits=0
while not (root/'STOP_MONITOR').exists():
 critical=[]
 for device,items in psutil.sensors_temperatures().items():
  for s in items:
   if s.critical and 0<s.critical<150 and s.current>=s.critical-2:critical.append({'device':device,'label':s.label,'current':s.current,'critical':s.critical})
 hits=hits+1 if critical else 0
 if hits>=5:
  (root/'THERMAL_ABORT.json').write_text(json.dumps({'time':time.time(),'readings':critical}))
  for name in subprocess.check_output(['docker','ps','--filter','name=sentinel-v8-capacity','--format','{{.Names}}'],text=True).splitlines():subprocess.run(['docker','stop','--time','2',name],capture_output=True)
  while (root/'THERMAL_ABORT.json').exists() and not (root/'STOP_MONITOR').exists():time.sleep(2)
  hits=0
 time.sleep(2)
