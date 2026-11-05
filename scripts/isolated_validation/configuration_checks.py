"""Run against the frozen image: real validator, synthetic filesystem inputs."""
import copy,json,tempfile,traceback
from pathlib import Path
from traffic_pilot_runtime.desired_state import DesiredStateValidator

results=[]
with tempfile.TemporaryDirectory() as folder:
 root=Path(folder);secrets=Path('/run/secrets/sentinel/cameras');secrets.mkdir(parents=True,exist_ok=True);validator=DesiredStateValidator(secrets)
 def make(n=1):
  cameras=[]
  for i in range(n):
   name=f'camera-{i+1:02d}';secret=secrets/(name+'.url');secret.write_text('tcp://127.0.0.1:19001\n')
   cameras.append({'camera_id':name,'source':'file:'+str(secret),'bindings':[{'use_case_id':f'scene-{i+1}','app':'scene_search','scope':'full_frame','geometry_ids':[],'object_types':[],'embedding_profile_ids':['siglip2-base-v1'],'sample_interval_seconds':2}]})
  return {'contract':'sentry-v8','schema_version':'8.0','edge_id':'validation','deployment_id':'validation','revision':1,'cameras':cameras}
 def check(name,payload,valid=True,prepare=None):
  try:
   if prepare:prepare(payload)
   validator.validate(payload);accepted=True;error=None
  except Exception as exc:accepted=False;error=(type(exc).__name__+': '+str(exc))[:600]
  results.append({'case':name,'expected_acceptance':valid,'accepted':accepted,'passed':accepted==valid,'error':error})
 for n in [1,8,16,50]:check(f'{n}-camera-schema-secrets',make(n))
 d=make();check('default-fps',d);results.append({'case':'default-fps-is-eight','passed':validator._camera(d['cameras'][0],d).fps==8})
 check('51-camera-rejected',make(51),False)
 for name,mutate in [
  ('duplicate-camera',lambda d:d['cameras'].append(copy.deepcopy(d['cameras'][0]))),
  ('edge-deployment-mismatch',lambda d:d.update(edge_id='other')),
  ('duplicate-binding',lambda d:d['cameras'][0]['bindings'].append(copy.deepcopy(d['cameras'][0]['bindings'][0]))),
  ('missing-geometry',lambda d:d['cameras'][0]['bindings'][0].update(scope='geometry',geometry_ids=['absent'])),
  ('invalid-fps',lambda d:d['cameras'][0].update(fps=0)),
  ('invalid-app',lambda d:d['cameras'][0]['bindings'][0].update(app='unknown')),
  ('unknown-root-field',lambda d:d.update(unexpected=True)),
  ('missing-secret',lambda d:Path(d['cameras'][0]['source'][5:]).unlink()),
  ('bad-secret-url',lambda d:Path(d['cameras'][0]['source'][5:]).write_text('file:///tmp/film.mp4')),
  ('wrong-secret-name',lambda d:d['cameras'][0].update(source='file:'+str(secrets/'camera-02.url'))),
 ]:check(name,make(),False,mutate)
 for scheme in ['rtsp','rtsps','http','https','tcp']:
  check('accepted-scheme-'+scheme,make(),True,lambda d,s=scheme:Path(d['cameras'][0]['source'][5:]).write_text(s+'://127.0.0.1:19001'))
 outside=root/'camera-01.url';outside.write_text('tcp://127.0.0.1:19001');check('outside-secret-root',make(),False,lambda d:d['cameras'][0].update(source='file:'+str(outside)))
 link=secrets/'camera-01.url';link.unlink();link.symlink_to(outside)
 check('symlink-outside-secret-root',{'contract':'sentry-v8','schema_version':'8.0','edge_id':'validation','deployment_id':'validation','revision':1,'cameras':[{'camera_id':'camera-01','source':'file:'+str(link),'bindings':[{'use_case_id':'count','app':'vehicle_counting','scope':'full_frame','geometry_ids':[],'object_types':['vehicle']}]}]},False)
 for name,data in [('malformed','{'),('empty','')]:
  f=root/'state.json';f.write_text(data)
  try:validator.load(f);ok=False;error=None
  except ValueError as e:ok=True;error=str(e)
  results.append({'case':name,'passed':ok,'error':error})
print(json.dumps({'scope':'baked validator and real Secret files; no camera capacity claim','results':results,'passed':all(r['passed']for r in results)},indent=2))
