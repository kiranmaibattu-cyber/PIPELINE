import cv2,numpy as np,time,json,hashlib
from concurrent.futures import ThreadPoolExecutor
cap=cv2.VideoCapture('/fixture.mp4');frames=[]
for _ in range(20):
 ok,f=cap.read();assert ok;frames.append(cv2.resize(f,(2560,1440)))
cap.release()
def worker():
 bg=cv2.createBackgroundSubtractorMOG2(history=300,varThreshold=25,detectShadows=False)
 h=hashlib.sha256();timings=[]
 for f in frames:
  for _ in range(3):
   t=time.perf_counter();m=bg.apply(f);timings.append((time.perf_counter()-t)*1000);h.update(memoryview(m))
 return {'hash':h.hexdigest(),'median_ms':float(np.median(timings[6:]))}
results={}
for threads in (1,2,4):
 cv2.setNumThreads(threads);t=time.perf_counter()
 with ThreadPoolExecutor(max_workers=5) as pool:rs=list(pool.map(lambda _:worker(),range(5)))
 results[threads]={'wall_seconds':time.perf_counter()-t,'workers':rs}
assert len({r['hash'] for v in results.values() for r in v['workers']})==1
print(json.dumps({'all_masks_exact':True,'five_concurrent_background_models':results},indent=2))
