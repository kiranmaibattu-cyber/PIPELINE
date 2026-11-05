import sys,cv2,numpy as np,json
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline.person_reid import OpenVINOGaitExtractor
from types import SimpleNamespace
cv2.setNumThreads(1)
old=OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor);old.backgrounds={}
new=OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor);new.backgrounds={}
cap=cv2.VideoCapture('/fixture.mp4');different=0;total=0;valid_iou=[]
for i in range(30):
 ok,frame=cap.read();assert ok
 h,w=frame.shape[:2]
 for j in range(5):
  person=SimpleNamespace(bbox=[j*w//5,0,(j+1)*w//5,h])
  a=old._silhouettes(frame,[person],'cam')[0]
  b=new._silhouettes(frame,[person],'cam',frame_key=('probe',i))[0]
  total+=1
  if not ((a is None and b is None) or (a is not None and b is not None and np.array_equal(a,b))):different+=1
  if a is not None and b is not None:
   u=np.logical_or(a>0,b>0).sum();valid_iou.append(float(np.logical_and(a>0,b>0).sum()/u) if u else 1.)
 new.release_frame('cam',('probe',i))
print(json.dumps({'normalized_masks':total,'changed_from_legacy':different,'background_calls_expected':{'legacy':150,'shared':30},'mean_normalized_mask_iou_when_both_valid':float(np.mean(valid_iou)) if valid_iou else None,'note':'Five constructed image regions, not labelled person masks; measures preprocessing drift, not recognition accuracy.'},indent=2))
