import cv2,numpy as np,time,json
cv2.setNumThreads(1)
cap=cv2.VideoCapture('/fixture.mp4')
models=[cv2.createBackgroundSubtractorMOG2(history=300,varThreshold=25,detectShadows=False) for _ in range(2)]
buffer=np.empty((1440,2560),np.uint8)
times=[[],[]];calls=0;reused=0
for i in range(40):
 ok,frame=cap.read();assert ok
 frame=cv2.resize(frame,(2560,1440))
 for repeat in range(3):
  results={}
  for mode in ([0,1] if calls%2==0 else [1,0]):
   t=time.perf_counter()
   results[mode]=models[mode].apply(frame) if mode==0 else models[mode].apply(frame,fgmask=buffer)
   times[mode].append((time.perf_counter()-t)*1000)
  assert np.array_equal(results[0],results[1])
  reused+=int(np.shares_memory(results[1],buffer));calls+=1
 np.testing.assert_array_equal(models[0].getBackgroundImage(),models[1].getBackgroundImage())
print(json.dumps({'calls':calls,'masks_and_background_exact':True,'buffer_reused_calls':reused,'median_ms':[float(np.median(t[12:])) for t in times],'p95_ms':[float(np.percentile(t[12:],95)) for t in times],'note':'Alternating paired CPU test, one OpenCV thread, crowd fixture resized to1440p; not live capacity'},indent=2))
