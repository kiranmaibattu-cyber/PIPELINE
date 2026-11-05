"""Independent source-PES-to-agent-API latency, including continuous marked replays."""
import json,cv2,sys,numpy as np,gzip,datetime
from pathlib import Path

def align(items,pes,parameters):
 records=[];counts={'ambiguous_repeat':0,'missing_sender_packet':0,'ordered_loop_unwrap':0};previous={}
 for item in sorted(items,key=lambda x:(x['camera_id'],x['stream_session_id'],x['frame_ordinal'])):
  camera=item['camera_id'];key=(camera,item['stream_session_id']);n=item['marker'];accepted=item['accepted_mono'];packets=pes.get(camera,{})
  fps,length=parameters.get(camera,(10,1200));old=previous.get(key);match=None
  if old is not None:
   delta=(n-old['marker'])%length;gap=accepted-old['accepted_mono']
   # Ordered images anchor cycle identity. A large observation gap/jump makes
   # loop count unknowable; preserve that uncertainty rather than choosing a
   # recently transmitted copy of an old, delayed frame.
   if abs(gap)<length/fps/2 and delta/fps<=max(30,abs(gap)+10):
    index=old['source_index']+delta;candidate=packets.get(index)
    if candidate and candidate['transmitted_mono']<=accepted:
     match=candidate;counts['ordered_loop_unwrap']+=int(index>=length)
  if match is None:
   options=[x for x in packets.values()if x['marker_index']==n and x['transmitted_mono']<=accepted]
   if len(options)==1:match=options[0]
   elif not options:counts['missing_sender_packet']+=1
   else:counts['ambiguous_repeat']+=1
  if match is None:previous.pop(key,None);continue
  previous[key]={'source_index':match['source_index'],'marker':n,'accepted_mono':accepted}
  records.append({**item,'source_index':match['source_index'],'transport_to_api_ms':(accepted-match['transmitted_mono'])*1000})
 return records,counts

def analyze(root,epochs):
 case=root.parent;api={}
 with gzip.open(case/'packages.jsonl.gz','rt')as f:
  for line in f:
   r=json.loads(line);api[r['package_id']]=r
 report=json.loads((case/'report.json').read_text());samples=[s for s in report['samples']if s.get('sample_phase')=='steady'];start=samples[0]['sample_monotonic_seconds'];end=samples[-1]['sample_monotonic_seconds']
 pes={};parameters={}
 for p in epochs.glob('*-pes.jsonl'):
  port=int(p.name.split('-')[0]);camera=f'capacity-{port-19000:02d}';pes[camera]={r['source_index']:r for r in map(json.loads,p.read_text().splitlines())}
  epoch=json.loads((epochs/(str(port)+'.json')).read_text());parameters[camera]=(epoch.get('source_fps',10),epoch.get('source_frames',1200))
 items=[];others=[];counts={'marker_decoded':0,'no_marker':0};other_counts={'eligible':0,'marker_decoded':0,'no_marker':0,'unsupported_frame_id':0}
 for p in root.glob('*/manifest.json'):
  m=json.loads(p.read_text());o=m['observation']
  scene=o.get('event_type')=='scene_sample'
  if not scene and o.get('event_type')not in {'plate_read','plate_outcome','line_cross','zone_entry','zone_exit'}:continue
  try:ordinal=int(o['source_frame_id'].rsplit(':',1)[1])
  except (ValueError,KeyError,AttributeError):
   if not scene:other_counts['unsupported_frame_id']+=1
   continue
  if not scene:
   if not any(e['metadata'].get('source_bbox_normalized')is None for e in m['evidence']):continue
   other_counts['eligible']+=1
  n=None
  for evidence in m['evidence']:
   if not scene and evidence['metadata'].get('source_bbox_normalized')is not None:continue
   im=cv2.imread(str(p.parent/(evidence['part_name']+'.jpg')))
   if im is None:continue
   scale=im.shape[1]/1280.;y=int(706*scale)
   if y>=im.shape[0]:continue
   bits=''.join('1'if im[y,int((8+12*i+6)*scale)].mean()>127 else'0'for i in range(56))
   if int(bits[:16],2)!=0xA55A:continue
   candidate=int(bits[16:48],2);crc=int(bits[48:],2)
   if sum(candidate.to_bytes(4,'big'))&255!=crc:continue
   n=candidate;break
  target=counts if scene else other_counts
  if n is None:target['no_marker']+=1;continue
  target['marker_decoded']+=1;ack=api[m['package_id']]
  runtime_age=(datetime.datetime.fromisoformat(ack['accepted_at']).timestamp()-datetime.datetime.fromisoformat(o['observed_at'].replace('Z','+00:00')).timestamp())*1000
  (items if scene else others).append({'camera_id':o['camera_id'],'stream_session_id':o['stream_session_id'],'frame_ordinal':ordinal,'event_type':o['event_type'],'marker':n,'package_id':m['package_id'],'observed_at':o['observed_at'],'accepted_mono':ack['accepted_mono'],'runtime_observed_to_api_ms_signed':runtime_age,'steady':start<=ack['accepted_mono']<=end})
 records,extra=align(items,pes,parameters);counts.update(extra)
 def summary(a):return {'samples':len(a),**{k:float(np.percentile(a,q))if a else None for k,q in [('min',0),('p50',50),('p95',95),('p99',99),('max',100)]}}
 result={'definition':'Source frame first video-PES packet submitted to TCP socket through complete JPEG/vector package acceptance at agent API. Includes transport/decode/inference/policy/evidence/handoff. Excludes camera capture/encoder and internal agent/Management work. Runtime zero-clamped age is not the primary measure.','continuous_unwrap_rule':'First marker must have one prior matching transmission. Following images are ordered by camera/session/source-frame ordinal; modulo progression tracks repeats. Gaps >= half the120s loop or implausible progress invalidate alignment, rather than selecting a recent copy of a delayed old frame.','counts':counts,'steady_per_camera_ms':{c:summary([x['transport_to_api_ms']for x in records if x['camera_id']==c and x['steady']])for c in sorted({x['camera_id']for x in records})},'ten_minute_checkpoints':{str(k):{c:summary([x['transport_to_api_ms']for x in records if x['camera_id']==c and start+k*600<=x['accepted_mono']<start+(k+1)*600])for c in sorted({x['camera_id']for x in records})}for k in range(max(1,int((end-start)/600)+1))},'records':records}
 other_records,other_alignment=align(items+others,pes,parameters)
 other_records=[x for x in other_records if x['event_type']!='scene_sample']
 result['other_fullframe_events']={'scope':'Additional full-frame JPEGs only. Crops/unparseable branch IDs excluded. Scene anchors unwrap loops; age includes candidate/temporal confirmation. Gallery sampling may be partial, so small samples do not establish an SLA.','counts':other_counts,'alignment':other_alignment,'steady_by_type_ms':{kind:summary([x['transport_to_api_ms']for x in other_records if x['event_type']==kind and x['steady']])for kind in sorted({x['event_type']for x in other_records})},'records':other_records}
 return result

if __name__=='__main__':print(json.dumps(analyze(Path(sys.argv[1]),Path(sys.argv[2])),indent=2))
