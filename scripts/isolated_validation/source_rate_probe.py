"""Post-performance input-rate diagnostic: copy compressed packets, no decode."""
from pathlib import Path
import json,subprocess,threading,time,signal,datetime,sys
from urllib.parse import urlsplit,urlunsplit
from collections import deque
urls=(json.loads(sys.argv[2]) if len(sys.argv)>2 else
      ['rtsp://192.168.1.95:8554/traffic1','rtsps://gpu-box.burmilla-bass.ts.net:443/traffic1']);results=[]
for url in urls:
 purl=urlsplit(url);safe=urlunsplit((purl.scheme,(purl.hostname or'')+(':'+str(purl.port)if purl.port else''),purl.path,'',''));samples=[];start=time.monotonic();utc_start=datetime.datetime.now(datetime.timezone.utc).isoformat();errors=deque(maxlen=100)
 p=subprocess.Popen(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-rtsp_transport','tcp','-timeout','8000000','-i',url,'-map','0:v:0','-an','-c:v','copy','-f','null','-progress','pipe:1','-stats_period','1','-'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
 def reader():
  values={}
  for line in p.stdout:
   key,_,value=line.strip().partition('=');values[key]=value
   if key=='progress':samples.append({'monotonic':time.monotonic(),'values':values});values={}
 t=threading.Thread(target=reader,daemon=True);t.start()
 def error_reader():
  for line in p.stderr:errors.append(line)
 error_thread=threading.Thread(target=error_reader,daemon=True);error_thread.start()
 until=start+22
 while p.poll()is None and time.monotonic()<until:time.sleep(.2)
 if p.poll()is None:p.send_signal(signal.SIGINT)
 try:p.wait(5)
 except subprocess.TimeoutExpired:p.kill();p.wait()
 t.join(3);error_thread.join(3);error=''.join(errors).replace(url,'<redacted-url>');steady=[s for s in samples if s['monotonic']-start>=5 and s['values'].get('frame')]
 rate=None
 if len(steady)>1:rate=(int(steady[-1]['values']['frame'])-int(steady[0]['values']['frame']))/(steady[-1]['monotonic']-steady[0]['monotonic'])
 results.append({'source':safe,'started_utc':utc_start,'copy_packet_frame_rate':rate,'scope':'Separate later source-only run, not simultaneous with CV; input may vary over time. No hardware/software decoding.','samples':samples,'returncode':p.returncode,'stderr':error[-2000:]})
Path(sys.argv[1]).write_text(json.dumps(results,indent=2));print(json.dumps([(r['source'],r['copy_packet_frame_rate'])for r in results]))
