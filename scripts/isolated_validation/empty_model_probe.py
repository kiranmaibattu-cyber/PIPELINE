"""Diagnostic on a second host, exact native primary/fire preprocessing, 3 frames."""
import json, sys
import gi
gi.require_version('Gst','1.0')
from gi.repository import Gst
from gstgva import VideoFrame
Gst.init([])
head='filesrc location=/fixtures/empty1080.mp4 ! qtdemux ! h264parse ! vah264dec ! vapostproc ! '
models={
 'primary':('video/x-raw(memory:VAMemory),format=NV12 ! ', '/models/traffic/openvino/vehicle_yolo26n_fp16_static_b1.xml','/opt/traffic-pilot/config/vehicle_yolo26_model_proc.json','va-surface-sharing',.3),
 'fire':('video/x-raw(memory:VAMemory),format=NV12 ! vapostproc add-borders=true ! video/x-raw,format=BGRx,width=320,height=320 ! videoconvert ! video/x-raw,format=BGR ! ','/models/traffic/openvino/smoke_fire.xml','/opt/traffic-pilot/config/fire_rgb_model_proc.json','opencv',.35)}
out={}
for name,(caps,model,proc,backend,threshold)in models.items():
 graph=head+caps+f'gvadetect model={model} model-proc={proc} device=GPU pre-process-backend={backend} threshold={threshold} nireq=1 ! appsink name=out sync=false max-buffers=2 drop=false'
 pipeline=Gst.parse_launch(graph);sink=pipeline.get_by_name('out');pipeline.set_state(Gst.State.PLAYING);rows=[]
 try:
  for i in range(3):
   sample=sink.emit('try-pull-sample',20*Gst.SECOND)
   if sample is None:
    msg=pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
    raise RuntimeError(str(msg.parse_error())if msg else'no native output')
   frame=VideoFrame(sample.get_buffer(),caps=sample.get_caps());rows.append([{'label':r.label(),'confidence':r.confidence()}for r in frame.regions()])
 finally:pipeline.set_state(Gst.State.NULL)
 out[name]=rows
print(json.dumps({'scope':'Frozen image on original/local host; native model output diagnosis only, not capacity/target-run instrumentation.','models':out},indent=2))
