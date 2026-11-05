import sys,cv2,numpy as np,json
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline.person_reid import opengait_pretreat
from pipeline.gait_morphology import clean_person_regions
cv2.setNumThreads(1)
a=cv2.createBackgroundSubtractorMOG2(history=300,varThreshold=25,detectShadows=False)
b=cv2.createBackgroundSubtractorMOG2(history=300,varThreshold=25,detectShadows=False)
cap=cv2.VideoCapture('/fixture.mp4');different=0;total=0
for i in range(30):
 ok,f=cap.read();assert ok
 h,w=f.shape[:2];once=b.apply(f)
 for j in range(5):
  box=[j*w//5,0,(j+1)*w//5,h]
  old=opengait_pretreat(clean_person_regions(a.apply(f),[box])[0])
  new=opengait_pretreat(clean_person_regions(once,[box])[0])
  total+=1
  if not ((old is None and new is None) or np.array_equal(old,new)):different+=1
cap.release();print(json.dumps({'normalized_masks_compared':total,'different':different,'note':'constructed five regions; tests temporal update equivalence, not identity accuracy'},indent=2))
