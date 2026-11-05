"""Independent TCP MPEG-TS replay for each consumer; supports reconnect/reload tests."""
import argparse,signal,socket,subprocess,threading,time,os,json,datetime
p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);p.add_argument('--fixture',required=True);p.add_argument('--corrupt',action='store_true');p.add_argument('--throttle-kib',type=float,default=0);p.add_argument('--log');a=p.parse_args()
stop=threading.Event();children=set();lock=threading.Lock()
def finish(*_):
 stop.set()
 with lock:
  for child in children:
   try:child.terminate()
   except ProcessLookupError:pass
signal.signal(signal.SIGTERM,finish)
def serve(connection):
 child=None
 try:
  if a.corrupt:
   while not stop.wait(.1):connection.sendall(b'INVALID-MPEG-TS-QUALIFICATION'*188)
   return
  child=subprocess.Popen(['ffmpeg','-v','error','-re','-stream_loop','-1','-i',a.fixture,'-map','0:v:0','-an','-c:v','copy','-f','mpegts','pipe:1'],stdout=subprocess.PIPE if a.throttle_kib else connection,stderr=subprocess.DEVNULL)
  with lock:children.add(child)
  if a.throttle_kib:
   epoch=time.monotonic();sent_bytes=0;last_log=-2
   while not stop.is_set():
    chunk=os.read(child.stdout.fileno(),65536)
    if not chunk:break
    if 30<=time.monotonic()-epoch<50:stop.wait(len(chunk)/(a.throttle_kib*1024))
    connection.sendall(chunk)
    sent_bytes+=len(chunk);elapsed=time.monotonic()-epoch
    if a.log and elapsed-last_log>=2:
     with open(a.log,'a')as log:log.write(json.dumps({'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'monotonic':time.monotonic(),'connection_elapsed_seconds':elapsed,'bytes_sent':sent_bytes,'cap_KiB_s':a.throttle_kib if 30<=elapsed<50 else None})+'\n')
     last_log=elapsed
  while child.poll()is None and not stop.wait(.2):pass
 finally:
  connection.close()
  if child:
   if child.poll()is None:child.terminate()
   try:child.wait(5)
   except subprocess.TimeoutExpired:child.kill();child.wait()
   with lock:children.discard(child)
with socket.socket()as server:
 server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);server.bind(('127.0.0.1',a.port));server.listen(50);server.settimeout(1)
 while not stop.is_set():
  try:connection,address=server.accept()
  except socket.timeout:continue
  threading.Thread(target=serve,args=(connection,),daemon=True).start()
finish()
