import cv2,numpy as np,json
cap=cv2.VideoCapture('/fixture.mp4');frames=[]
for _ in range(20):
 ok,f=cap.read()
 if not ok:break
 frames.append(f)
cap.release()
def run(threads):
 cv2.setNumThreads(threads)
 bg=cv2.createBackgroundSubtractorMOG2(history=500,varThreshold=16,detectShadows=True)
 out=[]
 for f in frames:
  gray=cv2.cvtColor(f,cv2.COLOR_BGR2GRAY)
  out.extend([gray, cv2.resize(f,(128,256),interpolation=cv2.INTER_LINEAR),cv2.resize(f,(224,224),interpolation=cv2.INTER_CUBIC), cv2.Laplacian(gray,cv2.CV_64F),bg.apply(f),cv2.imencode('.jpg',f,[int(cv2.IMWRITE_JPEG_QUALITY),88])[1]])
 return out
old,new=run(16),run(1)
print(json.dumps({'frames':len(frames),'arrays_compared':len(old),'all_exact':all(np.array_equal(a,b) for a,b in zip(old,new)),'max_delta':max(float(np.abs(a.astype(float)-b.astype(float)).max()) for a,b in zip(old,new))},indent=2))
