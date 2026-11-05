import json,time,subprocess,socket,ssl,re,sys
import gi
gi.require_version('Gst','1.0')
from gi.repository import Gst,Gio
Gst.init([])
def clean(s):
 s=re.sub(r'(?i)(rtsps?://)[^\s/@]+:[^\s/@]+@',r'\1<redacted>@',str(s))
 return s[:2000]
print(json.dumps({'tls_backend':Gio.TlsBackend.get_default().__class__.__name__,'supports_tls':Gio.TlsBackend.get_default().supports_tls()}),flush=True)
for uri in sys.argv[1:]:
 case={'source':uri.rsplit('/',1)[-1]}
 for verify in [False,True]:
  cmd=['ffprobe','-v','error','-rw_timeout','6000000','-rtsp_transport','tcp']
  if verify:cmd+=['-tls_verify','1']
  cmd+=['-select_streams','v:0','-show_entries','stream=codec_name,width,height','-of','json',uri]
  start=time.monotonic()
  try:
   r=subprocess.run(cmd,capture_output=True,text=True,timeout=12);res={'returncode':r.returncode,'seconds':round(time.monotonic()-start,2),'stdout':r.stdout[:1000],'stderr':clean(r.stderr)}
  except subprocess.TimeoutExpired:res={'timeout_seconds':12}
  case['ffprobe_verified'if verify else'ffprobe_default']=res
 # Test compressed RTP reception without loading any model or decoding video.
 pipeline=Gst.parse_launch('rtspsrc name=src protocols=tcp latency=100 tcp-timeout=5000000 ! application/x-rtp,media=video ! fakesink name=sink sync=false signal-handoffs=true')
 pipeline.get_by_name('src').set_property('location',uri);received=[];cert=[]
 def on_buffer(sink,buffer,pad):
  if not received:received.append(time.monotonic())
 def on_cert(src,connection,certificate,errors):
  cert.append({'validation_errors':int(errors)});return False
 pipeline.get_by_name('sink').connect('handoff',on_buffer)
 pipeline.get_by_name('src').connect('accept-certificate',on_cert)
 bus=pipeline.get_bus();start=time.monotonic();pipeline.set_state(Gst.State.PLAYING);errors=[];warnings=[]
 try:
  while not received and time.monotonic()-start<12:
   msg=bus.timed_pop_filtered(200*Gst.MSECOND,Gst.MessageType.ERROR|Gst.MessageType.WARNING|Gst.MessageType.EOS)
   if msg and msg.type==Gst.MessageType.ERROR:
    e,d=msg.parse_error();errors.append({'message':clean(e.message),'debug':clean(d),'domain':str(e.domain),'code':e.code});break
   if msg and msg.type==Gst.MessageType.WARNING:
    e,d=msg.parse_warning();warnings.append({'message':clean(e.message),'debug':clean(d)})
 finally:pipeline.set_state(Gst.State.NULL)
 case['gstreamer_default_verified']={'received_video_rtp':bool(received),'seconds':round((received[0]if received else time.monotonic())-start,3),'certificate_rejections':cert,'errors':errors,'warnings':warnings}
 print(json.dumps(case),flush=True)
