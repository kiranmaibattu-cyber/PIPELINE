import gi,time,json
gi.require_version('Gst','1.0')
from gi.repository import Gst
Gst.init([])
names=['ch9','ch10','ch16','traffic1','traffic2'];counts={n:0 for n in names}; graphs=[]; errors=[]
for n in names:
 codec='h264' if n=='traffic1' else 'h265'
 uri='rtsps://gpu-box.burmilla-bass.ts.net:443/'+n
 graph=Gst.parse_launch(f'rtspsrc location={json.dumps(uri)} protocols=tcp latency=100 ! rtp{codec}depay ! {codec}parse ! va{codec}dec name=decoder ! fakesink sync=false async=false')
 def observe(pad,info,name=n):
  if info.get_buffer() is not None:counts[name]+=1
  return Gst.PadProbeReturn.OK
 graph.get_by_name('decoder').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER,observe)
 graphs.append((n,graph));graph.set_state(Gst.State.PLAYING)
def check():
 for n,g in graphs:
  msg=g.get_bus().pop_filtered(Gst.MessageType.ERROR)
  if msg:
   err,debug=msg.parse_error();errors.append({'camera':n,'error':str(err),'debug':debug})
try:
 deadline=time.monotonic()+35
 while not all(counts.values()) and time.monotonic()<deadline:
  check();time.sleep(.1)
 # Preserve partial source failures as evidence rather than discard all rates.
 time.sleep(3)
 start=time.monotonic();before=counts.copy()
 while time.monotonic()-start<20:check();time.sleep(.1)
 elapsed=time.monotonic()-start
 report={'mode':'five simultaneous hardware decoders; no analytics','seconds':elapsed,'decoded_fps':{n:(counts[n]-before[n])/elapsed for n in names},'errors':errors}
 print(json.dumps(report,indent=2))
finally:
 for _,g in graphs:g.set_state(Gst.State.NULL)
