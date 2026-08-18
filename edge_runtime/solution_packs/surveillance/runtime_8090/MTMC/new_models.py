"""Loaders for the 6 new Stage-1 embedders not in reid_benchmark's registry.

  osnet_ibn      — torchreid osnet_ibn_x1_0 (auto-downloads ImageNet weights)
  agw / mgn      — fastreid (reuses repos/fastreid_sbs_bot_agw clone; weights
                   downloaded from the fastreid v0.1.1 GitHub release)
  transreid_ssl  — TransReID-SSL ViT-S (manual GDrive download expected)
  solider        — SOLIDER-REID Swin-S (manual GDrive download expected)
  lightmbn       — via boxmot's auto-downloading ReID backbone factory

Each loader returns (embedder, backend_detail) or (None, reason). Embedders
expose embed(list[bgr_crop]) -> (N, D) L2-normalized float32, same contract
as reid_benchmark.ReIDModel.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_MODELS = _ROOT / "models"

FASTREID_RELEASE = "https://github.com/JDAI-CV/fast-reid/releases/download/v0.1.1"
NEW_MODEL_WEIGHTS = {
    "agw": {
        "url": f"{FASTREID_RELEASE}/msmt_agw_R50.pth",
        "path": _MODELS / "agw" / "msmt_agw_R50.pth",
        "config": "configs/MSMT17/AGW_R50.yml",
    },
    "mgn": {
        "url": f"{FASTREID_RELEASE}/market_mgn_R50-ibn.pth",
        "path": _MODELS / "mgn" / "market_mgn_R50-ibn.pth",
        "config": "configs/Market1501/mgn_R50-ibn.yml",
    },
    "transreid_veri": {
        "url": "https://github.com/damo-cv/TransReID (GDrive: VeRi ViT)",
        "path": _MODELS / "transreid_veri" / "transreid_vit_veri.pth",
        "config": None,
    },
    "veri_sbs": {
        "url": f"{FASTREID_RELEASE}/veri_sbs_R50-ibn.pth",
        "path": _MODELS / "veri_sbs" / "veri_sbs_R50-ibn.pth",
        "config": "configs/VeRi/sbs_R50-ibn.yml",
    },
    "transreid_ssl": {
        "url": "https://github.com/damo-cv/TransReID-SSL (GDrive links in README)",
        "path": _MODELS / "transreid_ssl" / "vit_small_ics_msmt17.pth",
        "config": None,
    },
    "solider": {
        "url": "https://github.com/tinyvision/SOLIDER-REID (GDrive links in README)",
        "path": _MODELS / "solider" / "swin_small_msmt17.pth",
        "config": None,
    },
}


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def _l2_rows(m: np.ndarray) -> np.ndarray:
    return np.vstack([_l2(row) for row in m]).astype(np.float32)


# --------------------------------------------------------------------------
# osnet_ibn — plain torchreid, mirrors reid_benchmark's torchreid path
# --------------------------------------------------------------------------

class _TorchreidEmbedder:
    def __init__(self, model_name: str) -> None:
        os.environ.setdefault("TORCH_HOME", str(_MODELS / "torch_cache"))
        import torch
        import torchreid
        from torchvision import transforms

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = torchreid.models.build_model(model_name, num_classes=1000, pretrained=True)
        self.model.eval().to(self.device)
        self.transform = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize((256, 128)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
        self.backend = f"torchreid:{model_name}:{self.device}"
        self.key = model_name

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1), dtype=np.float32)
        batch = [self.transform(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)) for c in crops]
        tensor = self.torch.stack(batch).to(self.device)
        with self.torch.no_grad():
            feats = self.model(tensor)
        return _l2_rows(feats.detach().cpu().numpy())


# --------------------------------------------------------------------------
# agw / mgn — fastreid DefaultPredictor on the existing clone
# --------------------------------------------------------------------------

class _FastreidEmbedder:
    def __init__(self, key: str) -> None:
        import torch

        info = NEW_MODEL_WEIGHTS[key]
        weights: Path = info["path"]
        if not weights.exists():
            raise FileNotFoundError(
                f"weights missing: {weights} — download from {info['url']} "
                f"(python -m MTMC.download_new_models)"
            )
        repo_root = _ROOT / "repos" / "fastreid_sbs_bot_agw"
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from fastreid.config import get_cfg
        from fastreid.engine import DefaultPredictor

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        cfg = get_cfg()
        cfg.merge_from_file(str(repo_root / info["config"]))
        cfg.defrost()
        cfg.MODEL.WEIGHTS = str(weights)
        cfg.MODEL.DEVICE = self.device
        cfg.freeze()
        self.predictor = DefaultPredictor(cfg)
        self.cfg = cfg
        self.backend = f"fastreid:{key}:{self.device}"
        self.key = key

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        import torch

        if not crops:
            return np.empty((0, 1), dtype=np.float32)
        h, w = self.cfg.INPUT.SIZE_TEST
        batch = []
        for c in crops:
            img = cv2.cvtColor(c, cv2.COLOR_BGR2RGB)
            img = cv2.resize(img, (w, h))
            batch.append(torch.as_tensor(img.astype("float32").transpose(2, 0, 1)))
        tensor = torch.stack(batch)
        feats = self.predictor(tensor)
        return _l2_rows(feats.cpu().numpy())


# --------------------------------------------------------------------------
# lightmbn — through boxmot's ReID auto-download factory
# --------------------------------------------------------------------------

class _BoxmotEmbedder:
    """boxmot >= 19: boxmot.reid.core.reid.ReID auto-downloads by weight name."""

    def __init__(self, weights_name: str = "lmbn_n_duke.pt") -> None:
        import torch
        from boxmot.reid.core.reid import ReID  # type: ignore

        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        dest = _MODELS / "boxmot_reid" / weights_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.reid = ReID(dest, device=self.device, half=False)
        self.backend = f"boxmot:{weights_name}:{self.device}"
        self.key = weights_name

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1), dtype=np.float32)
        feats = self.reid(crops)
        if hasattr(feats, "cpu"):
            feats = feats.cpu().numpy()
        return _l2_rows(np.asarray(feats))


# --------------------------------------------------------------------------
# TransReID-family repos (transreid_ssl, solider) — colliding module names
# --------------------------------------------------------------------------

_TRANSREID_FAMILY_MODULES = ("config", "model", "utils", "datasets", "loss", "solver", "processor")


def _fresh_repo_import(repo_root: Path):
    """TransReID, TransReID-SSL and SOLIDER all define top-level packages
    named config/model/utils/... — purge cached modules and put repo first
    on sys.path so the right code loads."""
    for name in list(sys.modules):
        top = name.split(".")[0]
        if top in _TRANSREID_FAMILY_MODULES:
            del sys.modules[name]
    while str(repo_root) in sys.path:
        sys.path.remove(str(repo_root))
    sys.path.insert(0, str(repo_root))


def _torch_load_compat():
    import collections.abc
    import types

    import torch

    if "torch._six" not in sys.modules:
        torch_six = types.ModuleType("torch._six")
        torch_six.container_abcs = collections.abc
        torch_six.string_classes = (str, bytes)
        torch_six.int_classes = (int,)
        sys.modules["torch._six"] = torch_six

    original = torch.load
    def patched(*args, **kwargs):
        kwargs.setdefault("weights_only", False)
        kwargs.setdefault("map_location", "cpu")
        return original(*args, **kwargs)
    torch.load = patched

    # SOLIDER's swin_transformer.py needs exactly one symbol from mmcv
    # (mmcv.runner.load_checkpoint); mmcv won't build on Windows, so stub it.
    if "mmcv" not in sys.modules:
        def _load_checkpoint(model, filename, map_location="cpu", strict=False, logger=None):
            ckpt = torch.load(filename, map_location=map_location)
            state = ckpt.get("state_dict", ckpt.get("model", ckpt)) if isinstance(ckpt, dict) else ckpt
            model.load_state_dict(state, strict=strict)
            return ckpt
        mmcv_mod = types.ModuleType("mmcv")
        runner_mod = types.ModuleType("mmcv.runner")
        runner_mod.load_checkpoint = _load_checkpoint
        mmcv_mod.runner = runner_mod
        sys.modules["mmcv"] = mmcv_mod
        sys.modules["mmcv.runner"] = runner_mod


class _TransreidFamilyEmbedder:
    """Shared loader for TransReID-SSL (ViT-S+ICS) and SOLIDER-REID (Swin-S)."""

    def __init__(self, key: str) -> None:
        import torch
        from torchvision import transforms

        info = NEW_MODEL_WEIGHTS[key]
        weights: Path = info["path"]
        if not weights.exists():
            raise FileNotFoundError(f"weights missing: {weights} — see {info['url']}")

        _torch_load_compat()
        if key == "transreid_ssl":
            repo_root = (_ROOT / "repos" / "transreid_ssl" / "transreid_pytorch").resolve()
            config_rel = Path("configs") / "msmt17" / "vit_small_ics.yml"
            num_class, camera_num, view_num = 1041, 15, 1
        elif key == "transreid_veri":
            repo_root = (_ROOT / "repos" / "transreid").resolve()
            config_rel = Path("configs") / "VeRi" / "vit_transreid_stride.yml"
            num_class, camera_num, view_num = 576, 20, 8
        else:  # solider
            repo_root = (_ROOT / "repos" / "solider_reid").resolve()
            config_rel = Path("configs") / "msmt17" / "swin_small.yml"
            num_class, camera_num, view_num = 1041, 15, 1

        _fresh_repo_import(repo_root)
        from config import cfg          # noqa: PLC0415
        from model import make_model    # noqa: PLC0415

        cfg.defrost()
        cfg.merge_from_file(str(repo_root / config_rel))
        cfg.MODEL.PRETRAIN_CHOICE = "self"
        # transreid_ssl's ViT loads PRETRAIN_PATH unconditionally at build time
        # (point it at the fine-tuned ckpt; load_param overwrites after).
        # solider's Swin skips init when the path is empty — required, since its
        # init_weights can't parse the fine-tuned reid checkpoint format.
        cfg.MODEL.PRETRAIN_PATH = str(weights) if key == "transreid_ssl" else ""
        cfg.TEST.WEIGHT = str(weights)
        cfg.MODEL.DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        if key == "solider" and hasattr(cfg.MODEL, "SEMANTIC_WEIGHT"):
            cfg.MODEL.SEMANTIC_WEIGHT = 0.2
        cfg.freeze()

        self.device = cfg.MODEL.DEVICE
        if key == "solider":
            self.model = make_model(cfg, num_class=num_class, camera_num=camera_num,
                                    view_num=view_num, semantic_weight=0.2)
        else:
            self.model = make_model(cfg, num_class=num_class, camera_num=camera_num,
                                    view_num=view_num)
        self.model.load_param(str(weights))
        self.model.eval().to(self.device)
        self.transform = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize(tuple(cfg.INPUT.SIZE_TEST)),
            transforms.ToTensor(),
            transforms.Normalize(mean=cfg.INPUT.PIXEL_MEAN, std=cfg.INPUT.PIXEL_STD),
        ])
        self.torch = torch
        self.backend = f"{key}:msmt17:{self.device}"
        self.key = key

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1), dtype=np.float32)
        torch = self.torch
        batch = [self.transform(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)) for c in crops]
        tensor = torch.stack(batch).to(self.device)
        cam = torch.zeros((len(crops),), dtype=torch.long, device=self.device)
        view = torch.zeros((len(crops),), dtype=torch.long, device=self.device)
        with torch.no_grad():
            try:
                feats = self.model(tensor, cam_label=cam, view_label=view)
            except TypeError:
                feats = self.model(tensor)
        if isinstance(feats, (tuple, list)):
            feats = feats[0]
        return _l2_rows(feats.detach().cpu().numpy())


def load_new_embedder(key: str, tta_flip: bool = True):
    """Return (embedder, backend) or (None, reason). TTA is applied by the
    caller (MTMC.adapters.TTAEmbedder wraps whatever we return)."""
    from MTMC.adapters import TTAEmbedder  # local import to avoid cycle

    try:
        if key == "osnet_ibn":
            emb = _TorchreidEmbedder("osnet_ibn_x1_0")
        elif key in ("agw", "mgn", "veri_sbs"):
            emb = _FastreidEmbedder(key)
        elif key == "lightmbn":
            emb = _BoxmotEmbedder("lmbn_n_duke.pt")
        elif key in ("transreid_ssl", "solider", "transreid_veri"):
            emb = _TransreidFamilyEmbedder(key)
        else:
            return None, f"unknown new-model key: {key}"
    except FileNotFoundError as exc:
        return None, str(exc)
    except ImportError as exc:
        return None, f"{key}: dependency missing: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{key}: load failed: {exc}"

    class _Shim:
        """Duck-types reid_benchmark.ReIDModel enough for TTAEmbedder."""
        def __init__(self, inner):
            self._inner = inner
            self.key = key
            self.backend = inner.backend
        def embed(self, crops):
            return self._inner.embed(crops)

    return TTAEmbedder(_Shim(emb), flip=tta_flip), emb.backend
