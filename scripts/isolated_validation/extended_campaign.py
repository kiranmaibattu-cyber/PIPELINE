#!/usr/bin/env python3
"""Additional media, fault, latency and input checks after the one-hour campaign."""
from pathlib import Path
import json,os,subprocess,time
import campaign as c
root=c.ROOT
while True:
 try:state=json.loads((root/'STATUS.json').read_text())
 except Exception:state={}
 if state.get('phase')=='initial-campaign-complete':break
 if state.get('phase')=='extended-complete':raise SystemExit(0)
 time.sleep(10)
c.RESULTS=json.loads(c.LEDGER.read_text())
c.write(c.STATUS,{'phase':'extended-starting','completed':len(c.RESULTS)})
# Invalid pre-fix role profiles are preserved; these are new IDs with validated trigger bindings.
for profile,fixture,counts in [('scene','crowd-1080p.mp4',[4,8,12,16,20,24]),('identity','crowd-1080p.mp4',[4,8,12]),('vehicle','ANPR.mp4',[4,8,12])]:
 for n in counts:
  row=c.run('valid-bindings-scale-'+profile+'-'+str(n),n=n,profile=profile,fixture=fixture,steady=75)
  if not row.get('candidate_passing_point'):break
# Run controlled production-policy scenarios against baked V8 code, no GPU activity.
output=root/'artifacts/policy-scenarios';output.mkdir(exist_ok=True)
cmd=['docker','run','--rm','--network=host','-e','PYTEST_DISABLE_PLUGIN_AUTOLOAD=1','-e','PYTHONPATH=/testdeps','-v',str(root/'testdeps')+':/testdeps:ro','-v',str(c.REPO/'scripts/isolated_validation/test_policy_scenarios.py')+':/tests/test_policy_scenarios.py:ro','--entrypoint','python3',c.IMAGE,'-m','pytest','-q','-p','no:cacheprovider','/tests/test_policy_scenarios.py']
with (output/'result.txt').open('w') as f:r=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,timeout=180)
(output/'verdict.json').write_text(json.dumps({'returncode':r.returncode,'kind':'injected-observation policy scenarios, not detector accuracy'}))
with (output/'rejected-feature-diagnostic.json').open('w')as f:
 subprocess.run(['docker','run','--rm','--tmpfs','/tmp','-v',str(c.REPO/'scripts/isolated_validation/rejected_feature_diagnostic.py')+':/probe.py:ro','--entrypoint','python3',c.IMAGE,'/probe.py'],stdout=f,stderr=subprocess.STDOUT,timeout=60)
configout=root/'artifacts/configuration-checks';configout.mkdir(exist_ok=True)
with (configout/'result.json').open('w') as f:
 subprocess.run(['docker','run','--rm','--tmpfs','/run/secrets/sentinel/cameras:mode=1777','-v',str(c.REPO/'scripts/isolated_validation/configuration_checks.py')+':/probe.py:ro','--entrypoint','python3',c.IMAGE,'/probe.py'],stdout=f,stderr=subprocess.STDOUT,timeout=60)
logout=root/'artifacts/logging-checks';logout.mkdir(exist_ok=True)
with (logout/'result.json').open('w') as f:
 subprocess.run(['docker','run','--rm','-v',str(c.REPO/'scripts/isolated_validation/logging_checks.py')+':/probe.py:ro','--entrypoint','python3',c.IMAGE,'/probe.py'],stdout=f,stderr=subprocess.STDOUT,timeout=60)
for missing in ['GPU','NPU']:
 c.run('edge-device-absent-'+missing,steady=35,extra=['--startup-timeout','35','--container-env','WORKER_CAMERA_INITIALIZATION_SECONDS=20'],capacity=False,env_override={'SENTRY_TEST_HIDE_'+missing:'1'})
c.run('edge-primary-model-unreadable',steady=35,extra=['--startup-timeout','35','--container-env','WORKER_CAMERA_INITIALIZATION_SECONDS=20'],capacity=False,env_override={'SENTRY_TEST_MASK_PRIMARY':'1'})
c.run('edge-action-config-updates',n=2,steady=100,capacity=False,env_override={'SENTRY_TEST_ACTION':json.dumps({'mode':'config_updates','start':15})})
for fixture,profile in [('empty1080.mp4','all'),('empty4k.mp4','scene'),('quiet720.mp4','all'),('motion-transition.mp4','all'),('fire-clear.mp4','hazard'),('portrait.mp4','all'),('nonaligned.mp4','all'),('fps15.mp4','all'),('fps25.mp4','all')]:
 c.run('edge-'+fixture.split('.')[0],n=1,fps=1 if fixture=='empty4k.mp4' else 8,profile=profile,fixture=fixture,steady=90 if 'transition' in fixture or 'clear' in fixture else 60,capacity=False)
c.run('edge-load-ramp-empty-busy-empty',n=8,fps=10,fixture='controlled-load-ramp-1080p.mp4',steady=120,capacity=False)
# Codec capability: current TCP MPEG-TS path assumesH264; HEVC-over-TCP may fail explicitly.
c.run('edge-tcp-hevc',fixture='crowd-1080p-hevc.mp4',steady=45,extra=['--startup-timeout','45'],capacity=False)
c.run('edge-source-fps15-request30',fixture='fps15.mp4',fps=30,steady=60,capacity=False)
c.run('edge-default-fps-omitted',extra=['--omit-fps'],steady=60,capacity=False)
c.run('edge-mixed-fractional-fps',n=3,steady=90,capacity=False,env_override={'SENTRY_TEST_PER_CAMERA_FPS':'[4,8.5,10]'})
for mode in ['socket_absent','socket_permissions','pause_agent','restart_agent','pause_worker','kill_worker','restart_container','cpu_quota','memory_pressure','state_readonly']:
 c.run('edge-action-'+mode,n=2,steady=90,capacity=False,env_override={'SENTRY_TEST_ACTION':json.dumps({'mode':mode,'start':20,'duration':10}),'SENTRY_TEST_RESTORE_ACKS':'1'})
c.run('edge-api-drain-reconciliation',n=2,steady=90,capacity=False,env_override={'SENTRY_TEST_ACTION':json.dumps({'mode':'api_reconciliation','start':20})})
c.run('edge-native-worker-pause-45s',n=2,steady=130,capacity=False,env_override={'SENTRY_TEST_ACTION':json.dumps({'mode':'pause_worker','start':20,'duration':45})})
# A quiet21-camera/1FPS point can exercise the20+1 native group boundary without claiming active capacity.
c.run('edge-groups-21x1-scene',n=21,fps=1,profile='scene',fixture='quiet720.mp4',steady=90,capacity=False,env_override={'SENTRY_TEST_ACTION':json.dumps({'mode':'kill_worker','start':20,'duration':5})})
# Compare explicit process-group settings as declared diagnostics, not a silent new runtime default.
for grouping in [4,5]:
 c.run('edge-grouping-'+str(grouping)+'-8cams',n=8,profile='all',steady=75,capacity=False,extra=['--container-env','SENTINEL_NATIVE_GRAPH_MAX_CAMERAS_PER_PROCESS='+str(grouping)])
# Reusable independent TCP senders avoid a one-client replay confound in revision changes.
servers=[]
try:
 for port in [19111,19112]:
  servers.append(subprocess.Popen(['python3',str(c.REPO/'scripts/isolated_validation/reusable_sender.py'),'--port',str(port),'--fixture',str(c.FIX/('crowd-1080p.mp4' if port==19111 else 'ANPR.mp4'))],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
 servers.append(subprocess.Popen(['python3',str(c.REPO/'scripts/isolated_validation/reusable_sender.py'),'--port','19113','--fixture',str(c.FIX/'crowd-1080p.mp4'),'--corrupt'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
 servers.append(subprocess.Popen(['python3',str(c.REPO/'scripts/isolated_validation/reusable_sender.py'),'--port','19114','--fixture',str(c.FIX/'crowd-1080p.mp4'),'--throttle-kib','500','--log',str(root/'artifacts/source-bandwidth-sender.jsonl')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
 time.sleep(1)
 c.run('edge-source-bandwidth-cap',n=2,fixture=None,sources=['tcp://127.0.0.1:19114','tcp://127.0.0.1:19111'],steady=100,capacity=False)
 c.run('edge-corrupt-tcp-peer',n=2,fixture=None,sources=['tcp://127.0.0.1:19113','tcp://127.0.0.1:19111'],steady=45,extra=['--startup-timeout','60'],capacity=False)
 c.run('edge-secret-url-rotation',n=1,fixture=None,sources=['tcp://127.0.0.1:19111'],steady=100,capacity=False,env_override={'SENTRY_TEST_ACTION':json.dumps({'mode':'secret_url_rotation','start':20}),'SENTRY_TEST_SCENE_GALLERY_LIMIT':'100'})
 c.run('edge-camera-add-remove',n=2,fixture=None,sources=['tcp://127.0.0.1:19111','tcp://127.0.0.1:19112'],steady=130,capacity=False,env_override={'SENTRY_TEST_ACTION':json.dumps({'mode':'camera_membership','start':15})})
 c.run('edge-peer-unavailable-startup',n=2,fixture=None,sources=['tcp://127.0.0.1:19998','tcp://127.0.0.1:19111'],steady=45,extra=['--startup-timeout','60'],capacity=False)
 c.run('edge-all-unavailable-startup',n=1,fixture=None,sources=['tcp://127.0.0.1:19998'],steady=35,extra=['--startup-timeout','40'],capacity=False)
finally:
 for sender in servers:sender.terminate()
 for sender in servers:sender.wait(10)
# Same live endpoint/five logical consumers; source-side fanout distinguished from image execution.
sources=json.loads((root/'artifacts/preflight/sources.json').read_text());tcp=[s['url'] for s in sources if s.get('returncode')==0 and s['url'].startswith('tcp:')]
if tcp:
 c.run('edge-live-tcp-fanout5',n=5,fixture=None,sources=[tcp[0]],steady=75,capacity=False)
 with (root/'artifacts/raw-tcp-fanout.json').open('w')as f:subprocess.run(['python3',str(c.REPO/'scripts/isolated_validation/raw_fanout.py'),tcp[0]],stdout=f,stderr=subprocess.STDOUT,timeout=35)
# HTTP test server is isolated loopback; native acceptance/rejection recorded, not presumed working.
http=subprocess.Popen(['python3','-m','http.server','19090','--bind','127.0.0.1','--directory',str(c.FIX)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 c.run('edge-http-input',fixture=None,steady=45,extra=['--source-url','http://127.0.0.1:19090/crowd-1080p.mp4','--startup-timeout','45'],capacity=False)
 c.run('edge-https-input',fixture=None,steady=45,extra=['--source-url','https://127.0.0.1:19090/crowd-1080p.mp4','--startup-timeout','35'],capacity=False)
 c.run('edge-local-file-secret',fixture=None,steady=45,extra=['--source-url','/fixtures/crowd-1080p.mp4','--startup-timeout','30'],capacity=False)
finally:http.terminate();http.wait()
for resolution,n in [('720p',1),('1080p30',4),('1440p',4)]:
 case='edge-latency-'+resolution;epoch=root/'artifacts'/case/'sender-epochs';epoch.mkdir(parents=True,exist_ok=True)
 c.run(case,n=n,profile='scene' if n==1 else 'all',fixture='latency-'+resolution+'-420.mp4',steady=60,capacity=False,env_override={'SENTRY_TEST_MARKER_REPLAY':str(epoch),'SENTRY_TEST_GALLERY_LIMIT':'200'})
# Explicit rollback comparison is diagnostic; it changes background learning and is not a main deployment setting.
for fixture in ['crowd-1080p.mp4','crowd-1440p.mp4']:
 for shared in ['0','1']:
  c.run('compare-shared-background-'+shared+'-'+fixture.split('.')[0],n=4,profile='all',fixture=fixture,steady=90,capacity=False,extra=['--container-env','SENTINEL_GAIT_SHARED_BACKGROUND='+shared])
# Frozen opt76 comparison is imported only at this idle boundary, not during
# the hour or another measured run. No image rebuild or gate tuning.
request=root/'REFERENCE_IMPORT_REQUEST.json'
c.write(root/'CURRENT.json',{'id':'reference-import-idle','port':18080})
c.write(c.STATUS,{'phase':'waiting-reference-import','completed':len(c.RESULTS)})
c.write(request,{'image':'localhost/sentry-v8-opt76-body-prefetch','requested_utc':time.time()})
deadline=time.monotonic()+600
while not(root/'REFERENCE_IMPORT_READY.json').exists()and time.monotonic()<deadline:time.sleep(5)
if(root/'REFERENCE_IMPORT_READY.json').exists():
 candidate=c.IMAGE
 try:
  for fixture,n,fps in [('crowd-1080p.mp4',2,8),('crowd-1440p.mp4',4,8)]:
   for version,tag in [('opt76','localhost/sentry-v8-opt76-body-prefetch'),('opt79',candidate)]:
    c.IMAGE=tag
    c.run('frozen-reference-'+version+'-'+fixture.split('.')[0],n=n,fps=fps,fixture=fixture,steady=90,capacity=False)
 finally:c.IMAGE=candidate
else:
 (root/'artifacts/reference-comparison-unavailable.json').write_text(json.dumps({'reason':'Frozen reference import did not complete within 600 seconds; no substitution/rebuild'}))
# Gait silhouette sanity is run offline after performance measurements, not concurrently.
out=root/'artifacts/gait-input-gallery';out.mkdir(exist_ok=True);out.chmod(0o777)
with (out/'diagnostic.log').open('w') as f:
 r=subprocess.run(['docker','run','--rm','--device','/dev/dri','--device','/dev/accel/accel0','--group-add','992','--group-add','44','-v',str(c.FIX)+':/fixtures:ro','-v',str(out)+':/results','-v',str(c.REPO/'scripts/isolated_validation/gait_input_gallery.py')+':/probe.py:ro','--entrypoint','python3',c.IMAGE,'/probe.py'],stdout=f,stderr=subprocess.STDOUT,timeout=180)
(out/'verdict.json').write_text(json.dumps({'returncode':r.returncode,'scope':'silhouette input sanity, not labelled gait recognition accuracy'}))
c.write(c.STATUS,{'phase':'extended-complete','completed':len(c.RESULTS),'next':'configuration/Kubernetes checks and final report reconciliation'})
c.log('EXTENDED CAMPAIGN COMPLETE')
