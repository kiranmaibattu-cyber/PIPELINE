"""Root-only, post-performance Kubernetes integration on a disposable cluster.

Never points kubectl at the user's original kubeconfig/data directory.
"""
import base64,hashlib,json,os,signal,subprocess,sys,time,urllib.request,secrets
import psutil
from pathlib import Path
root=Path('/home/admin1/Documents/sentry-validation');out=root/'artifacts/kubernetes';out.mkdir(exist_ok=True)
repo=root/'repo';data=root/'k3s-disposable';config=data/'kubeconfig.yaml';events=[];server=None;sender=None
sys.path.insert(0,str(repo/'scripts'));from v8_capacity_probe import desired_state
image='localhost/sentry-v8-opt79-shared-gait-background:latest'
def run(args,timeout=60,save=None):
 r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 events.append({'time':time.time(),'command':args,'returncode':r.returncode,'stdout_tail':r.stdout[-1500:],'stderr_tail':r.stderr[-1500:]})
 if save:(out/save).write_text(r.stdout+r.stderr)
 return r
def kubectl(*args,timeout=60,save=None):return run(['k3s','kubectl','--kubeconfig',str(config),*args],timeout,save)
def health():
 try:
  with urllib.request.urlopen('http://127.0.0.1:18081/healthz',timeout=2)as r:return json.load(r)
 except Exception as e:return {'error':type(e).__name__}
verdict={'scope':'Disposable Kubernetes cluster, privileged device access; API receiver sidecar, no downstream Management','passed':False}
try:
 # Refuse if the user's original orchestration has returned.
 if subprocess.run(['systemctl','is-active','--quiet','k3s']).returncode==0:raise RuntimeError('Original k3s is active; refusing isolated cluster')
 if not os.environ.get('SENTRY_TEST_KUBE_ONLY'):
  # The four-camera ANPR role failed genuine matched-clock rate criteria.
  # Fill its two lower operating points while the frozen measurement power
  # condition and1Hz monitors are still active. No concurrent test producer.
  sys.path.insert(0,str(repo/'scripts/isolated_validation'))
  import campaign as capacity
  capacity.RESULTS=json.loads((root/'ledger.json').read_text())
  repeated=capacity.run('edge-action-pause_worker-persistent-replay',n=2,steady=90,capacity=False,env_override={'SENTRY_TEST_ACTION':json.dumps({'mode':'pause_worker','start':20,'duration':10})})
  verdict['repeated_pause_with_persistent_sources']=repeated['case']
  lower=[]
  for count in [2,3]:
   name='vehicle-lower-boundary-marked-'+str(count);case='a4-turbo-off-'+name
   epoch=root/'artifacts'/case/'sender-epochs';epoch.mkdir(parents=True,exist_ok=True)
   row=capacity.run(name,n=count,profile='vehicle',fixture='latency-anpr720p10-420.mp4',steady=60,capacity=False,env_override={'SENTRY_TEST_MARKER_REPLAY':str(epoch),'SENTRY_TEST_SCENE_GALLERY_LIMIT':'200','SENTRY_TEST_GALLERY_LIMIT':'200'})
   lower.append(row['case'])
  verdict['additional_marked_vehicle_capacity_cases']=lower
  (root/'CURRENT.json').write_text(json.dumps({'id':'kubernetes','port':18081}))
  # Post-performance diagnostic only, before creating the disposable cluster.
  with(out/'empty-native-models.json').open('w')as stdout,(out/'empty-native-models.log').open('w')as stderr:
   negative=subprocess.run(['docker','run','--rm','--device','/dev/dri','--group-add','992','--group-add','44','-v',str(root/'fixtures')+':/fixtures:ro','-v',str(repo/'scripts/isolated_validation/empty_model_probe.py')+':/probe.py:ro','--entrypoint','python3',image,'/probe.py'],stdout=stdout,stderr=stderr,timeout=90)
  verdict['empty_native_model_diagnostic_returncode']=negative.returncode
 log=(out/'k3s.log').open('w')
 data.mkdir(exist_ok=True);empty=data/'standalone-config.yaml';empty.write_text('{}\n');token=data/'standalone-token';token.write_text(secrets.token_hex(32));token.chmod(0o600)
 environment={k:v for k,v in os.environ.items()if not k.startswith('K3S_')}
 server=subprocess.Popen(['k3s','server','--config',str(empty),'--token-file',str(token),'--data-dir',str(data),'--write-kubeconfig',str(config),'--write-kubeconfig-mode','600','--https-listen-port','6444','--lb-server-port','6446','--kubelet-arg','root-dir='+str(data/'kubelet'),'--kubelet-arg','port=10260','--kubelet-arg','healthz-port=10262','--node-name','sentry-validation-node','--node-ip','192.168.1.64','--flannel-backend','none','--disable-network-policy','--disable-kube-proxy','--disable','traefik,servicelb,metrics-server,local-storage,coredns'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=environment)
 (root/'kubernetes-server.pid').write_text(str(server.pid))
 deadline=time.monotonic()+150
 while time.monotonic()<deadline:
  if (root/'THERMAL_ABORT.json').exists():raise RuntimeError('thermal guardrail')
  if server.poll()is not None:raise RuntimeError('Disposable k3s exited')
  if config.exists()and kubectl('get','--raw=/readyz',timeout=10).returncode==0:break
  time.sleep(3)
 else:raise RuntimeError('Disposable k3s API startup timeout')
 imported=run(['k3s','ctr','images','import',str(root/'opt79.docker.tar')],timeout=240,save='image-import.txt')
 if imported.returncode:raise RuntimeError('containerd import failed')
 # One independently replayed camera, multi-client sender supports pod restart.
 senderlog=(out/'sender.log').open('w')
 sender=subprocess.Popen(['python3',str(repo/'scripts/isolated_validation/reusable_sender.py'),'--port','19091','--fixture',str(root/'fixtures/motion-transition.mp4')],stdout=senderlog,stderr=subprocess.STDOUT,start_new_session=True)
 state=desired_state(1,8,full_usecases=True)
 # Full-binding Docker validation established genuine quiet/wake on this
 # recorded clip. Keep every binding in Kubernetes too. Uniform black frames
 # are a separate weak-smoke diagnostic and cannot stand in for this control.
 verdict['motion_negative_control']={'fixture':'motion-transition.mp4','fire_smoke_binding_enabled':True,'reason':'Use the previously observed fully-negative quiet recording followed by crowd and quiet, preserving all use-case bindings and gates.'}
 ns={'apiVersion':'v1','kind':'Namespace','metadata':{'name':'sentry-validation'}}
 meta={'namespace':'sentry-validation'}
 cm={'apiVersion':'v1','kind':'ConfigMap','metadata':dict(meta,name='inputs'),'data':{'desired_state.json':json.dumps(state),'agent.py':(repo/'run/v8-live/mock_delivery_agent.py').read_text()}}
 secret={'apiVersion':'v1','kind':'Secret','metadata':dict(meta,name='camera-urls'),'type':'Opaque','data':{'capacity-01.url':base64.b64encode(b'tcp://127.0.0.1:19091\n').decode()}}
 pod={'apiVersion':'v1','kind':'Pod','metadata':dict(meta,name='sentry-validation'),'spec':{'hostNetwork':True,'dnsPolicy':'Default','nodeName':'sentry-validation-node','restartPolicy':'Always','securityContext':{'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'supplementalGroups':[44,992]},'volumes':[{'name':'inputs','configMap':{'name':'inputs'}},{'name':'secrets','secret':{'secretName':'camera-urls'}},{'name':'delivery','emptyDir':{}},{'name':'state','emptyDir':{}},{'name':'dri','hostPath':{'path':'/dev/dri','type':'Directory'}},{'name':'accel','hostPath':{'path':'/dev/accel','type':'Directory'}}],'containers':[
 {'name':'agent','image':image,'imagePullPolicy':'Never','command':['python3','/inputs/agent.py','--socket','/run/sentinel-delivery/agent.sock','--schemas','/opt/traffic-pilot/contracts/sentry-v8','--log','/state/packages.jsonl','--status','/state/agent-status.json'],'volumeMounts':[{'name':'inputs','mountPath':'/inputs','readOnly':True},{'name':'delivery','mountPath':'/run/sentinel-delivery'},{'name':'state','mountPath':'/state'}]},
 {'name':'cv','image':image,'imagePullPolicy':'Never','env':[{'name':'APEX_API_PORT','value':'18081'}],'securityContext':{'privileged':True},'resources':{'requests':{'cpu':'1','memory':'3Gi'},'limits':{'cpu':'4','memory':'8Gi'}},'volumeMounts':[{'name':'inputs','mountPath':'/configs','readOnly':True},{'name':'secrets','mountPath':'/run/secrets/sentinel/cameras','readOnly':True},{'name':'delivery','mountPath':'/run/sentinel-delivery'},{'name':'dri','mountPath':'/dev/dri'},{'name':'accel','mountPath':'/dev/accel'}],'readinessProbe':{'httpGet':{'path':'/readyz','port':18081},'initialDelaySeconds':5,'periodSeconds':3},'livenessProbe':{'httpGet':{'path':'/healthz','port':18081},'initialDelaySeconds':10,'periodSeconds':5}}
 ]}}
 manifest={'apiVersion':'v1','kind':'List','items':[ns,cm,secret,pod]};path=out/'manifest.json';path.write_text(json.dumps(manifest,indent=2))
 if kubectl('apply','-f',str(path)).returncode:raise RuntimeError('Pod apply failed')
 deadline=time.monotonic()+180;samples=[]
 while time.monotonic()<deadline:
  if (root/'THERMAL_ABORT.json').exists():raise RuntimeError('thermal guardrail')
  h=health();samples.append({'time':time.time(),'phase':'startup','health':h})
  cams=h.get('telemetry',{}).get('cameras',[])
  if h.get('ready')and cams and all(c.get('processing',{}).get('totals',{}).get('inference_frames',0)>10 for c in cams):break
  time.sleep(3)
 else:raise RuntimeError('Kubernetes camera initialization timeout')
 for i in range(12):
  time.sleep(5);samples.append({'time':time.time(),'phase':'steady','health':health()})
  if (root/'THERMAL_ABORT.json').exists():raise RuntimeError('thermal guardrail')
 state['revision']=2;state['cameras'][0]['geometry']['zones'][0]['poly'][0]=[.02,.02]
 kubectl('-n','sentry-validation','patch','configmap','inputs','--type=merge','-p',json.dumps({'data':{'desired_state.json':json.dumps(state)}}))
 projection_started=time.monotonic();deadline=projection_started+150
 while time.monotonic()<deadline:
  h=health();samples.append({'time':time.time(),'phase':'projected_config_update','health':h})
  if h.get('revision')==2 and h.get('ready'):break
  if (root/'THERMAL_ABORT.json').exists():raise RuntimeError('thermal guardrail')
  time.sleep(3)
 verdict['projected_config_revision_two']=h.get('revision')==2
 verdict['projected_update_seconds']=time.monotonic()-projection_started
 time.sleep(5)
 kubectl('-n','sentry-validation','exec','sentry-validation','-c','agent','--','cat','/state/agent-status.json',save='agent-status.json')
 kubectl('-n','sentry-validation','exec','sentry-validation','-c','agent','--','cat','/state/packages.jsonl',save='packages.jsonl')
 kubectl('-n','sentry-validation','get','pod','sentry-validation','-o','json',save='pod.json')
 kubectl('-n','sentry-validation','logs','sentry-validation','-c','cv',save='cv.log')
 (out/'samples.json').write_text(json.dumps(samples))
 agent=json.loads((out/'agent-status.json').read_text());packages=[json.loads(x)for x in(out/'packages.jsonl').read_text().splitlines()];revisions=sorted({x.get('config_revision')for x in packages if x.get('config_revision')is not None})
 timeline=[]
 for sample in samples:
  for camera in sample['health'].get('telemetry',{}).get('cameras',[]):
   totals=camera.get('processing',{}).get('totals',{});timeline.append({'time':sample['time'],'inference_frames':totals.get('inference_frames',0),'quiet_frames':totals.get('quiet_frames',0)})
 quiet=next((x for x in timeline if x['quiet_frames']>10),None)
 bursts=[b for a,b in zip(timeline,timeline[1:])if quiet and a['time']>=quiet['time']and 0<b['time']-a['time']<=12 and (b['inference_frames']-a['inference_frames'])/(b['time']-a['time'])>=2]
 woke=bool(bursts)
 quiet_again=bool(bursts and any(b['time']>bursts[0]['time']and b['quiet_frames']-bursts[0]['quiet_frames']>=10 for b in timeline))
 verdict['motion_negative_control'].update(quiet_observed=quiet is not None,substantial_inference_after_quiet=woke,quiet_after_wake=quiet_again,note='Counter evidence plus1Hz trace: >=2 inferenceFPS in an interval <=12s after quiet, then >=10 additional quiet frames. Five-second safety sweeps alone cannot satisfy that burst criterion.')
 verdict.update(passed=bool(samples[-1]['health'].get('ready')and agent.get('packages',agent.get('package_count',0))>0 and verdict.get('projected_config_revision_two')and 2 in revisions),agent_status=agent,final_health=samples[-1]['health'],api_observation_revisions=revisions)
except Exception as e:verdict['error']=type(e).__name__+': '+str(e)
finally:
 if config.exists():
  try:
   kubectl('-n','sentry-validation','describe','pod','sentry-validation',save='pod-description.txt')
   kubectl('-n','sentry-validation','delete','pod','sentry-validation','--ignore-not-found=true','--wait=true','--timeout=30s',timeout=40)
  except Exception:pass
 if sender:
  try:os.killpg(sender.pid,signal.SIGTERM);sender.wait(10)
  except Exception:pass
 if server:
  descendants=[]
  try:descendants=[(p.pid,p.create_time())for p in psutil.Process(server.pid).children(recursive=True)]
  except psutil.Error:pass
  try:os.killpg(server.pid,signal.SIGTERM);server.wait(30)
  except Exception:pass
  owned=[]
  for pid,created in descendants:
   try:
    p=psutil.Process(pid)
    if abs(p.create_time()-created)<.01:p.terminate();owned.append(p)
   except psutil.Error:pass
  gone,alive=psutil.wait_procs(owned,timeout=5)
  for p in alive:
   try:p.kill()
   except psutil.Error:pass
 # K3s keeps this credential outside --data-dir. Preserve the original
 # stopped cluster's node credential, and never export its value.
 try:
  original=json.loads((root/'inventory/original-node-password-fingerprint.json').read_text())
  nodepass=Path('/etc/rancher/node/password')
  if original['exists']:
   if not nodepass.exists()or hashlib.sha256(nodepass.read_bytes()).hexdigest()!=original['sha256']:
    nodepass.write_bytes((root/'inventory/original-node-password.private-backup').read_bytes());nodepass.chmod(int(original['mode'],8))
   verdict['original_node_credential_preserved']=hashlib.sha256(nodepass.read_bytes()).hexdigest()==original['sha256']
  elif nodepass.exists()and server is not None:
   nodepass.unlink();verdict['original_node_credential_preserved']=True
 except Exception as e:verdict['node_credential_restore_error']=type(e).__name__+': '+str(e)
 (out/'commands.json').write_text(json.dumps(events,indent=2));(out/'verdict.json').write_text(json.dumps(verdict,indent=2));print(json.dumps({'passed':verdict['passed'],'error':verdict.get('error')}))
