"""Local-only marked1080p30 source matching the principal decode workload."""
from pathlib import Path
import cv2,subprocess,shutil
cv2.setNumThreads(1)
out=Path('/tmp/sentry-validation-extra-fixtures/latency-1080p30.mp4')
cap=cv2.VideoCapture('/home/admin1/Downloads/Usecase-Catalog-main/videos/crowd.mp4')
p=subprocess.Popen(['ffmpeg','-v','error','-f','rawvideo','-pixel_format','bgr24','-video_size','1280x720','-framerate','30','-i','pipe:0','-vf','scale=1920:1080','-an','-c:v','libx264','-pix_fmt','yuv420p','-preset','veryfast','-crf','20','-threads','2','-g','30','-bf','0','-y',str(out)],stdin=subprocess.PIPE)
try:
 for i in range(3600):
  if shutil.disk_usage(out.parent).free<180*1024**2:raise RuntimeError('local fixture disk guard')
  ok,f=cap.read()
  if not ok:cap.set(cv2.CAP_PROP_POS_FRAMES,0);ok,f=cap.read()
  assert ok;f=cv2.resize(f,(1280,720));bits=f'{0xA55A:016b}{i:032b}{sum(i.to_bytes(4,"big"))&255:08b}'
  f[694:718,4:688]=127
  for b,v in enumerate(bits):f[696:716,8+12*b:8+12*(b+1)]=255 if v=='1' else 0
  p.stdin.write(f.tobytes())
finally:p.stdin.close();p.wait();cap.release()
assert p.returncode==0
print(out)
