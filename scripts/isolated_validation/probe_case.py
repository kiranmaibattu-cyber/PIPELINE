#!/usr/bin/env python3
"""Scenario adapter around unchanged runtime image and existing replay harness."""
import argparse,os,sys,json,time
from pathlib import Path
repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(repo/'scripts'))
import v8_capacity_probe as probe
p=argparse.ArgumentParser(allow_abbrev=False);p.add_argument('--case',required=True);p.add_argument('--profile',default='all');p.add_argument('--root',type=Path,default=repo.parent);p.add_argument('--sources-json');p.add_argument('--recordings');p.add_argument('--dry-run',action='store_true');p.add_argument('--dump-desired',action='store_true');args,rest=p.parse_known_args()
if args.dry_run:
 print(json.dumps({'recordings':args.recordings,'forwarded':rest}));raise SystemExit(0)
root=args.root;os.environ['PATH']=str(repo/'scripts/isolated_validation')+':'+os.environ['PATH']
base=probe.desired_state
profiles={'scene':{'scene_search'},'presence':{'scene_search','person_presence','vehicle_presence','people_counting','vehicle_counting','vehicle_entry_exit_counts'},'identity':{'scene_search','person_presence','person_identity'},'vehicle':{'scene_search','vehicle_presence','vehicle_counting','vehicle_entry_exit_counts','anpr'},'hazard':{'scene_search','fire_smoke'}}
def desired(*a,**kw):
 kw['full_usecases']=True;d=base(*a,**kw)
 for i,c in enumerate(d['cameras']):
  if os.environ.get('SENTRY_TEST_PER_CAMERA_FPS'):
   rates=json.loads(os.environ['SENTRY_TEST_PER_CAMERA_FPS']);assert len(rates)==len(d['cameras']);c['fps']=rates[i]
  if args.profile in profiles:c['bindings']=[b for b in c['bindings'] if b['app'] in profiles[args.profile]]
  elif args.profile.startswith('mixed'):
   fraction=float(args.profile.removeprefix('mixed'))
   keep=profiles['identity'] if i<round(len(d['cameras'])*fraction) else profiles['vehicle']
   c['bindings']=[b for b in c['bindings'] if b['app'] in keep]
  enabled={b['use_case_id'] for b in c['bindings']}
  for b in c['bindings']:
   if 'scene_trigger_use_case_ids' in b:b['scene_trigger_use_case_ids']=[x for x in b['scene_trigger_use_case_ids'] if x in enabled]
 return d
probe.desired_state=desired
if args.dump_desired:
 print(json.dumps(desired(4,8)));raise SystemExit(0)
if args.sources_json:probe.SOURCE_URLS=json.loads(args.sources_json)
if args.recordings:os.environ['SENTRY_TEST_RECORDINGS']=args.recordings
if os.environ.get('SENTRY_TEST_ACTION')and '--recording'in rest and not os.environ.get('SENTRY_TEST_MARKER_REPLAY'):
 os.environ['SENTRY_TEST_REUSABLE_REPLAY']='1'
 directory=root/'artifacts'/args.case;directory.mkdir(parents=True,exist_ok=True)
 (directory/'persistent-replay.json').write_text(json.dumps({'enabled':True,'scope':'Test producer stays listening across decoder/worker/container reconnect; frozen CV input/decoder unchanged.'}))
if 'stress-60min-all-' in args.case:
 # Keep the same underlying people/vehicle content, source geometry/rate and
 # full bindings, adding test pixels and independent sender timestamps only.
 count=int(rest[rest.index('--cameras')+1]);fixtures=root/'fixtures'
 actual=[str(fixtures/('latency-1080p30-420.mp4' if i%2==0 else 'latency-anpr720p10-420.mp4')) for i in range(count)]
 assert all(Path(x).is_file() for x in actual),'Missing marked hour fixture'
 original=list(rest);rest[rest.index('--recording')+1]=actual[0]
 os.environ['SENTRY_TEST_RECORDINGS']=json.dumps(actual)
 epoch=root/'artifacts'/args.case/'sender-epochs';epoch.mkdir(parents=True,exist_ok=True)
 os.environ['SENTRY_TEST_MARKER_REPLAY']=str(epoch)
 os.environ['SENTRY_TEST_SCENE_GALLERY_LIMIT']='4000'
 (epoch.parent/'external-marker-instrumentation.json').write_text(json.dumps({'original_argv':original,'actual_recordings':actual,'purpose':'Continuous externally marked scene freshness during the complete hour; no runtime/gate/contract edits','source_rates':[30 if i%2==0 else 10 for i in range(count)]},indent=2))
if args.profile!='all' and '--full-usecases' in rest:rest.remove('--full-usecases')
root.mkdir(exist_ok=True,parents=True);tmp=root/'CURRENT.new';tmp.write_text(json.dumps({'id':args.case,'port':18080,'profile':args.profile,'argv':rest,'started_utc':time.time()}));tmp.replace(root/'CURRENT.json')
from case_actions import Actions
actions=Actions(root,rest)
sys.argv=[sys.argv[0]]+rest
try:code=probe.main()
except BaseException as exc:
 (root/'artifacts'/args.case).mkdir(parents=True,exist_ok=True)
 (root/'artifacts'/args.case/'driver-error.json').write_text(json.dumps({'type':type(exc).__name__,'message':str(exc)}))
 raise
finally:
 actions.close()
 tmp=root/'CURRENT.new';tmp.write_text(json.dumps({'id':'idle','finished_case':args.case}));tmp.replace(root/'CURRENT.json')
raise SystemExit(code)
