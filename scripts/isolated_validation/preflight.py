#!/usr/bin/env python3
import json,subprocess,time,hashlib,os,sys,shutil,importlib.util
from pathlib import Path
root=Path('/home/admin1/Documents/sentry-validation');repo=root/'repo';out=root/'artifacts/preflight';out.mkdir(parents=True,exist_ok=True)
image='localhost/sentry-v8-opt79-shared-gait-background';results=[]
def run(name,cmd,timeout=180):
 start=time.time()
 try:
  r=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout);code=r.returncode;text=r.stdout+r.stderr
 except subprocess.TimeoutExpired as e:code=124;text=str(e)
 (out/(name+'.txt')).write_text(text);results.append({'case':name,'returncode':code,'seconds':round(time.time()-start,2)})
 return code
# Mount only pure-Python test-runner dependencies; do not pip-install into the frozen image.
deps=root/'testdeps';deps.mkdir(exist_ok=True)
for name in ['pytest','_pytest','pluggy','iniconfig','packaging','pygments']:
 spec=importlib.util.find_spec(name)
 if spec is not None:
  p=Path(spec.origin);src=p.parent if p.name=='__init__.py' else p
  if src.is_dir():shutil.copytree(src,deps/src.name,dirs_exist_ok=True)
  else:shutil.copy2(src,deps/src.name)
run('runtime-unit-tests',['docker','run','--rm','--network=host','-e','PYTEST_DISABLE_PLUGIN_AUTOLOAD=1','-e','PYTHONPATH=/testdeps','-v',str(deps)+':/testdeps:ro','-v',str(repo/'edge_runtime/solution_packs/sporada_secure/runtime_v8/tests')+':/opt/traffic-pilot/tests:ro','-v',str(repo/'newdetails/sentry-v8')+':/opt/traffic-pilot/image_schema:ro','--entrypoint','python3',image,'-m','pytest','-q','-p','no:cacheprovider','/opt/traffic-pilot/tests'])
run('canonical-contract',['python3',str(repo/'newdetails/sentry-v8/validate_contract.py')])
run('baked-contract',['docker','run','--rm','--entrypoint','python3',image,'/opt/traffic-pilot/contracts/sentry-v8/validate_contract.py'])
run('devices-models-plugins',['docker','run','--rm','--device','/dev/dri','--device','/dev/accel/accel0','--group-add','992','--group-add','44','--entrypoint','bash',image,'-c',"python3 -c 'import openvino as ov; c=ov.Core(); print(c.available_devices); print([(d,c.get_property(d,\"FULL_DEVICE_NAME\")) for d in c.available_devices])'; for x in vah264dec vah265dec vapostproc gvadetect gvatrack gvaclassify gvainference gvamotiondetect tsdemux h264parse h265parse tcpclientsrc; do gst-inspect-1.0 $x >/dev/null && echo plugin:$x:OK || echo plugin:$x:MISSING; done; find /models -type f \\( -name '*.bin' -o -name '*.xml' \\) -print0 | sort -z | xargs -0 sha256sum"],300)
urls=['rtsp://192.168.1.95:8554/'+n for n in ['traffic1','traffic2','ch9']]+['rtsps://gpu-box.burmilla-bass.ts.net:443/'+n for n in ['ch9','ch10','ch16','traffic1','traffic2','camera01']]+['tcp://192.168.1.92:'+str(p) for p in range(9001,9011)]
probes=[]
for i,url in enumerate(urls):
 cmd=['ffprobe','-v','error','-rw_timeout','4000000']
 if url.startswith(('rtsp:','rtsps:')):cmd+=['-rtsp_transport','tcp']
 cmd+=['-show_entries','stream=codec_name,width,height,avg_frame_rate,r_frame_rate','-of','json',url]
 start=time.time()
 try:
  r=subprocess.run(cmd,capture_output=True,text=True,timeout=8);item={'url':url,'returncode':r.returncode,'probe':r.stdout,'error':r.stderr[:1000]}
 except subprocess.TimeoutExpired:item={'url':url,'returncode':124,'error':'source probe timeout'}
 item['seconds']=round(time.time()-start,2);probes.append(item)
 (out/'sources.json').write_text(json.dumps(probes,indent=2))
(root/'preflight-results.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results),flush=True)
