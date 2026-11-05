"""Validate saved OpenVINO QAFT IR against its frozen PyTorch reference."""
import os
os.environ.setdefault("OMP_NUM_THREADS","8")
import argparse,copy,json,time,collections
from pathlib import Path
from types import SimpleNamespace
from common import *
import numpy as np
import torch
import openvino as ov
from engine import make_loader,collections_counts
from export_openvino_qat import load_selected
from ultralytics.models.yolo.detect import DetectionValidator
from ultralytics.utils.metrics import box_iou

CPU_CONFIG={"INFERENCE_PRECISION_HINT":"f32","NUM_STREAMS":"1","INFERENCE_NUM_THREADS":8,"PERF_COUNT":"YES"}

def validator(data,config,out,names):
    v=DetectionValidator(save_dir=out,args={"data":str(Path(config["dataset"])/"data.yaml"),
        "task":"detect","mode":"val","imgsz":640,"batch":config["batch"],"workers":0,
        "plots":True,"conf":config["eval_conf"],"iou":config["eval_iou"],
        "max_det":config["max_det"],"split":"test","save_json":False})
    v.device=torch.device("cpu");v.training=False;v.data=data
    v.iouv=torch.linspace(.5,.95,10);v.niou=10
    v.init_metrics(SimpleNamespace(names=names,end2end=True))
    return v

def finish(v,counts,out):
    m=v.get_stats();box=v.metrics.box;idx={int(c):i for i,c in enumerate(box.ap_class_index)}
    rows={}
    for c,name in enumerate(CLASSES):
        i=idx.get(c)
        rows[name]={"objects":int(counts[c]),"supported":bool(counts[c]),**{
           field:(float(vals[i]) if i is not None else (0.0 if counts[c] else None))
           for field,vals in [("ap50",box.ap50),("ap50_95",box.ap),("precision",box.p),("recall",box.r)]}}
    report={"images":v.seen,"metrics":{k:float(x) for k,x in m.items()},"per_class":rows}
    write_json(out/"metrics.json",report)
    return report

def validate_split(compiled,model,state,dataset,out,tag):
    config=copy.deepcopy(state["config"]);config["batch"]=16
    config["dataset"]=str(dataset);config["balance_classes"]=False
    loader,data,_=make_loader(dataset,"test",config)
    counts=collections_counts(loader.dataset)
    vs={k:validator(data,config,out/k,model.names) for k in ["openvino_int8","pytorch_int8_sim"]}
    shapes=[];timings=[];raw_predictions=[]
    for j,batch in enumerate(loader):
        arr=batch["img"].numpy().astype(np.float32)/255.0
        t=time.perf_counter();pred=np.array(compiled([arr])[compiled.output(0)],copy=True)
        timings.append({"batch":int(arr.shape[0]),"milliseconds":(time.perf_counter()-t)*1000})
        with torch.inference_mode():
            ref=model(torch.from_numpy(arr).cuda())
            if isinstance(ref,tuple):ref=ref[0]
            ref=ref.float().cpu()
        shapes.append(list(pred.shape))
        for name,y in [("openvino_int8",torch.from_numpy(pred)),("pytorch_int8_sim",ref)]:
            vs[name].batch_i=j
            pp=vs[name].postprocess(y)
            vs[name].update_metrics(pp,batch)
        if j==0:
            np.savez_compressed(out/"first_batch_predictions.npz",openvino=pred,pytorch=ref.numpy())
        raw_predictions.extend([{"image":str(path),"detections":[p.tolist() for p in rows if p[4]>=.25]} for path,rows in zip(batch["im_file"],pred)])
        print(tag,"TEST",min((j+1)*16,len(loader.dataset)),"/",len(loader.dataset),flush=True)
        write_json(out.parent/"status.json",{"stage":"evaluating_"+tag,"images":min((j+1)*16,len(loader.dataset)),"total":len(loader.dataset),"time":now()})
    reports={name:finish(v,counts,out/name) for name,v in vs.items()}
    gaps={name:reports["openvino_int8"]["per_class"][name]["ap50_95"]-reports["pytorch_int8_sim"]["per_class"][name]["ap50_95"] for name in CLASSES if counts[CLASSES.index(name)]}
    write_json(out/"predictions_conf025.json",raw_predictions)
    result={"dataset":str(dataset),"reports":reports,"ap50_95_deltas_openvino_minus_sim":gaps,"inference_timings":timings,"output_shapes":shapes}
    write_json(out/"comparison.json",result)
    return result

def match_stats(a,b,conf=.25):
    a=torch.from_numpy(np.array(a,copy=True));b=torch.from_numpy(np.array(b,copy=True))
    a=a[a[:,4]>=conf];b=b[b[:,4]>=conf]
    used=set();pairs=[]
    for row in a:
        candidates=[j for j in range(len(b)) if j not in used and int(b[j,5])==int(row[5])]
        if not candidates:continue
        ious=box_iou(row[None,:4],b[candidates,:4])[0]
        best=int(ious.argmax());iou=float(ious[best]);j=candidates[best]
        if iou>=.5:used.add(j);pairs.append((iou,abs(float(row[4]-b[j,4]))))
    return {"detections":len(a),"reference_detections":len(b),"matched_same_class_iou05":len(pairs),
        "min_matched_iou":min([p[0] for p in pairs],default=None),
        "max_matched_confidence_difference":max([p[1] for p in pairs],default=None)}

def run(out):
    out=inside(out)
    manifest=read_json(out/"export_manifest.json")
    model,state=load_selected(manifest["checkpoint"])
    model.model[-1].max_det=300
    # Evaluate the original trained inference behavior, not an alternate detection head.
    model.model[-1].export=False
    core=ov.Core();compiled=core.compile_model(str(out/"model.xml"),"CPU",CPU_CONFIG)
    torch.set_num_threads(8)
    assert digest(manifest["checkpoint"])==manifest["checkpoint_sha256"]
    config=copy.deepcopy(state["config"]);config["batch"]=16
    loader,_,_=make_loader(config["dataset"],"test",config)
    batch=next(iter(loader));arr=batch["img"].numpy().astype(np.float32)/255
    del loader
    individual=[np.array(compiled([arr[i:i+1]])[compiled.output(0)][0],copy=True) for i in range(16)]
    batch_reports=[]
    for n in [1,2,3,4,8,16]:
        x=np.ascontiguousarray(arr[:n])
        for _ in range(2):compiled([x])
        times=[]
        for _ in range(5):
            t=time.perf_counter()
            y=np.array(compiled([x])[compiled.output(0)],copy=True)
            times.append((time.perf_counter()-t)*1000)
        if tuple(y.shape)!=(n,300,6) or not np.isfinite(y).all():raise RuntimeError("Invalid batch output")
        matches=[match_stats(y[i],individual[i]) for i in range(n)]
        stable=all(m["matched_same_class_iou05"]==m["reference_detections"]==m["detections"] and (m["min_matched_iou"] is None or m["min_matched_iou"]>.99) and (m["max_matched_confidence_difference"] is None or m["max_matched_confidence_difference"]<.01) for m in matches)
        batch_reports.append({"batch":n,"shape":list(y.shape),"median_inference_ms":float(np.median(times)),
            "images_per_second":float(n*1000/np.median(times)),"consistent_with_single_image":stable,"image_comparisons":matches})
        print("BATCH",n,batch_reports[-1]["median_inference_ms"],"stable",stable,flush=True)
    write_json(out/"batch_validation.json",batch_reports)
    mixed=validate_split(compiled,model,state,Path(config["dataset"]),out/"mixed_test","mixed")
    campaign=Path(read_json(PROJECT/"latest_trained_model.json")["campaign"])
    human=validate_split(compiled,model,state,campaign/"human_test_subset",out/"human_test","human")
    del model
    mapkey="metrics/mAP50-95(B)"
    drop=mixed["reports"]["pytorch_int8_sim"]["metrics"][mapkey]-mixed["reports"]["openvino_int8"]["metrics"][mapkey]
    classdrops={k:-v for k,v in mixed["ap50_95_deltas_openvino_minus_sim"].items() if v<-.01}
    passed=all(x["consistent_with_single_image"] for x in batch_reports) and drop<=.01 and not classdrops
    result={"time":now(),"passed":passed,"conversion_acceptance_max_absolute_ap_drop":.01,
        "mixed_test":mixed,"human_test":human,"batch_validation":batch_reports,
        "overall_ap_drop":drop,"class_drops_over_tolerance":classdrops,
        "evaluation_provisional":True,"note":"Conversion fidelity only. Most mixed-test labels are original-teacher predictions; human test has 14 images and 9 persons. No full-class independent accuracy or retention certification."}
    write_json(out/"validation.json",result)
    manifest["validation_pending"]=False;manifest["conversion_validation_passed"]=passed
    write_json(out/"export_manifest.json",manifest)
    write_json(out/"status.json",{"stage":"complete" if passed else "validation_failed","time":now(),"overall_ap_drop":drop})
    assert digest(manifest["checkpoint"])==manifest["checkpoint_sha256"]
    print("VALIDATION",passed,"AP_DROP",drop,flush=True)
    if not passed:raise RuntimeError("Conversion failed acceptance criteria")
    return result

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--out",required=True);a=p.parse_args();run(Path(a.out))
