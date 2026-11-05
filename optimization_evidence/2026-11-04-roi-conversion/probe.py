#!/usr/bin/env python3
"""Compare VA region conversion to full BGR crops from identical retained frames.
Qualification only: does not alter production sampling or evidence policy.
"""
import argparse
from pathlib import Path
from collections import Counter
import json
import sys
import time
import numpy as np
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo
sys.path.insert(0, '/opt/traffic-pilot/services/worker')
from pipeline.surface_stream import VAMemorySelectiveBgrConverter, _map_bgr_any, paired_pts


class RoiConverter:
    def __init__(self, caps, explicit_color=False):
        self.w = caps.get_structure(0).get_value('width')
        self.h = caps.get_structure(0).get_value('height')
        self.pipeline = Gst.parse_launch(
            'appsrc name=source format=time block=true max-buffers=1 max-bytes=0 ! '
            'videocrop name=region ! vapostproc disable-passthrough=true ! '
            'video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! '
            'appsink name=result sync=false max-buffers=1 drop=false '
            'wait-on-eos=false enable-last-sample=false')
        self.source = self.pipeline.get_by_name('source')
        if explicit_color:
            info=GstVideo.VideoInfo.new_from_caps(caps)
            caps=caps.copy()
            caps.set_value('colorimetry',info.colorimetry.to_string())
        self.source.set_property('caps', caps)
        self.region = self.pipeline.get_by_name('region')
        self.sink = self.pipeline.get_by_name('result')
        self.va_inputs = 0
        self.last_caps = None
        for element in self.pipeline.iterate_elements():
            if element.get_factory().get_name() == 'vapostproc':
                def inspect(pad, info):
                    cap = pad.get_current_caps()
                    if cap and cap.get_features(0).contains('memory:VAMemory'):
                        self.va_inputs += 1
                    return Gst.PadProbeReturn.OK
                element.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, inspect)
        self.pipeline.set_state(Gst.State.PLAYING)

    def convert(self, sample, bounds):
        x1,y1,x2,y2=bounds
        # Expand to even NV12 coordinates with a halo before restoring exact
        # requested pixels. No scaling and no modification of the source sample.
        l=max(0, (x1-4)//2*2); t=max(0, (y1-4)//2*2)
        r=min(self.w, (x2+5)//2*2); b=min(self.h, (y2+5)//2*2)
        started=time.perf_counter()
        self.region.set_properties(left=l, top=t, right=self.w-r, bottom=self.h-b)
        if self.source.emit('push-sample', sample) != Gst.FlowReturn.OK:
            raise RuntimeError('ROI submission failed')
        result=self.sink.emit('try-pull-sample', 10*Gst.SECOND)
        if result is None:
            msg=self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            raise RuntimeError(str(msg.parse_error()) if msg else 'ROI conversion timeout')
        paired_pts(sample,result)
        self.last_caps = result.get_caps().to_string()
        array=_map_bgr_any(result)
        if array.shape[:2] != (b-t,r-l):
            raise RuntimeError(f'crop geometry mismatch {array.shape} vs {(b-t,r-l)}')
        return array[y1-t:y2-t,x1-l:x2-l].copy(),time.perf_counter()-started,(r-l)*(b-t)

    def close(self):
        self.pipeline.set_state(Gst.State.NULL)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('video');ap.add_argument('--frames',type=int,default=20);ap.add_argument('--output',required=True);ap.add_argument('--fixed-converters',action='store_true');ap.add_argument('--explicit-color',action='store_true');args=ap.parse_args()
    Gst.init([])
    graph=Gst.parse_launch(f'filesrc location={json.dumps(args.video)} ! qtdemux ! h264parse ! vah264dec ! vapostproc ! video/x-raw(memory:VAMemory),format=NV12 ! appsink name=frames sync=false max-buffers=2 drop=false enable-last-sample=false wait-on-eos=false')
    sink=graph.get_by_name('frames'); full=roi=None; rows=[]; fixed={}; controls=[]; caps_record={}
    graph.set_state(Gst.State.PLAYING)
    try:
        for index in range(args.frames):
            sample=sink.emit('try-pull-sample',10*Gst.SECOND)
            if sample is None:raise RuntimeError('source decode failed')
            caps=sample.get_caps()
            if not caps.get_features(0).contains('memory:VAMemory'):raise RuntimeError('source not VA memory')
            if full is None:
                full=VAMemorySelectiveBgrConverter(caps,Counter());roi=RoiConverter(caps,args.explicit_color)
            start=time.perf_counter(); full_sample=full.convert(sample); reference=_map_bgr_any(full_sample);full_ms=(time.perf_counter()-start)*1000
            repeat=_map_bgr_any(full.convert(sample)); control=np.abs(repeat.astype(np.int16)-reference.astype(np.int16))
            controls.append(dict(frame=index,max_delta=int(control.max()),mean_delta=float(control.mean())))
            caps_record={'source':caps.to_string(),'full':full_sample.get_caps().to_string()}
            h,w=reference.shape[:2]
            boxes=[(0,0,64,64),(w-63,h-65,w,h),(w//3+1,h//4+1,w//2+2,h*3//4+1),(0,0,w,h),(100,100,116,116)]
            for box in boxes:
                converter=roi
                if args.fixed_converters:
                    if box not in fixed:fixed[box]=RoiConverter(caps,args.explicit_color)
                    converter=fixed[box]
                result,seconds,pixels=converter.convert(sample,box);x1,y1,x2,y2=box
                delta=np.abs(result.astype(np.int16)-reference[y1:y2,x1:x2].astype(np.int16))
                rows.append(dict(frame=index,bbox=box,pts=int(sample.get_buffer().pts),max_delta=int(delta.max()),mean_delta=float(delta.mean()),equal=bool(not delta.any()),roi_ms=seconds*1000,full_ms=full_ms,converted_pixels=pixels,source_pixels=w*h,output_caps=converter.last_caps))
                if index == 0:
                    import cv2
                    dest=Path(args.output).parent
                    name="_".join(map(str,box))
                    cv2.imwrite(str(dest/(name+"-reference.png")),reference[y1:y2,x1:x2])
                    cv2.imwrite(str(dest/(name+"-roi.png")),result)
    finally:
        for converter in fixed.values():converter.close()
        if roi:roi.close()
        if full:full.close()
        graph.set_state(Gst.State.NULL)
    report={'rows':rows,'controls':controls,'caps':caps_record,'fixed_converters':args.fixed_converters,'explicit_color':args.explicit_color,'va_crop_inputs':sum(c.va_inputs for c in fixed.values()) if fixed else roi.va_inputs,'all_exact':all(r['equal'] for r in rows),'max_delta':max(r['max_delta'] for r in rows)}
    open(args.output,'w').write(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('rows','controls','caps')}))
if __name__=='__main__':main()
