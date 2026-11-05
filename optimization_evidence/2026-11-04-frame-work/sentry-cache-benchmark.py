import sys,time,statistics,json,hashlib
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline.frame_work_cache import FrameWorkCache
import cv2
cap=cv2.VideoCapture('/fixture.mp4');ok,image=cap.read();cap.release();assert ok
result={}
for duplicates in (1,2,4):
 old=[];new=[]
 for trial in range(40):
  cache=FrameWorkCache()
  t=time.perf_counter()
  for i in range(duplicates):
   body=cv2.imencode('.jpg',image,[int(cv2.IMWRITE_JPEG_QUALITY),88])[1].tobytes();hashlib.sha256(body).hexdigest()
  old.append((time.perf_counter()-t)*1000)
  t=time.perf_counter()
  for i in range(duplicates):cache.jpeg(str(trial),image)
  new.append((time.perf_counter()-t)*1000)
 result[duplicates]={'original_median_ms':statistics.median(old[5:]),'cached_median_ms':statistics.median(new[5:])}
print(json.dumps(result,indent=2))
