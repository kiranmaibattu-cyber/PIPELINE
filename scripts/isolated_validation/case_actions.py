"""Out-of-band fault injection for our own named test containers only."""
import json,os,signal,subprocess,threading,time,urllib.request
from pathlib import Path
import psutil
class Actions:
 def __init__(self,root,rest):
  self.root=root;self.rest=rest;self.stop=threading.Event();self.children=[];self.thread=None
  self.config=json.loads(os.environ.get('SENTRY_TEST_ACTION','{}'))
  if self.config:self.thread=threading.Thread(target=self.work,daemon=True);self.thread.start()
 def note(self,event,**details):
  case=json.loads((self.root/'CURRENT.json').read_text())['id'];p=self.root/'artifacts'/case;p.mkdir(parents=True,exist_ok=True)
  with (p/'actions.jsonl').open('a') as f:f.write(json.dumps({'event':event,'mono':time.monotonic(),'utc':time.time(),**details})+'\n')
 def command(self,args):
  r=subprocess.run(args,capture_output=True,text=True,timeout=45);self.note('command',argv=args,returncode=r.returncode,output=(r.stdout+r.stderr)[-1200:]);return r
 def health(self):
  try:
   with urllib.request.urlopen('http://127.0.0.1:18080/healthz',timeout=1) as r:return json.load(r)
  except Exception:return {}
 def sources(self,h,pid):
  if os.environ.get('SENTRY_TEST_REUSABLE_REPLAY'):
   self.note('source_replay_is_persistent',reason='Existing test servers accept replacement decoder connections');return
  recording=self.rest[self.rest.index('--recording')+1] if '--recording' in self.rest else None
  if not recording:return
  suite=json.loads(os.environ.get('SENTRY_TEST_RECORDINGS','[]'))
  for camera in h.get('telemetry',{}).get('cameras',[]):
   if pid is not None and (camera.get('decoder') or {}).get('camera_process_pid')!=pid:continue
   index=int(camera['camera_id'].rsplit('-',1)[1])-1;port=19001+index;path=suite[index%len(suite)] if suite else recording
   # A terminated one-client replay sender releases its port before replacement.
   p=subprocess.Popen(['ffmpeg','-v','error','-re','-stream_loop','-1','-i',path,'-map','0:v:0','-an','-c:v','copy','-f','mpegts','-listen','1','tcp://127.0.0.1:'+str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   self.children.append(p);self.note('source_relaunched',camera=camera['camera_id'],pid=p.pid)
 def work(self):
  try:
   deadline=time.monotonic()+150;h={}
   while not self.stop.is_set() and time.monotonic()<deadline:
    h=self.health()
    if h.get('ready') and h.get('telemetry',{}).get('cameras'):break
    self.stop.wait(1)
   if not h.get('ready'):self.note('action_not_executed',reason='no ready worker');return
   if self.stop.wait(self.config.get('start',20)):return
   names=subprocess.check_output(['docker','ps','--filter','name=sentinel-v8-capacity','--format','{{.Names}}'],text=True).splitlines()
   if len(names)!=1:raise RuntimeError('expected exactly one owned test container')
   name=names[0];h=self.health();mode=self.config['mode'];duration=self.config.get('duration',10)
   self.note('before',mode=mode,health=h)
   camera=h.get('telemetry',{}).get('cameras',[{}])[0];pid=(camera.get('decoder') or {}).get('camera_process_pid')
   def sig(which):return self.command(['docker','exec','-u','0',name,'python3','-c',f'import os,signal;os.kill({int(pid)},{int(which)})'])
   if mode=='secret_url_rotation':
    data=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    secretroot=Path(next(m['Source']for m in data['Mounts']if m['Destination']=='/run/secrets/sentinel/cameras'))
    path=Path(next(m['Source']for m in data['Mounts']if m['Destination']=='/configs/desired_state.json'))
    desired=json.loads(path.read_text());secret=secretroot/(desired['cameras'][0]['camera_id']+'.url')
    secret.write_text('tcp://127.0.0.1:19112\n');desired['revision']=2
    with path.open('w')as f:f.write(json.dumps(desired));f.flush();os.fsync(f.fileno())
    self.note('secret_rotation_submitted',target_port=19112,desired_revision=2)
    self.stop.wait(35);self.note('secret_rotation_result',health=self.health())
   elif mode=='api_reconciliation':
    data=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    folder=Path(next(m['Source'] for m in data['Mounts'] if m['Destination']=='/run/sentinel-delivery')).parent
    sig(signal.SIGSTOP)
    try:
     deadline=time.monotonic()+15
     while time.monotonic()<deadline:
      self.stop.wait(1);current=self.health();scheduler=current.get('telemetry',{}).get('handoff_scheduler',{})
      if scheduler.get('retained_packages')==0 and scheduler.get('counters',{}).get('admitted')==scheduler.get('counters',{}).get('durable'):break
     self.stop.wait(2)
     current=self.health();scheduler=current.get('telemetry',{}).get('handoff_scheduler',{});status=json.loads((folder/'agent-status.json').read_text());packages=[json.loads(x)for x in(folder/'packages.jsonl').read_text().splitlines()]
     ids=[x['package_id']for x in packages];objects=[(s['object_type'],s['object_id'])for x in packages for s in x['sequences']];seqs=sorted(s['sequence']for x in packages for s in x['sequences']);counters=scheduler.get('counters',{})
     expected_accepted=int(counters.get('admitted',0))-int(counters.get('coalesced',0))-int(counters.get('exhausted',0))
     passed=bool(scheduler.get('retained_packages')==0 and expected_accepted==counters.get('durable')==len(set(ids))==status['packages'] and not counters.get('protocol_errors') and not counters.get('exhausted') and len(ids)==len(set(ids)) and len(objects)==len(set(objects)) and seqs==list(range(1,status['next_sequence'])))
     self.note('api_common_endpoint_reconciliation',passed=passed,expected_accepted_after_allowed_coalescing=expected_accepted,scheduler=scheduler,api_status=status,accepted_unique_package_ids=len(set(ids)),accepted_objects=len(objects),unique_objects=len(set(objects)),sequence_contiguous=seqs==list(range(1,status['next_sequence'])))
    finally:sig(signal.SIGCONT)
   elif mode=='camera_membership':
    data=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    path=Path(next(m['Source'] for m in data['Mounts'] if m['Destination']=='/configs/desired_state.json'))
    original=json.loads(path.read_text());reduced=json.loads(path.read_text());reduced['cameras']=reduced['cameras'][:1];reduced['revision']=2
    for label,payload in [('remove-camera',reduced),('add-camera',dict(original,revision=3))]:
     with path.open('w')as f:f.write(json.dumps(payload));f.flush();os.fsync(f.fileno())
     self.stop.wait(30);self.note('membership_step',label=label,health=self.health())
   elif mode=='config_updates':
    data=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    path=Path(next(m['Source'] for m in data['Mounts'] if m['Destination']=='/configs/desired_state.json'))
    original=json.loads(path.read_text());current=json.loads(path.read_text())
    def apply(label,payload,wait=7):
     # File bind mount: write the existing inode, matching the mounted-file contract.
     with path.open('w') as f:
      f.write(payload if isinstance(payload,str) else json.dumps(payload));f.flush();os.fsync(f.fileno())
     self.stop.wait(wait);self.note('config_step',label=label,health=self.health())
    current['revision']=2
    for cam in current['cameras']:
     for z in cam.get('geometry',{}).get('zones',[]):z['poly'][0]=[0.02,0.02]
    apply('higher-revision-geometry',current)
    stale=json.loads(json.dumps(current));stale['cameras'][0]['geometry']['zones'][0]['poly'][0]=[0.03,0.03]
    apply('changed-content-same-revision',stale)
    apply('malformed-json','{')
    current['revision']=3;apply('restore-higher-revision',current)
    secretroot=Path(next(m['Source'] for m in data['Mounts'] if m['Destination']=='/run/secrets/sentinel/cameras'))
    secret=secretroot/(current['cameras'][0]['camera_id']+'.url');oldmode=secret.stat().st_mode&0o777
    try:
     secret.chmod(0);current['revision']=4;apply('unreadable-secret',current)
    finally:secret.chmod(oldmode)
    current['revision']=5;apply('restored-secret-revision-five',current)
    import urllib.error
    for endpoint in ['/healthz','/healthy','/metrics']:
     try:
      with urllib.request.urlopen('http://127.0.0.1:18080'+endpoint,timeout=3) as response:self.note('endpoint',path=endpoint,status=response.status,length=len(response.read()))
     except urllib.error.HTTPError as error:self.note('endpoint',path=endpoint,status=error.code)
   elif mode=='pause_worker':
    sig(signal.SIGSTOP);self.stop.wait(duration);sig(signal.SIGCONT)
   elif mode=='kill_worker':
    sig(signal.SIGKILL);self.stop.wait(3);self.sources(h,pid)
   elif mode=='restart_container':
    self.command(['docker','restart','--time','5',name]);self.stop.wait(2);self.sources(h,None)
   elif mode in ['socket_absent','socket_permissions']:
    data=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
    folder=Path(next(m['Source'] for m in data['Mounts'] if m['Destination']=='/run/sentinel-delivery'));sock=folder/'agent.sock'
    if mode=='socket_absent':
     hidden=folder/'agent.sock.hidden';sock.rename(hidden)
     try:self.stop.wait(duration)
     finally:hidden.rename(sock)
    else:
     old=sock.stat().st_mode&0o777;sock.chmod(0)
     try:self.stop.wait(duration)
     finally:sock.chmod(old)
   elif mode in ['pause_agent','restart_agent']:
    candidates=[]
    for p in psutil.process_iter(['cmdline']):
     try:
      cmd=p.info['cmdline'] or []
      if any(x.endswith('mock_delivery_agent.py') for x in cmd) and '--socket' in cmd:candidates.append((p,cmd))
     except (psutil.NoSuchProcess,psutil.AccessDenied):pass
    if len(candidates)!=1:raise RuntimeError('expected one test receiver')
    p,cmd=candidates[0]
    if mode=='pause_agent':
     p.send_signal(signal.SIGSTOP);self.stop.wait(duration);p.send_signal(signal.SIGCONT)
    else:
     p.terminate();p.wait(5);self.stop.wait(duration)
     child=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=os.environ.copy());self.children.append(child)
   elif mode=='cpu_quota':
    self.command(['docker','update','--cpus','1',name]);self.stop.wait(duration);self.command(['docker','update','--cpus','0',name])
   elif mode=='memory_pressure':
    self.command(['docker','update','--memory','6g','--memory-swap','6g',name])
    child=subprocess.Popen(['docker','exec',name,'python3','-c',f'import time;a=bytearray(1536*1024*1024);time.sleep({duration})'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);self.children.append(child)
    self.stop.wait(duration+1)
    self.command(['docker','update','--memory','0','--memory-swap','-1',name])
   elif mode=='state_readonly':
    path='/tmp/sentinel/state/metrics'
    old=self.command(['docker','exec',name,'stat','-c','%a',path]).stdout.strip()
    self.command(['docker','exec','-u','0',name,'chmod','a-w',path]);self.stop.wait(duration)
    if old:self.command(['docker','exec','-u','0',name,'chmod',old,path])
   self.note('after',health=self.health())
  except Exception as e:self.note('action_error',error=type(e).__name__+': '+str(e))
 def close(self):
  self.stop.set()
  if self.thread:self.thread.join(20)
  for p in self.children:
   if p.poll() is None:
    p.terminate()
    try:p.wait(5)
    except subprocess.TimeoutExpired:p.kill();p.wait()
