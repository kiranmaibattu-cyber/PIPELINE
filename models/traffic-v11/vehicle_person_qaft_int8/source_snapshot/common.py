import os,sys,json,hashlib,random,platform,subprocess,importlib.metadata
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(r"D:\TRAINING")
PROJECT=ROOT/"vehicle_person_finetune"
SUITE=PROJECT/"suite"
CLASSES=["bus","car","minivan","motorcycle","rickshaw","truck","bicycle","person","others"]
for key,folder in {"YOLO_CONFIG_DIR":"cache/ultralytics","HF_HOME":"cache/huggingface","TORCH_HOME":"cache/torch","TORCH_EXTENSIONS_DIR":"cache/torch_extensions","TEMP":"tmp","TMP":"tmp","MPLCONFIGDIR":"cache/matplotlib"}.items():
    p=ROOT/folder;p.mkdir(parents=True,exist_ok=True);os.environ[key]=str(p)
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG",":4096:8")
os.environ.setdefault("WANDB_MODE","disabled")
os.chdir(ROOT)
def inside(path):
    p=Path(path).resolve()
    if not p.is_relative_to(ROOT.resolve()):raise ValueError(f"Path must be under {ROOT}: {p}")
    return p
def now():return datetime.now(timezone.utc).isoformat()
def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1048576),b""):h.update(b)
    return h.hexdigest()
def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
def write_json(path,value):
    path=inside(path);path.parent.mkdir(parents=True,exist_ok=True)
    import uuid,time
    temp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    temp.write_text(json.dumps(value,indent=2,default=str,allow_nan=False),encoding="utf-8")
    try:
        for attempt in range(24):
            try:
                os.replace(temp,path);break
            except PermissionError:
                if attempt==23:raise
                time.sleep(min(.01*(2**attempt),.25))
    finally:
        if temp.exists():temp.unlink(missing_ok=True)
def read_json(path):return json.loads(inside(path).read_text(encoding="utf-8"))
def jsonl(path):
    with inside(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():yield json.loads(line)
def seed_all(seed):
    import numpy as np,torch
    random.seed(seed);np.random.seed(seed);torch.manual_seed(seed);torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark=False
    torch.use_deterministic_algorithms(True,warn_only=True)
def environment():
    import torch
    versions={}
    for pkg in ["torch","torchvision","ultralytics","nncf","numpy","matplotlib","tensorboard"]:
        try:versions[pkg]=importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:versions[pkg]=None
    return {"time":now(),"python":sys.version,"platform":platform.platform(),"packages":versions,"gpu":torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,"cuda":torch.version.cuda,"cudnn":torch.backends.cudnn.version(),"nvidia_smi":subprocess.run(["nvidia-smi"],capture_output=True,text=True).stdout,"source_hashes":{str(p.relative_to(SUITE)):digest(p) for p in SUITE.rglob("*.py")},"argv":sys.argv,"caches":{k:os.environ.get(k) for k in ["HF_HOME","TORCH_HOME","TORCH_EXTENSIONS_DIR","TEMP","YOLO_CONFIG_DIR"]}}
