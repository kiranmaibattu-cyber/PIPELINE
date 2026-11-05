"""Export the suite QAFT checkpoint with frozen learned Q/DQ and dynamic batch."""
import os
os.environ.setdefault("OMP_NUM_THREADS","8")
import copy,json,sys,time,argparse,collections,traceback
from pathlib import Path
from common import *
import torch
import quantization as quant
from engine import load_checkpoint
import onnx
import openvino as ov

def load_selected(checkpoint):
    state=torch.load(inside(checkpoint),map_location="cpu",weights_only=False)
    if not state.get("quantization"):raise ValueError("Expected a quantization-aware checkpoint")
    model,state=load_checkpoint(checkpoint,state["config"])
    model=model.eval().float().requires_grad_(False)
    for m in model.modules():m._forward_hooks.clear()
    quant.mode(model,True,False)
    return model,state

def export(checkpoint,out):
    out=inside(out);out.mkdir(parents=True,exist_ok=True)
    write_json(out/"status.json",{"stage":"loading","time":now(),"checkpoint":str(checkpoint)})
    before=digest(checkpoint)
    model,state=load_selected(checkpoint)
    original_sha=digest(state["config"]["original_checkpoint"])
    head=model.model[-1]
    head.export=True;head.format="onnx";head.dynamic=False;head.max_det=state["config"]["max_det"]
    assert head.end2end
    x=torch.zeros(1,3,640,640,device="cuda")
    with torch.no_grad():sample=model(x)
    assert tuple(sample.shape)==(1,300,6),tuple(sample.shape)
    # Freeze observer statistics. The trained scales and zero-points remain constants.
    write_json(out/"frozen_quantizers.json",quant.observer_state(model))
    model=model.cpu();x=x.cpu()
    torch.set_num_threads(8)
    path=out/"model_qat.onnx"
    write_json(out/"status.json",{"stage":"onnx_export","time":now()})
    print("EXPORT ONNX",flush=True)
    torch.onnx.export(model,x,str(path),input_names=["images"],output_names=["detections"],
        opset_version=17,dynamo=False,do_constant_folding=False,
        dynamic_axes={"images":{0:"batch"},"detections":{0:"batch"}})
    graph=onnx.load(str(path));onnx.checker.check_model(graph)
    counts=dict(collections.Counter(n.op_type for n in graph.graph.node))
    if not counts.get("QuantizeLinear") or not counts.get("DequantizeLinear"):
        raise RuntimeError("Export discarded quantization")
    dims=graph.graph.input[0].type.tensor_type.shape.dim
    assert dims[0].dim_param=="batch" and [d.dim_value for d in dims[1:]]==[3,640,640]
    print("ONNX",counts,flush=True)
    write_json(out/"status.json",{"stage":"openvino_convert","time":now()})
    ir=ov.convert_model(str(path))
    ir.reshape({ir.input(0):ov.PartialShape([-1,3,640,640])})
    ir.set_rt_info({i:n for i,n in enumerate(CLASSES)},["model_info","names"])
    ir.set_rt_info("detect",["model_info","task"])
    ir.set_rt_info(640,["model_info","imgsz"])
    ir.set_rt_info(True,["model_info","end2end"])
    ov.save_model(ir,str(out/"model.xml"),compress_to_fp16=False)
    ircounts=dict(collections.Counter(n.get_type_name() for n in ir.get_ops()))
    if not ircounts.get("FakeQuantize"):raise RuntimeError("OpenVINO has no retained quantizers")
    print("IR",ircounts,flush=True)
    core=ov.Core()
    compiled=core.compile_model(ir,"CPU",{"INFERENCE_PRECISION_HINT":"f32","NUM_STREAMS":"1","INFERENCE_NUM_THREADS":8,"PERF_COUNT":"YES"})
    import numpy as np
    pred=np.array(compiled([np.zeros((1,3,640,640),np.float32)])[compiled.output(0)],copy=True)
    runtime=[]
    for node in compiled.get_runtime_model().get_ops():
        info=node.get_rt_info()
        fields={k:str(info[k].value) for k in ["layerType","runtimePrecision","execType","originalLayersNames"] if k in info}
        if fields:runtime.append({"name":node.get_friendly_name(),**fields})
    write_json(out/"runtime_layers.json",runtime)
    ints=[n for n in runtime if n.get("layerType") in ["Convolution","GroupConvolution"] and any(v in n.get("runtimePrecision","").lower() for v in ["u8","i8"])]
    if not ints:raise RuntimeError("CPU compiled without INT8 convolutions")
    assert before==digest(checkpoint) and original_sha==digest(state["config"]["original_checkpoint"])
    report={"time":now(),"checkpoint":str(checkpoint),"checkpoint_sha256":before,"epoch":state["epoch"]+1,
      "original_sha256":original_sha,"dataset":state["config"]["dataset"],"dataset_fingerprint":state["dataset_fingerprint"],
      "class_names":CLASSES,"input":"images: float32 NCHW RGB normalized 0..1, letterbox 640x640",
      "input_shape":["N",3,640,640],"output":"detections: [N,300,6], xyxy pixel coordinates on letterboxed input, confidence, class_id",
      "end2end":True,"quantization_source":"QAFT frozen trained scales; no PTQ or new calibration",
      "precision":"INT8/UINT8 convolutions with float outputs and other float operations",
      "onnx_node_counts":counts,"ir_node_counts":ircounts,"compiled_int8_convolutions":len(ints),
      "openvino_version":ov.__version__,"device":"CPU","device_name":core.get_property("CPU","FULL_DEVICE_NAME"),
      "files":{p.name:{"sha256":digest(p),"bytes":p.stat().st_size} for p in [path,out/"model.xml",out/"model.bin"]},
      "evaluation_provisional":True,"validation_pending":True}
    write_json(out/"export_manifest.json",report)
    write_json(out/"status.json",{"stage":"exported_pending_validation","time":now(),"compiled_int8_convolutions":len(ints)})
    print("EXPORTED",out,"INT8_CONVS",len(ints),flush=True)
    # Release runtime views before thread pools during Windows interpreter shutdown.
    del node,info,compiled,ir,core,head,model,x,sample,pred
    import gc
    gc.collect();torch.cuda.empty_cache()
    return report

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--checkpoint",required=True);p.add_argument("--out",required=True)
    a=p.parse_args()
    try:export(a.checkpoint,a.out)
    except Exception:
        write_json(Path(a.out)/"status.json",{"stage":"failed","time":now(),"error":traceback.format_exc()})
        raise
