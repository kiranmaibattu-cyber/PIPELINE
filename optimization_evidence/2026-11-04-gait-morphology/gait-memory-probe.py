import cv2,numpy as np,psutil,json,gc
cv2.setNumThreads(1);p=psutil.Process();rss=lambda:p.memory_info().rss
before=rss();frames=[np.zeros((h,w,3),np.uint8) for h,w in [(1440,2560)]*3+[(1080,1920)]*2]
for f in frames:f[:]=80
base=rss();models=[cv2.createBackgroundSubtractorMOG2(history=300,varThreshold=25,detectShadows=False) for _ in frames]
for bg,f in zip(models,frames):bg.apply(f)
first=rss()
for i in range(10):
 for bg,f in zip(models,frames):
  cv2.rectangle(f,(i*20,20),(i*20+30,400),(120,120,120),-1);bg.apply(f)
end=rss()
print(json.dumps({'before_bytes':before,'frames_allocated_bytes':base,'after_first_mog_bytes':first,'after_11_mog_updates_bytes':end,'increment_over_frames_bytes':end-base,'mixtures':[bg.getNMixtures() for bg in models],'scope':'isolated OpenCV five MOG instances; not full pipeline memory attribution'},indent=2))
