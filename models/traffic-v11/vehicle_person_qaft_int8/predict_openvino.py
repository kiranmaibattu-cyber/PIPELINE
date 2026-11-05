"""OpenVINO INT8 batched image inference. No PyTorch dependency."""
import argparse,json,time,os
from pathlib import Path
import cv2
import numpy as np
import openvino as ov

CLASSES=["bus","car","minivan","motorcycle","rickshaw","truck","bicycle","person","others"]
def letterbox(image):
    h,w=image.shape[:2]
    ratio=min(640/h,640/w,1.0)
    nw,nh=round(w*ratio),round(h*ratio)
    dw,dh=(640-nw)/2,(640-nh)/2
    left,top=round(dw-.1),round(dh-.1)
    if (nw,nh)!=(w,h):image=cv2.resize(image,(nw,nh),interpolation=cv2.INTER_LINEAR)
    image=cv2.copyMakeBorder(image,top,640-nh-top,left,640-nw-left,cv2.BORDER_CONSTANT,value=(114,114,114))
    x=np.ascontiguousarray(image[:,:,::-1].transpose(2,0,1),dtype=np.float32)/255
    return x,ratio,left,top,(h,w)

def source_images(source):
    source=Path(source)
    if source.is_dir():
        return sorted(p for p in source.rglob("*") if p.suffix.lower() in [".jpg",".jpeg",".png",".bmp",".webp"])
    if source.suffix.lower()==".txt":
        return [Path(p.strip()) for p in source.read_text(encoding="utf-8").splitlines() if p.strip()]
    return [source]

def infer(model_path,paths,batch_size,confidence,threads=8,device="CPU"):
    if batch_size<1:raise ValueError("Batch must be a positive integer")
    if not 0<=confidence<=1:raise ValueError("Confidence must be between 0 and 1")
    core=ov.Core()
    config={"INFERENCE_PRECISION_HINT":"f32","NUM_STREAMS":"1","INFERENCE_NUM_THREADS":threads} if device=="CPU" else {}
    model=core.compile_model(str(model_path),device,config)
    output=model.output(0);results=[];total_ms=0.0;batches=[]
    for start in range(0,len(paths),batch_size):
        current=paths[start:start+batch_size];xs=[];transforms=[]
        for path in current:
            image=cv2.imread(str(path))
            if image is None:raise ValueError(f"Cannot read image: {path}")
            x,*transform=letterbox(image);xs.append(x);transforms.append(transform)
        arr=np.stack(xs)
        t=time.perf_counter()
        ys=np.array(model([arr])[output],copy=True)
        elapsed=(time.perf_counter()-t)*1000;total_ms+=elapsed;batches.append(len(current))
        if ys.shape!=(len(current),300,6) or not np.isfinite(ys).all():raise RuntimeError("Invalid model outputs")
        for path,rows,transform in zip(current,ys,transforms):
            ratio,left,top,(h,w)=transform
            detections=[]
            for x1,y1,x2,y2,score,cls in rows:
                if score<confidence:continue
                c=int(cls)
                if c<0 or c>=len(CLASSES):raise RuntimeError("Invalid model class")
                box=[float(np.clip((x1-left)/ratio,0,w)),float(np.clip((y1-top)/ratio,0,h)),
                     float(np.clip((x2-left)/ratio,0,w)),float(np.clip((y2-top)/ratio,0,h))]
                if box[2]<=box[0] or box[3]<=box[1]:continue
                detections.append({"class_id":c,"class_name":CLASSES[c],"confidence":float(score),"xyxy":box})
            results.append({"image":str(path),"width":w,"height":h,"detections":detections})
    del output,model,core
    return {"model":str(model_path),"device":device,"requested_batch":batch_size,"actual_batches":batches,
        "confidence_threshold":confidence,"images":len(results),"inference_ms":total_ms,
        "timing_note":"Inference calls only, including first-call setup; excludes reading, preprocessing, compilation, and output serialization.",
        "results":results}

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--model",default=str(Path(__file__).with_name("model.xml")))
    p.add_argument("--source",required=True,help="Image, directory, or text file with one image path per line")
    p.add_argument("--batch",type=int,default=1)
    p.add_argument("--conf",type=float,default=.25)
    p.add_argument("--threads",type=int,default=8)
    p.add_argument("--device",default="CPU")
    p.add_argument("--output",required=True)
    a=p.parse_args()
    dest=Path(a.output).resolve()
    # All generated task outputs stay under the user's chosen workspace on this machine.
    root=Path(r"D:\TRAINING").resolve()
    if os.name=="nt" and not dest.is_relative_to(root):raise ValueError("Output must be under D:\\TRAINING")
    paths=source_images(a.source)
    if not paths:raise ValueError("No input images found")
    report=infer(Path(a.model),paths,a.batch,a.conf,a.threads,a.device)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({"images":report["images"],"requested_batch":a.batch,"actual_batches":report["actual_batches"],"output":str(dest)}))
