from pathlib import Path
import json,sys,shutil,gzip
from datetime import datetime
root=Path(__file__).resolve().parent
rows=[]
for label,log in zip(sys.argv[1::2],sys.argv[2::2]):
 entries=[json.loads(line) for line in Path(log).read_text().splitlines() if line.startswith('{')]
 if not entries:continue
 r=entries[-1];src=Path(r['artifact_directory']);out=root/label;out.mkdir(exist_ok=True)
 for name in ['report.json','healthz-final.json','agent-status.json']:
  if (src/name).exists():shutil.copy2(src/name,out/name)
 if (src/'config').exists():shutil.copytree(src/'config',out/'config',dirs_exist_ok=True)
 for name in ['container.log','packages.jsonl','agent.log']:
  if (src/name).exists():
   with (src/name).open('rb') as i,gzip.open(out/(name+'.gz'),'wb') as o:shutil.copyfileobj(i,o)
 h=json.loads((src/'healthz-final.json').read_text()); cameras=h.get('telemetry',{}).get('cameras',[])
 stages={}
 for c in cameras:
  for key,v in c.get('use_case_processing',{}).items():
   if any(term in key for term in ['gait','body_embedding','face','scene','fire','plate_detection','ocr']):
    stages.setdefault(key,[]).append({'camera_id':c['camera_id'],'calls':v.get('calls_total'),'avg_ms':v.get('lifetime_avg_ms')})
 row={'label':label,'image':r['image'],'passed':r['passed'],'full_usecases':r['full_usecases'],'steady':r.get('steady_throughput'),'end_health':r.get('end_health'),'final':r.get('final'),'packages':r.get('package_summary'),'fatal_lines':r.get('fatal_lines'),'stages':stages,'source_latency':{c['camera_id']:c.get('outputs',{}).get('production_latency_by_event_type') for c in cameras}}
 steady=[x for x in r.get('samples',[]) if x.get('sample_phase')=='steady']
 if len(steady)>1:
  first,last=steady[0],steady[-1];duration=last['sample_monotonic_seconds']-first['sample_monotonic_seconds']
  row['inference_fps_cumulative']={cam:(last['per_camera'][cam]['totals'].get('inference_frames',0)-values['totals'].get('inference_frames',0))/duration for cam,values in first['per_camera'].items()}
 spans={}
 if (src/'packages.jsonl').exists():
  for line in (src/'packages.jsonl').read_text().splitlines():
   obj=json.loads(line)
   if obj.get('event_type')=='scene_sample' and obj.get('observed_at'):
    spans.setdefault(obj['camera_id'],[]).append(datetime.fromisoformat(obj['observed_at'].replace('Z','+00:00')).timestamp())
 row['scene_source_span_seconds']={k:max(v)-min(v) for k,v in spans.items()}
 rows.append(row)
(root/'comparison.json').write_text(json.dumps(rows,indent=2))
for r in rows:
 f=r['final'];print(r['label'],r['passed'],'selected',f['aggregate_decode_fps'],'inference',f['aggregate_inference_fps'],'RAM',f['container_memory_mb'],'packages',r['packages'])
