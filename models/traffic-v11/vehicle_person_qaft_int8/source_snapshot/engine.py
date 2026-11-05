import copy,csv,math,time,random,traceback,contextlib
from types import SimpleNamespace
import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader,WeightedRandomSampler
from torch.utils.tensorboard import SummaryWriter
from ultralytics import YOLO
from ultralytics.cfg import get_cfg
from ultralytics.data import build_yolo_dataset
from ultralytics.models.yolo.detect import DetectionValidator
from common import *
from dataset import verify_lock
import quantization as quant
import batching

def native_args(config):
    return get_cfg(overrides={"epochs":config["epochs"],"lr0":config["lr"],"weight_decay":config["weight_decay"],"nbs":config["batch"]*config["accumulate"],"freeze":config["freeze_layers"],"imgsz":config["imgsz"],"batch":config["batch"],"workers":config["workers"],"device":0,"task":"detect","mode":"train","seed":config["seed"],"mosaic":0.0,"mixup":0.0,"copy_paste":0.0,"hsv_h":0.015,"hsv_s":0.4,"hsv_v":0.3,"translate":0.05,"scale":0.2,"fliplr":0.5,"box":config["loss_gains"]["box"],"cls":config["loss_gains"]["cls"],"dfl":config["loss_gains"]["dfl"],"deterministic":True,"save":False})
def load_original(config):
    original=inside(config["original_checkpoint"])
    model=YOLO(str(original)).model.float()
    if [model.names[i] for i in range(9)]!=CLASSES:raise ValueError("Checkpoint class map differs")
    model.requires_grad_(True);model.args=native_args(config);model.criterion=None
    return model.cuda()
def load_checkpoint(path,config):
    state=torch.load(inside(path),map_location="cpu",weights_only=False)
    if state.get("suite_schema")!=1:raise ValueError("Expected suite checkpoint, not a generic .pt")
    if config.get("batch") in ["auto",-1]:
        config=copy.deepcopy(config);config["batch"]=state["config"]["batch"];config["accumulate"]=state["config"]["accumulate"]
    if digest(inside(config["original_checkpoint"]))!=state["original_sha256"]:raise ValueError("Original checkpoint hash mismatch")
    model=load_original(config)
    if state["quantization"] is not None:quant.prepare(model)
    model.load_state_dict(state["model"],strict=True);model.criterion=None
    return model,state
def make_loader(dest,split,config):
    import yaml
    data=yaml.safe_load((Path(dest)/"data.yaml").read_text())
    for s in ["train","val","test"]:data[s]=str(Path(dest)/data[s])
    from unittest.mock import patch
    # Disable incompatible optional third-party augmentation; native YOLO transforms remain active.
    with patch.dict(sys.modules,{"albumentations":None}):
        ds=build_yolo_dataset(native_args(config),data[split],config["batch"],data,mode="train" if split=="train" else "val",rect=False,stride=32)
    sampler=None;weights=None
    if split=="train" and config["balance_classes"]:
        freq=np.zeros(9)
        for label in ds.labels:
            for c in set(label["cls"].reshape(-1).astype(int).tolist()):freq[c]+=1
        weights=[min(config.get("max_class_sampling_weight",5.0),max([math.sqrt(max(freq.max(),1)/max(freq[c],1)) for c in set(label["cls"].reshape(-1).astype(int).tolist())] or [config.get("negative_sampling_weight",1.0)])) for label in ds.labels]
        sampler=WeightedRandomSampler(weights,len(weights),replacement=True)
    loader=DataLoader(ds,batch_size=config["batch"],shuffle=split=="train" and sampler is None,sampler=sampler,num_workers=config["workers"],collate_fn=ds.collate_fn,pin_memory=True)
    return loader,data,weights
def move_batch(batch,half=False):
    result={key:(value.cuda(non_blocking=True) if isinstance(value,torch.Tensor) else value) for key,value in batch.items()}
    result["img"]=result["img"].float()/255
    if half:result["img"]=result["img"].half()
    return result
class FeatureCache:
    def __init__(self,model,layers):
        self.values={};self.handles=[]
        for index in layers:
            self.handles.append(model.model[index].register_forward_hook(self.hook(str(index))))
    def hook(self,key):
        def capture(module,args,out):self.values[key]=out
        return capture
    def close(self):
        for h in self.handles:h.remove()
        self.values.clear()
def feature_loss(student,teacher):
    terms=[torch.nn.functional.mse_loss(torch.nn.functional.normalize(value.float(),dim=1),torch.nn.functional.normalize(teacher[key].float(),dim=1)) for key,value in student.items()]
    if not terms:raise RuntimeError("No distillation features captured")
    return torch.stack(terms).mean()
def evaluate(model,dest,split,config,out,precision="fp32",plots=True):
    out=inside(out);out.mkdir(parents=True,exist_ok=True)
    candidate=quant.floating_copy(model) if precision in ["fp32","fp16"] else copy.deepcopy(model)
    candidate.criterion=None
    for module in candidate.modules():module._forward_hooks.clear()
    if precision=="int8_sim":quant.mode(candidate,True,False)
    loader,data,_=make_loader(dest,split,config)
    validator=DetectionValidator(dataloader=loader,save_dir=out,args={"data":str(Path(dest)/"data.yaml"),"task":"detect","mode":"val","imgsz":config["imgsz"],"batch":config["batch"],"device":0,"workers":config["workers"],"half":precision=="fp16","plots":plots,"conf":config["eval_conf"],"iou":config["eval_iou"],"max_det":config["max_det"],"split":split,"save_json":False})
    shim=SimpleNamespace(device=torch.device("cuda:0"),data=data,amp=precision=="fp16",ema=SimpleNamespace(ema=None),model=candidate,loss_items=torch.zeros(3,device="cuda"),args=SimpleNamespace(compile=False),stopper=SimpleNamespace(possible_stop=True),epoch=0,epochs=1,world_size=1,label_loss_items=lambda value,prefix:{f"{prefix}/{name}":float(v) for name,v in zip(["box_loss","cls_loss","dfl_loss"],value)})
    metrics=validator(trainer=shim)
    box=validator.metrics.box;index={int(c):i for i,c in enumerate(box.ap_class_index)}
    counts=collections_counts(loader.dataset)
    perclass={}
    for c,name in enumerate(CLASSES):
        i=index.get(c);supported=bool(counts[c]>0)
        perclass[name]={"objects":int(counts[c]),"supported":supported,"ap50":float(box.ap50[i]) if i is not None else (0.0 if supported else None),"ap50_95":float(box.ap[i]) if i is not None else (0.0 if supported else None),"precision":float(box.p[i]) if i is not None else (0.0 if supported else None),"recall":float(box.r[i]) if i is not None else (0.0 if supported else None)}
    report={"time":now(),"split":split,"precision":precision,"metrics":{k:float(v) for k,v in metrics.items()},"per_class":perclass,"speed_ms":validator.speed,"evaluation_provisional":read_json(Path(dest)/"lock.json")["policy"].get("allow_auto_evaluation_labels",False),"note":"int8_sim measures fake-quantized float execution, not native INT8 device speed."}
    write_json(out/"metrics.json",report)
    del candidate,validator,loader;torch.cuda.empty_cache()
    return report
def collections_counts(ds):
    counts=np.zeros(9,dtype=int)
    for row in ds.labels:
        for c in row["cls"].reshape(-1).astype(int):counts[c]+=1
    return counts
def retention(reference,candidate,tolerance):
    unsupported=[];regressions={};deltas={}
    for name in CLASSES:
        before=reference["per_class"][name];after=candidate["per_class"][name]
        if not before["supported"] or not after["supported"]:unsupported.append(name);continue
        delta=after["ap50_95"]-before["ap50_95"];deltas[name]=delta
        if delta < -tolerance:regressions[name]=delta
    return {"eligible":not unsupported and not regressions,"unsupported_classes":unsupported,"regressions":regressions,"ap50_95_deltas":deltas,"max_allowed_absolute_drop":tolerance,"note":"Eligibility is an observed validation result, not a guarantee on unseen data."}
def snapshot(model,optimizer,scaler,epoch,step,config,lock,profile,run):
    return {"suite_schema":1,"model":{k:v.detach().cpu().clone() for k,v in model.state_dict().items()},"optimizer":optimizer.state_dict(),"scaler":scaler.state_dict(),"epoch":epoch,"step":step,"config":config,"config_hash":fingerprint(config),"dataset_fingerprint":lock["fingerprint"],"original_sha256":digest(config["original_checkpoint"]),"quantization":profile,"run":str(run),"rng":{"python":random.getstate(),"numpy":np.random.get_state(),"torch":torch.get_rng_state(),"cuda":torch.cuda.get_rng_state_all()},"time":now()}
def save_checkpoint(path,state):
    temp=Path(path).with_suffix(".tmp");torch.save(state,temp);temp.replace(path)
def chart(run):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows=list(jsonl(run/"epochs.jsonl"))
    if not rows:return
    fig,axes=plt.subplots(2,2,figsize=(14,9))
    for name in sorted({key for row in rows for key in row["train"] if "loss" in key}):
        axes[0,0].plot([r["epoch"] for r in rows],[r["train"].get(name,float("nan")) for r in rows],label=name)
    for precision in ["fp32","fp16","int8_sim"]:
        values=[(r["epoch"],r["validation"][precision]["metrics"].get("metrics/mAP50-95(B)",0)) for r in rows if precision in r["validation"]]
        if values:axes[0,1].plot(*zip(*values),label=precision)
    for name in CLASSES:
        axes[1,0].plot([r["epoch"] for r in rows],[r["validation"]["fp32"]["per_class"][name]["ap50_95"] if r["validation"]["fp32"]["per_class"][name]["supported"] else float("nan") for r in rows],label=name)
    axes[1,1].plot([r["epoch"] for r in rows],[r["lr"] for r in rows],label="learning rate")
    for ax,title in zip(axes.flat,["Training losses (per image)","Validation mAP50-95 by precision","FP32 per-class AP50-95","Learning rate"]):
        ax.set_title(title);ax.set_xlabel("Epoch");ax.grid(alpha=.2);ax.legend(fontsize=7)
    fig.tight_layout();fig.savefig(run/"curves.png",dpi=150);plt.close(fig)

def make_memory_probe(config):
    """Profile disposable models and a repeated real training sample; restore RNG after each trial."""
    import gc
    provisional=copy.deepcopy(config);provisional["batch"]=1;provisional["accumulate"]=1;provisional["balance_classes"]=False
    loader,_,_=make_loader(config["dataset"],"train",provisional)
    sample=next(iter(loader));del loader
    free,total=torch.cuda.mem_get_info()
    budget=int(free*config.get("auto_batch_memory_fraction",.75))
    def trial(size):
        rng={"python":random.getstate(),"numpy":np.random.get_state(),"torch":torch.get_rng_state(),"cuda":torch.cuda.get_rng_state_all()}
        student=teacher=optimizer=student_features=teacher_features=batch=prediction=native=components=loss=None
        start_allocated=torch.cuda.memory_allocated()
        result={"fits":False,"available_gpu_bytes":free,"additional_memory_budget_bytes":budget}
        try:
            if config["stage"]=="qaft":
                if not config.get("initial_checkpoint"):raise ValueError("QAFT auto batch requires a floating-stage checkpoint")
                student,state=load_checkpoint(config["initial_checkpoint"],provisional)
                if state["quantization"] is not None:raise ValueError("QAFT initial checkpoint must be floating stage")
                del state
                quant.prepare(student)
            else:student=load_original(provisional)
            teacher=load_original(provisional).eval().requires_grad_(False)
            student.train()
            for index,layer in enumerate(student.model):
                if index<config["freeze_layers"]:
                    layer.requires_grad_(False)
                    for m in layer.modules():
                        if isinstance(m,nn.BatchNorm2d):m.eval()
            teacher_features=FeatureCache(teacher,config["distill_layers"]);student_features=FeatureCache(student,config["distill_layers"])
            batch={"img":sample["img"].repeat(size,1,1,1),"cls":sample["cls"].repeat(size,1),"bboxes":sample["bboxes"].repeat(size,1),"batch_idx":torch.cat([sample["batch_idx"]+i for i in range(size)])}
            batch=move_batch(batch)
            optimizer=torch.optim.AdamW([p for p in student.parameters() if p.requires_grad],lr=0.0,weight_decay=0.0)
            torch.cuda.reset_peak_memory_stats()
            with torch.no_grad():teacher(batch["img"])
            for precision,weight in config["precision_weights"].items():
                if weight<=0:continue
                quant.mode(student,precision=="int8_sim",precision=="int8_sim")
                with torch.autocast("cuda",dtype=torch.float16,enabled=precision=="fp16"):
                    prediction=student(batch["img"]);native,components=student.loss(batch,prediction)
                    loss=(native.sum()/size+config["distill_weight"]*feature_loss(student_features.values,teacher_features.values))*weight
                loss.backward()
            # Allocate Adam's moment buffers. Only disposable preflight copies are touched (LR zero).
            optimizer.step();torch.cuda.synchronize()
            peak=torch.cuda.max_memory_allocated()
            # Reserve room for validation's extra student copy, activations, and non-PyTorch allocations.
            model_bytes=sum(p.numel()*p.element_size() for p in student.parameters())
            required=max(0,peak-start_allocated)+2*model_bytes+64*1024*1024
            result.update(fits=required<=budget,peak_allocated_bytes=peak,estimated_additional_bytes=required)
        except torch.cuda.OutOfMemoryError:
            result.update(fits=False,error="cuda_out_of_memory")
        finally:
            if teacher_features:teacher_features.close()
            if student_features:student_features.close()
            student=teacher=optimizer=student_features=teacher_features=batch=prediction=native=components=loss=None
            gc.collect();torch.cuda.empty_cache()
            random.setstate(rng["python"]);np.random.set_state(rng["numpy"]);torch.set_rng_state(rng["torch"]);torch.cuda.set_rng_state_all(rng["cuda"])
        print(json.dumps({"batch_preflight":{"batch":size,**result}}),flush=True)
        return result
    return trial

def train(config_path,resume=None,overrides=None):
    requested=read_json(config_path)
    if overrides:requested=batching.apply_overrides(requested,**overrides)
    config=copy.deepcopy(requested);dest=inside(config["dataset"]);lock=verify_lock(dest)
    if config["stage"] not in ["float","qaft"]:raise ValueError("stage must be float or qaft")
    if not torch.cuda.is_available():raise RuntimeError("CUDA required")
    if config["epochs"]<1:raise ValueError("Invalid epochs")
    if config["workers"]!=0:raise ValueError("This reproducible Windows suite currently requires workers=0")
    if config["require_all_classes_in_validation"]:
        missing=[name for name,n in lock["report"]["objects"]["val"].items() if n==0]
        if missing:raise ValueError(f"Reviewed validation has no examples for: {missing}. Add representative data before claiming retention.")
    if config.get("diagnostic_only",False) != any(r.get("source")=="synthetic_diagnostic" for r in lock["records"]):raise ValueError("Synthetic diagnostic data may only be used in diagnostic runs")
    seed_all(config["seed"]);profile=None;state=None
    if resume:
        resume_state=torch.load(inside(resume),map_location="cpu",weights_only=False)
        config=batching.resolve_resume(requested,resume_state);del resume_state
    else:
        config=batching.resolve(requested,make_memory_probe(requested) if requested.get("batch") in ["auto",-1] else None)
    print(json.dumps({"batch_resolution":config.get("batch_resolution",{"batch":config["batch"],"accumulate":config["accumulate"]})}),flush=True)
    if resume:
        model,state=load_checkpoint(resume,config)
        if state["config_hash"]!=fingerprint(config) or state["dataset_fingerprint"]!=lock["fingerprint"]:raise ValueError("Resume config/dataset differs")
        profile=state["quantization"];run=inside(state["run"])
        if inside(resume)!=(run/"last.pt").resolve():raise ValueError("Resume from the run last.pt; earlier checkpoints require a new experiment")
        if (run/"epochs.jsonl").exists():
            completed=[row for row in jsonl(run/"epochs.jsonl") if row["epoch"]<=state["epoch"]+1]
            (run/"epochs.jsonl").write_text("".join(json.dumps(row)+"\n" for row in completed),encoding="utf-8")
    else:
        run=PROJECT/("diagnostics" if config.get("diagnostic_only") else "experiments")/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")+"_"+config["stage"]);run.mkdir(parents=True)
        if config["stage"]=="qaft":
            if not config.get("initial_checkpoint"):raise ValueError("QAFT requires a completed floating-stage suite checkpoint")
            model,initial=load_checkpoint(config["initial_checkpoint"],config)
            if initial["quantization"] is not None:raise ValueError("QAFT initial checkpoint must be floating-stage")
            if initial["dataset_fingerprint"]!=lock["fingerprint"]:raise ValueError("QAFT dataset differs from floating stage")
            profile=quant.prepare(model)
        else:model=load_original(config)
    teacher=load_original(config).eval()
    for p in teacher.parameters():p.requires_grad_(False)
    frozen=[]
    for index,layer in enumerate(model.model):
        if index<config["freeze_layers"]:
            frozen.append(layer)
            for p in layer.parameters():p.requires_grad_(False)
    optimizer=torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],lr=config["lr"],weight_decay=config["weight_decay"],betas=tuple(config["betas"]))
    scaler=torch.amp.GradScaler("cuda",enabled=config["precision_weights"].get("fp16",0)>0,init_scale=config["amp_initial_scale"])
    loader,data,weights=make_loader(dest,"train",config)
    if not len(loader):raise ValueError("Empty training loader")
    start=0;step=0
    if state:
        optimizer.load_state_dict(state["optimizer"]);scaler.load_state_dict(state["scaler"]);start=state["epoch"]+1;step=state["step"]
        rng=state["rng"];random.setstate(rng["python"]);np.random.set_state(rng["numpy"]);torch.set_rng_state(rng["torch"]);torch.cuda.set_rng_state_all(rng["cuda"])
    else:
        write_json(run/"config.json",config);write_json(run/"requested_config.json",requested);write_json(run/"batch_resolution.json",config.get("batch_resolution",{}));write_json(run/"environment.json",environment());write_json(run/"dataset_lock.json",lock)
        write_json(run/"sampler.json",{"balanced":weights is not None,"weights":weights,"seed":config["seed"],"transforms":str(loader.dataset.transforms),"optional_albumentations":"disabled; use native YOLO augmentation"})
        write_json(run/"quantization.json",profile)
        write_json(run/"resolved_native_args.json",vars(native_args(config)))
        write_json(run/"architecture.json",model.yaml)
        write_json(run/"optimizer.json",{"class":"AdamW","defaults":optimizer.defaults,"trainable_parameters":sum(p.numel() for p in model.parameters() if p.requires_grad),"total_parameters":sum(p.numel() for p in model.parameters()),"scheduler":"linear step warmup multiplied by cosine decay","reproducibility":"Seeds, dataset snapshot, sampler weights and full RNG state saved; strict bitwise determinism is not guaranteed across CUDA/runtime versions."})
    if not state and config["stage"]=="float":save_checkpoint(run/"initial.pt",snapshot(model,optimizer,scaler,-1,0,config,lock,profile,run))
    reference_path=run/"reference"/"fp32"/"metrics.json"
    if reference_path.exists():reference=read_json(reference_path)
    else:reference=evaluate(teacher,dest,"val",config,run/"reference"/"fp32","fp32",True)
    writer=SummaryWriter(str(run/"tensorboard"))
    writer.add_text("config",json.dumps(config,indent=2));writer.add_text("quantization",json.dumps(profile,indent=2))
    teacher_features=FeatureCache(teacher,config["distill_layers"]);student_features=FeatureCache(model,config["distill_layers"])
    best=read_json(run/"best_scores.json") if (run/"best_scores.json").exists() else {"candidate":-1.0,"eligible":-1.0}
    from training_controls import early_stopping,fit_signals,eligible_checkpoint_epoch
    history=list(jsonl(run/"epochs.jsonl")) if (run/"epochs.jsonl").exists() else []
    early_stop=None
    total_steps=config["epochs"]*math.ceil(len(loader)/config["accumulate"])
    write_json(run/"status.json",{"status":"running","stage":config["stage"],"started":now()})
    try:
        for epoch in range(start,config["epochs"]):
            model.train()
            for layer in frozen:
                for m in layer.modules():
                    if isinstance(m,nn.BatchNorm2d):m.eval()
            averages={};seen=0;updates=0;skipped=0;optimizer.zero_grad(set_to_none=True);pending=0;step_images=0;tick=time.monotonic()
            torch.cuda.reset_peak_memory_stats()
            for batch_index,batch in enumerate(loader):
                batch=move_batch(batch);bs=batch["img"].shape[0];seen+=bs;pending+=1;step_images+=bs
                with torch.no_grad():teacher(batch["img"])
                recorded={}
                for precision,weight in config["precision_weights"].items():
                    if weight<=0:continue
                    if precision=="int8_sim" and profile is None:raise ValueError("INT8 simulation requires QAFT stage")
                    quant.mode(model,precision=="int8_sim" and epoch>=config["quant_start_epoch"],precision=="int8_sim" and epoch<config["observer_freeze_epoch"])
                    with torch.autocast("cuda",dtype=torch.float16,enabled=precision=="fp16"):
                        prediction=model(batch["img"]);native,components=model.loss(batch,prediction)
                        supervised=native.sum()/bs
                        kd=feature_loss(student_features.values,teacher_features.values)
                        loss=supervised+config["distill_weight"]*kd
                    if not torch.isfinite(loss):raise FloatingPointError(f"Nonfinite {precision} loss")
                    scaler.scale(loss*weight/config["accumulate"]).backward()
                    recorded[f"{precision}/supervised_loss"]=float(supervised.detach())
                    recorded[f"{precision}/distill_loss"]=float(kd.detach())
                    recorded[f"{precision}/weighted_total_loss"]=float(loss.detach())*weight
                    for name,v in zip(["box_loss","cls_loss","dfl_loss"],components):recorded[f"{precision}/{name}"]=float(v.detach())
                recorded["total_loss"]=sum(v for k,v in recorded.items() if k.endswith("/weighted_total_loss"))
                for name,value in recorded.items():averages[name]=averages.get(name,0)+value*bs
                if pending==config["accumulate"] or batch_index==len(loader)-1:
                    progress=step/max(total_steps-1,1);warmup_steps=config["warmup_steps"]
                    lr=config["lr"]*min(1.0,(step+1)/max(warmup_steps,1))*(config["min_lr_fraction"]+(1-config["min_lr_fraction"])*(1+math.cos(math.pi*progress))/2)
                    for group in optimizer.param_groups:group["lr"]=lr
                    scaler.unscale_(optimizer)
                    if pending<config["accumulate"]:
                        for p in model.parameters():
                            if p.grad is not None:p.grad.mul_(config["accumulate"]/pending)
                    norm=torch.nn.utils.clip_grad_norm_(model.parameters(),config["grad_clip"],error_if_nonfinite=not scaler.is_enabled())
                    finite=bool(torch.isfinite(norm));scale_before=scaler.get_scale()
                    scaler.step(optimizer);scaler.update();optimizer.zero_grad(set_to_none=True);step+=1
                    if finite:updates+=1
                    else:skipped+=1
                    with (run/"optimizer_steps.jsonl").open("a",encoding="utf-8") as log:log.write(json.dumps({"step":step,"epoch":epoch+1,"updated":finite,"grad_norm":float(norm) if finite else None,"scale_before":scale_before,"scale_after":scaler.get_scale(),"effective_batch":step_images})+"\n")
                    writer.add_scalar("optimizer/grad_norm",float(norm),step);writer.add_scalar("optimizer/lr",lr,step);writer.add_scalar("optimizer/effective_batch",step_images,step);writer.add_scalar("optimizer/loss_scale",scaler.get_scale(),step)
                    for name,value in recorded.items():writer.add_scalar("batch/"+name,value,step)
                    pending=0;step_images=0
            if updates==0:raise FloatingPointError("No finite optimizer updates this epoch; lower amp_initial_scale or inspect gradients")
            teacher_features.values.clear();student_features.values.clear()
            validation={}
            for precision in (["fp32","fp16","int8_sim"] if profile is not None else ["fp32","fp16"]):
                validation[precision]=evaluate(model,dest,"val",config,run/"validation"/f"epoch_{epoch+1:04d}"/precision,precision,plots=True)
                for name,value in validation[precision]["metrics"].items():writer.add_scalar(f"validation/{precision}/{name}",value,epoch+1)
                for name,metrics in validation[precision]["per_class"].items():
                    for metric,value in metrics.items():
                        if metric not in ["supported","objects"] and value is not None:writer.add_scalar(f"class/{precision}/{name}/{metric}",value,epoch+1)
            gates={precision:retention(reference,report,config["max_class_ap_drop"]) for precision,report in validation.items()}
            row={"epoch":epoch+1,"time":now(),"train":{k:v/seen for k,v in averages.items()},"lr":lr,"step":step,"train_images_sampled":seen,"optimizer_updates":updates,"overflow_skipped_steps":skipped,"epoch_seconds":time.monotonic()-tick,"peak_gpu_memory_bytes":torch.cuda.max_memory_allocated(),"validation":validation,"retention":gates,"quant_active":profile is not None and epoch>=config["quant_start_epoch"],"observers_active":profile is not None and epoch<config["observer_freeze_epoch"]}
            row["selection_score"]=min(report["metrics"].get("metrics/mAP50-95(B)",0) for report in validation.values())
            history.append(row)
            selection_history=[r for r in history if eligible_checkpoint_epoch(profile is not None,r["quant_active"])]
            row["early_stopping"]=early_stopping(selection_history,config);row["fit_signals"]=fit_signals(selection_history,config)
            write_json(run/"fit_monitor.json",{"epoch":epoch+1,"early_stopping":row["early_stopping"],"fit_signals":row["fit_signals"],"evaluation_provisional":lock["policy"].get("allow_auto_evaluation_labels",False)})
            with (run/"epochs.jsonl").open("a",encoding="utf-8") as f:f.write(json.dumps(row,allow_nan=False)+"\n")
            for name,value in row["train"].items():writer.add_scalar("epoch/"+name,value,epoch+1)
            writer.add_scalar("resources/peak_gpu_memory_bytes",row["peak_gpu_memory_bytes"],epoch+1)
            if profile:write_json(run/"observer_state.json",quant.observer_state(model))
            checkpoint=snapshot(model,optimizer,scaler,epoch,step,config,lock,profile,run)
            save_checkpoint(run/"last.pt",checkpoint)
            score=min(report["metrics"].get("metrics/mAP50-95(B)",0) for report in validation.values())
            if eligible_checkpoint_epoch(profile is not None,row["quant_active"]) and score>best["candidate"]:save_checkpoint(run/"candidate_best.pt",checkpoint);best["candidate"]=score
            if eligible_checkpoint_epoch(profile is not None,row["quant_active"]) and all(g["eligible"] for g in gates.values()) and score>best["eligible"]:
                save_checkpoint(run/"retention_best.pt",checkpoint);best["eligible"]=score
            if (epoch+1)%config["save_period"]==0:save_checkpoint(run/f"epoch_{epoch+1:04d}.pt",checkpoint)
            write_json(run/"best_scores.json",best)
            write_json(run/"status.json",{"status":"running","epoch":epoch+1,"epochs":config["epochs"],"best":best,"retention":gates})
            chart(run);writer.flush()
            print(json.dumps({"run":str(run),"epoch":epoch+1,"losses":row["train"],"retention":gates}),flush=True)
            if row["early_stopping"]["stop"]:
                early_stop={"epoch":epoch+1,**row["early_stopping"]};break
        write_json(run/"status.json",{"status":"complete","finished":now(),"best":best,"early_stop":early_stop,"completed_epochs":epoch+1 if start<config["epochs"] else start,"note":"No format conversion performed. INT8 results refer to simulation."})
    except BaseException as error:
        write_json(run/"status.json",{"status":"failed","error":str(error),"traceback":traceback.format_exc(),"time":now()})
        raise
    finally:teacher_features.close();student_features.close();writer.close()
    return run
