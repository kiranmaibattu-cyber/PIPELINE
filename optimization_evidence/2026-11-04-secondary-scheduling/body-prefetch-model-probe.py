import sys,os,time,json,numpy as np,cv2
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline.dls_secondary import DlsBodyExtractor
cv2.setNumThreads(1)
cap=cv2.VideoCapture('/fixture.mp4');ok,frame=cap.read();cap.release();assert ok
crops=[frame[60:650,100+i*150:230+i*150].copy() for i in range(6)]
x=DlsBodyExtractor('/models/surveillance/transreid_ssl_int8.xml','/opt/traffic-pilot/config/body_reid_model_proc.json',device='NPU',owner_id='probe')
rows=[]
try:
 x.warmup()
 for iteration in range(8):
  pair={};vectors={}
  for flag in ([0,1] if iteration%2==0 else [1,0]):
   os.environ['SENTINEL_DLS_BODY_PREFETCH']=str(flag)
   t=time.perf_counter();vectors[flag]=x.embed(crops);pair[str(flag)]=(time.perf_counter()-t)*1000
  rows.append({'iteration':iteration,'batch_ms':pair,'max_vector_delta':float(np.abs(vectors[0]-vectors[1]).max())})
finally:x.close()
print(json.dumps({'rows':rows,'all_vectors_exact':all(r['max_vector_delta']==0 for r in rows),'sync_median_ms':float(np.median([r['batch_ms']['0'] for r in rows[2:]])),'prefetch_median_ms':float(np.median([r['batch_ms']['1'] for r in rows[2:]]))},indent=2))
