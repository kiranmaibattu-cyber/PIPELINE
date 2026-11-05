#!/usr/bin/env python3
"""One-client TCP sender with an independent source epoch for frame-marker tests."""
import argparse,json,socket,subprocess,time,os,signal
from fractions import Fraction
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);p.add_argument('--fixture',required=True);p.add_argument('--logdir',required=True);a=p.parse_args()
info=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=avg_frame_rate,nb_frames','-of','json',a.fixture]))['streams'][0]
source_fps=float(Fraction(info['avg_frame_rate']));source_frames=int(info.get('nb_frames') or 1200)
def stop(*unused):raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
root=Path(a.logdir);root.mkdir(parents=True,exist_ok=True)
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',a.port));s.listen(1)
c,address=s.accept();epoch={'port':a.port,'accepted_mono':time.monotonic(),'accepted_utc':time.time(),'fixture':a.fixture,'source_fps':source_fps,'source_frames':source_frames};f=None
transport=bytearray();first_pts=None;peslog=(root/(str(a.port)+'-pes.jsonl')).open('w',buffering=1)
try:
 f=subprocess.Popen(['ffmpeg','-v','error','-re','-stream_loop','-1','-i',a.fixture,'-map','0:v:0','-an','-c:v','copy','-f','mpegts','pipe:1'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
 epoch['producer_started_mono']=time.monotonic();(root/(str(a.port)+'.json')).write_text(json.dumps(epoch))
 while True:
  chunk=os.read(f.stdout.fileno(),4096)
  if not chunk:break
  if 'first_bytes_mono' not in epoch:
   epoch['first_bytes_mono']=time.monotonic();epoch['first_bytes_utc']=time.time();(root/(str(a.port)+'.json')).write_text(json.dumps(epoch))
  c.sendall(chunk)
  sent=time.monotonic();transport.extend(chunk)
  while len(transport)>=188:
   packet=bytes(transport[:188]);del transport[:188]
   if packet[0]!=0x47:raise RuntimeError('Replay is not aligned MPEG-TS')
   if not packet[1]&0x40:continue
   control=(packet[3]>>4)&3
   if control not in (1,3):continue
   offset=4+(1+packet[4] if control==3 else 0);payload=packet[offset:]
   if len(payload)<14 or payload[:3]!=b'\x00\x00\x01' or not 0xe0<=payload[3]<=0xef or not payload[7]&0x80:continue
   b=payload[9:14];pts=(((b[0]>>1)&7)<<30)|(b[1]<<22)|((b[2]>>1)<<15)|(b[3]<<7)|(b[4]>>1)
   if first_pts is None:first_pts=pts
   index=round((pts-first_pts)*source_fps/90000)
   peslog.write(json.dumps({'source_index':index,'marker_index':index%source_frames,'pts90k':pts,'transmitted_mono':sent,'port':a.port})+'\n')
except (BrokenPipeError,ConnectionResetError):pass
finally:
 peslog.close()
 c.close();s.close()
 if f:
  f.terminate()
  try:f.wait(timeout=3)
  except subprocess.TimeoutExpired:f.kill();f.wait()
