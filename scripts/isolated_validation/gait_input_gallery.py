"""Offline input sanity diagnostic using baked detector and actual silhouettes."""
from pathlib import Path
import sys,json,cv2,numpy as np
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from detectors.backends.openvino_detector import OpenVINOYOLODetector
from pipeline.person_reid import OpenVINOGaitExtractor
from types import SimpleNamespace
cv2.setNumThreads(1)
out=Path('/results');out.mkdir(exist_ok=True)
names=dict(enumerate(['bus','car','minivan','motorcycle','rickshaw','truck','bicycle','pedestrian','others']))
det=OpenVINOYOLODetector('/models/traffic/openvino/vehicle_yolo26n_fp16_static_b1.xml','vehicle',device='GPU',class_names=names)
g=OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor);g.backgrounds={}
cap=cv2.VideoCapture('/fixtures/crowd-1080p.mp4');rows=[];saved=0
for i in range(60):
 for _ in range(11):
  ok,f=cap.read()
  if not ok:cap.set(cv2.CAP_PROP_POS_FRAMES,0);ok,f=cap.read()
  assert ok
 people=[d for d in det.detect(f) if d.class_name=='pedestrian']
 for j,person in enumerate(people):
  mask=g._silhouettes(f,[person],'cam',frame_key=('diagnostic',i))[0]
  rows.append({'sample':i,'person':j,'bbox':list(map(float,person.bbox)),'valid_mask':mask is not None,'occupancy':float((mask>0).mean()) if mask is not None else None})
  if mask is not None and saved<30:
   x1,y1,x2,y2=map(int,person.bbox);crop=f[max(0,y1):min(f.shape[0],y2),max(0,x1):min(f.shape[1],x2)]
   if crop.size:
    left=cv2.resize(crop,(132,192));right=cv2.cvtColor(cv2.resize(mask,(132,192),interpolation=cv2.INTER_NEAREST),cv2.COLOR_GRAY2BGR)
    cv2.imwrite(str(out/f'gait-{saved:02d}.jpg'),np.hstack((left,right)));saved+=1
 g.release_frame('cam',('diagnostic',i))
(out/'gait-inputs.json').write_text(json.dumps({'note':'Offline CPU decode and baked GPU detector; silhouette sanity, not native tracking/recognition accuracy.','samples':rows},indent=2))
