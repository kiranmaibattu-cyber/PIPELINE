"""Direct source-side TCP fanout diagnostic; no inference or image assumptions."""
import concurrent.futures,json,socket,sys,time
from urllib.parse import urlsplit
address=urlsplit(sys.argv[1]);duration=10
def consume(index):
 start=time.monotonic();total=0;error=None
 try:
  with socket.create_connection((address.hostname,address.port),timeout=5)as s:
   s.settimeout(1)
   while time.monotonic()-start<duration:
    try:data=s.recv(65536)
    except socket.timeout:continue
    if not data:break
    total+=len(data)
 except Exception as exc:error=type(exc).__name__+': '+str(exc)
 return {'consumer':index,'bytes':total,'elapsed_seconds':time.monotonic()-start,'error':error}
with concurrent.futures.ThreadPoolExecutor(max_workers=5)as pool:many=list(pool.map(consume,range(5)))
single=consume(0)
print(json.dumps({'scope':'Raw TCP bytes only, no decode/FPS guarantee','host':address.hostname,'port':address.port,'five_simultaneous':many,'single_followup':single},indent=2))
