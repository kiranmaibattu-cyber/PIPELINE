"""Offline frozen-image audit of sampled JPEGs and complete embedding manifests."""
import collections,hashlib,json,math,sys
from pathlib import Path
import cv2
root=Path(sys.argv[1]);counts=collections.Counter();errors=[];dimensions={};norms=collections.defaultdict(list);sizes=collections.defaultdict(list)
expected={'adaface-ir101-v18.1':512,'transreid-ssl-v18.1':384,'gaitbase-v18.1':4096,'siglip2-base-v1':768}
pattern = sys.argv[2] if len(sys.argv) > 2 else 'a*/gallery/*/manifest.json'
for p in root.glob(pattern):
 if len(sys.argv)>3 and sys.argv[3] in p.relative_to(root).parts:continue
 try:
  m=json.loads(p.read_text());o=m['observation'];oid=o['observation_id'];counts['sampled_packages']+=1
  evidence={x['metadata']['evidence_id']:x for x in m['evidence']}
  assert set(o.get('evidence_ids',[]))==set(evidence),'promised evidence mismatch'
  assert set(o.get('embedding_ids',[]))=={x['embedding_id'] for x in m['embeddings']},'promised embedding mismatch'
  for item in evidence.values():
   meta=item['metadata'];b=(p.parent/(item['part_name']+'.jpg')).read_bytes();image=cv2.imread(str(p.parent/(item['part_name']+'.jpg')))
   assert image is not None,'undecodable JPEG'
   assert image.shape[:2]==(meta['height'],meta['width']),'JPEG dimensions mismatch'
   assert len(b)==meta['size_bytes'],'size mismatch'
   assert 'sha256:'+hashlib.sha256(b).hexdigest()==meta['sha256'],'hash mismatch'
   assert meta['observation_id']==oid,'evidence parent mismatch'
   if meta.get('source_frame_id'):assert meta['source_frame_id'].startswith(o['camera_id']+':'),'evidence camera mismatch'
   box=meta.get('source_bbox_normalized')
   if box:assert 0<=box['x1']<box['x2']<=1 and 0<=box['y1']<box['y2']<=1,'source crop outside bounds'
   counts['JPEGs']+=1;counts['evidence_'+meta['evidence_type']]+=1
   sizes[meta['evidence_type']].append(len(b))
  for e in m['embeddings']:
   vector=e['vector'];profile=e['profile_id'];assert len(vector)==expected[profile],'embedding dimension'
   assert all(math.isfinite(v)for v in vector),'nonfinite embedding'
   norm=math.sqrt(sum(v*v for v in vector));assert abs(norm-1)<.01,'not normalized'
   assert e['observation_id']==oid and e.get('evidence_id') in evidence,'embedding parent/evidence link'
   if e.get('presence_id'):assert e['presence_id']==o.get('presence_id'),'embedding presence mismatch'
   if e.get('source_frame_id'):assert e['source_frame_id']==o.get('source_frame_id'),'embedding frame mismatch'
   dimensions[profile]=len(vector);norms[profile].append(norm);counts['embeddings']+=1
 except Exception as exc:errors.append({'path':str(p),'error':str(exc)})
def size_stats(v):
 v=sorted(v);return {'samples':len(v),'min':v[0],'p50':v[round((len(v)-1)*.5)],'p95':v[round((len(v)-1)*.95)],'max':v[-1],'mean':sum(v)/len(v)}
print(json.dumps({'scope':'Saved first-per-kind gallery subset plus enlarged marker gallery, not every accepted package; receiver validates all accepted schemas/bytes. Non-scene size samples are not an unbiased whole-run distribution.','counts':dict(counts),'dimensions':dimensions,'normalization_ranges':{k:[min(v),max(v)]for k,v in norms.items()},'sampled_JPEG_bytes':{k:size_stats(v)for k,v in sizes.items()},'errors':errors,'passed':not errors and counts['sampled_packages']>0},indent=2))
