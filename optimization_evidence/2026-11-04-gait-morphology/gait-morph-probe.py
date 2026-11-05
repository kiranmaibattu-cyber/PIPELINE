import sys,time,json
import numpy as np,cv2
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
# Candidate source mounted independently of installed runtime.
sys.path.insert(0,'/candidate')
from gait_morphology import clean_foreground,clean_person_regions
from pipeline.person_reid import OpenVINOGaitExtractor,opengait_pretreat
cv2.setNumThreads(1)
rng=np.random.default_rng(182);cases=0
for shape in [(31,47),(120,180),(720,1280),(1440,2560)]:
 for typ in range(5):
  h,w=shape
  if typ==0:mask=rng.integers(0,2,shape,dtype=np.uint8)*255
  elif typ==1:mask=np.zeros(shape,np.uint8)
  elif typ==2:mask=np.full(shape,255,np.uint8)
  else:
   mask=np.zeros(shape,np.uint8)
   for _ in range(50):
    x,y=int(rng.integers(w)),int(rng.integers(h));cv2.rectangle(mask,(x,y),(min(w-1,x+40),min(h-1,y+80)),255,-1)
  boxes=[[0,0,w,h],[0,0,17,25],[w-19,h-23,w,h],[-10,-5,w+5,h+5],[3,2,2,1],[-4,0,-1,h]]
  for _ in range(30):
   x,y=int(rng.integers(w)),int(rng.integers(h));boxes.append([x,y,min(w,x+int(rng.integers(1,100))),min(h,y+int(rng.integers(1,150)))])
  full=clean_foreground(mask)
  for box,got in zip(boxes,clean_person_regions(mask,boxes)):
   expected=OpenVINOGaitExtractor._crop(full,box)
   if expected.size==0:assert got is None
   else:
    assert np.array_equal(expected,got),str((shape,typ,box))
    a,b=opengait_pretreat(expected),opengait_pretreat(got)
    assert (a is None and b is None) or np.array_equal(a,b)
   cases+=1
cap=cv2.VideoCapture('/fixture.mp4');mog=cv2.createBackgroundSubtractorMOG2(history=300,varThreshold=25,detectShadows=False)
timings={'mog_ms':[],'full_morph_ms':[],'region_morph_ms':[]}
for i in range(30):
 ok,frame=cap.read();assert ok
 frame=cv2.resize(frame,(2560,1440))
 for repeat in range(3):
  t=time.perf_counter();mask=mog.apply(frame);timings['mog_ms'].append((time.perf_counter()-t)*1000)
  t=time.perf_counter();full=clean_foreground(mask);timings['full_morph_ms'].append((time.perf_counter()-t)*1000)
  box=[1050,300,1320,1100]
  t=time.perf_counter();region=clean_person_regions(mask,[box])[0];timings['region_morph_ms'].append((time.perf_counter()-t)*1000)
  assert np.array_equal(full[300:1100,1050:1320],region)
cap.release()
print(json.dumps({'mask_cases':cases,'mog_calls':90,'all_exact':True,'timing_median_ms':{k:float(np.median(v[6:])) for k,v in timings.items()},'fixture_note':'720p crowd recording resized to1440p for cost probe, not live timing'},indent=2))
