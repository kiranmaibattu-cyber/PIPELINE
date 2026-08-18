from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .config import ensure_dirs, load_config
from .registry import MODEL_REGISTRY, selected_models
from .multimodal import ColorHistModel, FaceReIDModel, FeatureFusionModel


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    if norm <= 1e-12:
        return vector
    return vector / norm


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(1.0 - np.dot(a, b))


# Cosine-distance match thresholds calibrated per model from the verification
# eval: threshold = 1 - (mean_same_person_sim + mean_diff_person_sim) / 2.
# Lower = stricter (needs higher similarity to merge into an existing identity).
CALIBRATED_THRESHOLDS: dict[str, float] = {
    "clip_reid": 0.12,
    "pass_reid": 0.09,
    "transreid": 0.44,
    "kpr": 0.33,
    "bpbreid": 0.44,
    "deepsort_mars": 0.19,
    "fastreid_sbs_bot_agw": 0.03,
    "osnet_ain": 0.29,
    "osnet_x1_0": 0.28,
    "osnet_x0_75": 0.27,
    "osnet_x0_5": 0.25,
    "strongsort_reid": 0.23,
    "nvidia_tao_reid": 0.34,
    "fairmot": 0.24,
    "botsort_reid": 0.46,
    "openvino_reid_retail": 0.58,
}


def xyxy_iou(a: np.ndarray, b: np.ndarray) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


TRACK_EMB_BUFFER_SIZE = 5  # frames to average for stable gallery matching


@dataclass
class Track:
    local_id: int
    bbox: np.ndarray
    last_seen_frame: int
    global_id: int | None = None
    embedding: np.ndarray | None = None
    hits: int = 1
    embedding_buffer: list = field(default_factory=list)
    embedding_buffer_dict: dict = field(default_factory=dict)  # per-subkey buffers for fusion


def smooth_track_embedding(track: "Track", new_emb: np.ndarray) -> np.ndarray:
    """Append new_emb to track's buffer, return L2-normed mean of the buffer."""
    track.embedding_buffer.append(new_emb)
    if len(track.embedding_buffer) > TRACK_EMB_BUFFER_SIZE:
        track.embedding_buffer.pop(0)
    if len(track.embedding_buffer) < 2:
        return new_emb
    return l2_normalize(np.mean(np.stack(track.embedding_buffer), axis=0))


def smooth_track_embeddings_dict(
    track: "Track", new_embs: dict[str, np.ndarray]
) -> dict[str, np.ndarray]:
    """Smooth per-sub-key embeddings for score-fusion models."""
    result: dict[str, np.ndarray] = {}
    for k, emb in new_embs.items():
        buf = track.embedding_buffer_dict.setdefault(k, [])
        buf.append(emb)
        if len(buf) > TRACK_EMB_BUFFER_SIZE:
            buf.pop(0)
        result[k] = l2_normalize(np.mean(np.stack(buf), axis=0)) if len(buf) >= 2 else emb
    return result


class IoUTracker:
    def __init__(self, max_age_frames: int = 40, iou_threshold: float = 0.25) -> None:
        self.max_age_frames = max_age_frames
        self.iou_threshold = iou_threshold
        self.next_id = 1
        self.tracks: dict[int, Track] = {}

    def update(self, detections: list[np.ndarray], frame_idx: int) -> list[Track]:
        unmatched = set(range(len(detections)))
        for track in list(self.tracks.values()):
            best_idx = None
            best_iou = 0.0
            for idx in unmatched:
                iou = xyxy_iou(track.bbox, detections[idx])
                if iou > best_iou:
                    best_iou = iou
                    best_idx = idx
            if best_idx is not None and best_iou >= self.iou_threshold:
                track.bbox = detections[best_idx]
                track.last_seen_frame = frame_idx
                track.hits += 1
                unmatched.remove(best_idx)

        for idx in unmatched:
            self.tracks[self.next_id] = Track(self.next_id, detections[idx], frame_idx)
            self.next_id += 1

        expired = [
            local_id
            for local_id, track in self.tracks.items()
            if frame_idx - track.last_seen_frame > self.max_age_frames
        ]
        for local_id in expired:
            del self.tracks[local_id]

        return [track for track in self.tracks.values() if track.last_seen_frame == frame_idx]


@dataclass
class GalleryEntry:
    global_id: int
    embedding: np.ndarray
    last_seen_time: float
    seen_count: int = 1


class GlobalMatcher:
    def __init__(self, threshold: float, max_age_seconds: float) -> None:
        self.threshold = threshold
        self.max_age_seconds = max_age_seconds
        self.next_global_id = 1
        self.gallery: dict[int, GalleryEntry] = {}

    def match(self, embedding: np.ndarray, timestamp: float) -> tuple[int, float]:
        expired = [
            gid
            for gid, entry in self.gallery.items()
            if timestamp - entry.last_seen_time > self.max_age_seconds
        ]
        for gid in expired:
            del self.gallery[gid]

        best_gid = None
        best_distance = 999.0
        for gid, entry in self.gallery.items():
            distance = cosine_distance(embedding, entry.embedding)
            if distance < best_distance:
                best_distance = distance
                best_gid = gid

        if best_gid is None or best_distance > self.threshold:
            gid = self.next_global_id
            self.next_global_id += 1
            self.gallery[gid] = GalleryEntry(gid, embedding.copy(), timestamp)
            return gid, best_distance

        entry = self.gallery[best_gid]
        alpha = 0.85
        entry.embedding = l2_normalize(alpha * entry.embedding + (1 - alpha) * embedding)
        entry.last_seen_time = timestamp
        entry.seen_count += 1
        return best_gid, best_distance


class ReIDModel:
    def __init__(self, key: str) -> None:
        self.key = key
        self.name = MODEL_REGISTRY[key].name
        self.kind = MODEL_REGISTRY[key].loader
        self.backend = "unloaded"
        self.model: Any = None
        self.device = "cpu"
        self.transform = None
        self.cfg = None
        self.extract_test_embeddings = None
        # multi-modal delegates
        self._mm_delegate: Any = None

    def load(self) -> tuple[bool, str]:
        if self.kind == "color_hist":
            return self._load_color_hist()
        if self.kind == "face":
            return self._load_face()
        if self.kind == "feature_fusion":
            return self._load_feature_fusion()
        if self.kind == "fastreid":
            return self._load_fastreid()
        if self.kind == "bpbreid":
            return self._load_bpbreid()
        if self.kind == "transreid":
            return self._load_transreid()
        if self.kind == "openvino":
            return self._load_openvino()
        if self.kind == "clip_reid":
            return self._load_clip_reid()
        if self.kind == "fairmot":
            return self._load_fairmot()
        if self.kind == "kpr":
            return self._load_kpr()
        if self.kind == "strongsort":
            return self._load_strongsort()
        if self.kind == "deepsort":
            return self._load_deepsort()
        if self.kind == "nvidia_tao":
            return self._load_nvidia_tao()
        if self.kind == "pass_reid":
            return self._load_pass_reid()
        if self.kind != "torchreid":
            return False, f"{self.name} needs a model-specific adapter or credentials; run is skipped."
        os.environ.setdefault("TORCH_HOME", str(Path("models") / "torch_cache"))
        try:
            import torch
            import torchreid
            from torchvision import transforms
        except Exception as exc:  # noqa: BLE001
            return False, f"torch/torchreid unavailable: {exc}"

        name_map = {
            "osnet_ain": "osnet_ain_x1_0",
            "osnet_x1_0": "osnet_x1_0",
            "osnet_x0_75": "osnet_x0_75",
            "osnet_x0_5": "osnet_x0_5",
        }
        model_name = name_map[self.key]
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = torchreid.models.build_model(model_name, num_classes=1000, pretrained=True)
        self.model.eval().to(self.device)
        self.transform = transforms.Compose(
            [
                transforms.ToPILImage(),
                transforms.Resize((256, 128)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ]
        )
        self.backend = f"torchreid:{model_name}:{self.device}"
        return True, self.backend

    def _load_fastreid(self) -> tuple[bool, str]:
        try:
            import sys
            import torch
            repo_root = Path("repos") / "fastreid_sbs_bot_agw"
            config_path = repo_root / "configs" / "MSMT17" / "sbs_R50-ibn.yml"
            weights = Path("models") / "fastreid_sbs_bot_agw" / "msmt_sbs_R50-ibn.pth"
            model_label = "msmt_sbs_R50-ibn"
            if self.key == "botsort_reid":
                repo_root = Path("repos") / "botsort_reid"
                config_path = repo_root / "fast_reid" / "configs" / "MOT17" / "sbs_S50.yml"
                weights = Path("models") / "botsort_reid" / "mot17_sbs_S50.pth"
                model_label = "mot17_sbs_S50"
            sys.path.insert(0, str(repo_root))
            if self.key == "botsort_reid":
                from fast_reid.fastreid.config import get_cfg
                from fast_reid.fastreid.engine import DefaultPredictor
            else:
                from fastreid.config import get_cfg
                from fastreid.engine import DefaultPredictor
        except Exception as exc:  # noqa: BLE001
            return False, f"FastReID dependencies unavailable: {exc}"

        if not weights.exists():
            return False, f"FastReID weights missing: {weights}"
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        cfg = get_cfg()
        cfg.merge_from_file(str(config_path))
        cfg.defrost()
        cfg.MODEL.WEIGHTS = str(weights)
        cfg.MODEL.DEVICE = self.device
        cfg.freeze()
        self.cfg = cfg
        self.model = DefaultPredictor(cfg)
        self.backend = f"fastreid:{model_label}:{self.device}"
        return True, self.backend

    def _load_bpbreid(self) -> tuple[bool, str]:
        try:
            import sys

            import matplotlib
            import matplotlib.cm
            import torch

            if not hasattr(matplotlib.cm, "get_cmap"):
                matplotlib.cm.get_cmap = matplotlib.colormaps.get_cmap

            original_torch_load = torch.load

            def torch_load_compat(*args, **kwargs):
                kwargs.setdefault("weights_only", False)
                return original_torch_load(*args, **kwargs)

            torch.load = torch_load_compat
            repo_root = (Path("repos") / "bpbreid").resolve()
            sys.path.insert(0, str(repo_root))
            from torchreid.scripts.default_config import get_default_config
            from torchreid.tools.feature_extractor import FeatureExtractor
            from torchreid.utils.tools import extract_test_embeddings
        except Exception as exc:  # noqa: BLE001
            return False, f"BPBreID dependencies unavailable: {exc}"

        weights = (Path("models") / "bpbreid" / "pretrained_models" / "bpbreid_occluded_duke_hrnet32_10670.pth").resolve()
        if not weights.exists():
            return False, f"BPBreID weights missing: {weights}"

        cfg = get_default_config()
        cfg.merge_from_file(str(repo_root / "configs" / "bpbreid" / "bpbreid_occ_duke_test.yaml"))
        cfg.model.load_weights = str(weights)
        cfg.model.bpbreid.hrnet_pretrained_path = str((Path("models") / "bpbreid" / "pretrained_models").resolve()) + "/"
        cfg.use_gpu = torch.cuda.is_available()

        self.device = "cuda" if cfg.use_gpu else "cpu"
        self.cfg = cfg
        self.model = FeatureExtractor(
            cfg,
            model_path=cfg.model.load_weights,
            image_size=(cfg.data.height, cfg.data.width),
            device=self.device,
            verbose=False,
        )
        self.extract_test_embeddings = extract_test_embeddings
        self.backend = f"bpbreid:occluded_duke_hrnet32:{self.device}"
        return True, self.backend

    def _load_transreid(self) -> tuple[bool, str]:
        try:
            import collections.abc
            import sys
            import types

            import torch
            from torchvision import transforms

            if "torch._six" not in sys.modules:
                torch_six = types.ModuleType("torch._six")
                torch_six.container_abcs = collections.abc
                sys.modules["torch._six"] = torch_six

            original_torch_load = torch.load

            def torch_load_compat(*args, **kwargs):
                kwargs.setdefault("weights_only", False)
                kwargs.setdefault("map_location", "cpu")
                return original_torch_load(*args, **kwargs)

            torch.load = torch_load_compat
            repo_root = (Path("repos") / "transreid").resolve()
            sys.path.insert(0, str(repo_root))
            from config import cfg
            from model import make_model
        except Exception as exc:  # noqa: BLE001
            return False, f"TransReID dependencies unavailable: {exc}"

        weights = (Path("models") / "transreid" / "transreid_vit_msmt17.pth").resolve()
        if not weights.exists():
            return False, f"TransReID weights missing: {weights}"

        cfg.defrost()
        cfg.merge_from_file(str(repo_root / "configs" / "MSMT17" / "vit_transreid_stride.yml"))
        cfg.MODEL.PRETRAIN_CHOICE = "self"
        cfg.TEST.WEIGHT = str(weights)
        cfg.MODEL.DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        cfg.freeze()

        self.device = cfg.MODEL.DEVICE
        self.cfg = cfg
        self.model = make_model(cfg, num_class=1041, camera_num=15, view_num=1)
        self.model.load_param(cfg.TEST.WEIGHT)
        self.model.eval().to(self.device)
        self.transform = transforms.Compose(
            [
                transforms.ToPILImage(),
                transforms.Resize(tuple(cfg.INPUT.SIZE_TEST)),
                transforms.ToTensor(),
                transforms.Normalize(mean=cfg.INPUT.PIXEL_MEAN, std=cfg.INPUT.PIXEL_STD),
            ]
        )
        self.backend = f"transreid:vit_msmt17_stride:{self.device}"
        return True, self.backend

    def _load_openvino(self) -> tuple[bool, str]:
        try:
            import openvino as ov
        except Exception as exc:  # noqa: BLE001
            return False, f"OpenVINO runtime unavailable: {exc}"

        model_path = Path("models") / "openvino_reid_retail" / "person-reidentification-retail-0288.xml"
        if not model_path.exists():
            return False, f"OpenVINO model missing: {model_path}"

        core = ov.Core()
        model = core.read_model(str(model_path))
        self.model = core.compile_model(model, "CPU")
        self.input_layer = self.model.input(0)
        self.output_layer = self.model.output(0)
        shape = [int(dim) for dim in self.input_layer.shape]
        if len(shape) != 4 or shape[2:] != [256, 128]:
            return False, f"Unexpected OpenVINO input shape: {shape}"
        self.backend = "openvino:person-reidentification-retail-0288:CPU"
        return True, self.backend

    def _load_clip_reid(self) -> tuple[bool, str]:
        try:
            import sys

            import torch
            from torchvision import transforms

            original_torch_load = torch.load

            def torch_load_compat(*args, **kwargs):
                kwargs.setdefault("weights_only", False)
                kwargs.setdefault("map_location", "cpu")
                return original_torch_load(*args, **kwargs)

            torch.load = torch_load_compat
            repo_root = (Path("repos") / "clip_reid").resolve()
            sys.path.insert(0, str(repo_root))
            from config import cfg
            from model.make_model_clipreid import make_model
        except Exception as exc:  # noqa: BLE001
            return False, f"CLIP-ReID dependencies unavailable: {exc}"

        weights = (Path("models") / "clip_reid" / "vit_clipreid_sie_olp_msmt17.pth").resolve()
        if not weights.exists():
            return False, f"CLIP-ReID weights missing: {weights}"

        # Checkpoint is ViT-B-16 trained with SIE (camera) + OLP on MSMT17,
        # overlapping stride 12 (21x10 patches -> 211 positional embeddings).
        cfg.defrost()
        cfg.MODEL.NAME = "ViT-B-16"
        cfg.MODEL.STRIDE_SIZE = [12, 12]
        cfg.MODEL.SIE_CAMERA = True
        cfg.MODEL.SIE_VIEW = False
        cfg.MODEL.SIE_COE = 1.0
        cfg.INPUT.SIZE_TRAIN = [256, 128]
        cfg.INPUT.SIZE_TEST = [256, 128]
        cfg.INPUT.PIXEL_MEAN = [0.5, 0.5, 0.5]
        cfg.INPUT.PIXEL_STD = [0.5, 0.5, 0.5]
        cfg.TEST.NECK_FEAT = "before"
        cfg.DATASETS.NAMES = "msmt17"
        cfg.freeze()

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.cfg = cfg
        self.model = make_model(cfg, num_class=1041, camera_num=15, view_num=1)
        self.model.load_param(str(weights))
        self.model.eval().to(self.device)
        self.transform = transforms.Compose(
            [
                transforms.ToPILImage(),
                transforms.Resize(tuple(cfg.INPUT.SIZE_TEST)),
                transforms.ToTensor(),
                transforms.Normalize(mean=cfg.INPUT.PIXEL_MEAN, std=cfg.INPUT.PIXEL_STD),
            ]
        )
        self.backend = f"clip_reid:vit_msmt17_sie_olp:{self.device}"
        return True, self.backend

    def _load_fairmot(self) -> tuple[bool, str]:
        try:
            import math
            import sys
            import types

            import torch
            import torch.nn as nn
            from torchvision.ops import deform_conv2d
        except Exception as exc:  # noqa: BLE001
            return False, f"FairMOT dependencies unavailable: {exc}"

        # DCNv2 is not built on aarch64; provide a weight-compatible shim
        # (same param names: weight/bias + conv_offset_mask) backed by
        # torchvision's modulated deformable convolution.
        def _pair(x):
            return (x, x) if isinstance(x, int) else tuple(x)

        class _DCN(nn.Module):
            def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                         padding=0, dilation=1, deformable_groups=1):
                super().__init__()
                ks = _pair(kernel_size)
                self.kernel_size = ks
                self.stride = _pair(stride)
                self.padding = _pair(padding)
                self.dilation = _pair(dilation)
                self.deformable_groups = deformable_groups
                self.weight = nn.Parameter(torch.empty(out_channels, in_channels, *ks))
                self.bias = nn.Parameter(torch.zeros(out_channels))
                chan = deformable_groups * 3 * ks[0] * ks[1]
                self.conv_offset_mask = nn.Conv2d(
                    in_channels, chan, kernel_size=ks, stride=self.stride,
                    padding=self.padding, dilation=self.dilation, bias=True,
                )
                stdv = 1.0 / math.sqrt(in_channels * ks[0] * ks[1])
                self.weight.data.uniform_(-stdv, stdv)
                self.conv_offset_mask.weight.data.zero_()
                self.conv_offset_mask.bias.data.zero_()

            def forward(self, x):
                o = self.conv_offset_mask(x)
                o1, o2, mask = torch.chunk(o, 3, dim=1)
                offset = torch.cat((o1, o2), dim=1)
                mask = torch.sigmoid(mask)
                return deform_conv2d(
                    x, offset, self.weight, self.bias, stride=self.stride,
                    padding=self.padding, dilation=self.dilation, mask=mask,
                )

        shim = types.ModuleType("dcn_v2")
        shim.DCN = _DCN
        sys.modules.setdefault("dcn_v2", shim)

        try:
            lib_root = (Path("repos") / "fairmot" / "src" / "lib").resolve()
            if str(lib_root) not in sys.path:
                sys.path.insert(0, str(lib_root))
            from models.model import create_model, load_model
        except Exception as exc:  # noqa: BLE001
            return False, f"FairMOT model code unavailable: {exc}"

        weights = (Path("models") / "fairmot" / "fairmot_dla34.pth").resolve()
        if not weights.exists():
            return False, f"FairMOT weights missing: {weights}"

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        heads = {"hm": 1, "wh": 4, "id": 128, "reg": 2}
        model = create_model("dla_34", heads, 256)
        model = load_model(model, str(weights))
        self.model = model.eval().to(self.device)
        self._fairmot_mean = np.array([0.408, 0.447, 0.470], dtype=np.float32)
        self._fairmot_std = np.array([0.289, 0.274, 0.278], dtype=np.float32)
        self.backend = f"fairmot:dla34_id128:{self.device}"
        return True, self.backend

    def _load_kpr(self) -> tuple[bool, str]:
        # KPR (keypoint-promptable Re-ID) lives in a torchreid fork that loads
        # relative config/weight paths, so build the extractor from inside the
        # repo dir and restore the cwd afterwards. Image-only mode is enabled by
        # disabling inference prompting (no keypoint detector required).
        try:
            import sys

            import torch
        except Exception as exc:  # noqa: BLE001
            return False, f"KPR dependencies unavailable: {exc}"

        # torch.load compat + matplotlib.cm shim used by the fork.
        try:
            _orig_load = torch.load

            def _compat_load(*args, **kwargs):
                kwargs.setdefault("weights_only", False)
                kwargs.setdefault("map_location", "cpu")
                return _orig_load(*args, **kwargs)

            torch.load = _compat_load
            import matplotlib.cm as _cm  # noqa: F401

            if not hasattr(_cm, "get_cmap"):
                import matplotlib

                _cm.get_cmap = matplotlib.colormaps.get_cmap
        except Exception:  # noqa: BLE001
            pass

        repo_root = (Path("repos") / "kpr").resolve()
        if not repo_root.exists():
            return False, f"KPR repo missing: {repo_root}"
        cfg_path = "configs/kpr/imagenet/kpr_occ_posetrack_test.yaml"

        prev_cwd = os.getcwd()
        try:
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            os.chdir(repo_root)
            from torchreid.scripts.builder import build_config
            from torchreid.tools.feature_extractor import KPRFeatureExtractor

            cfg = build_config(config_path=cfg_path)
            cfg.use_gpu = torch.cuda.is_available()
            cfg.model.promptable_trans.disable_inference_prompting = True
            self.extractor = KPRFeatureExtractor(
                cfg,
                image_size=(cfg.data.height, cfg.data.width),
                pixel_mean=cfg.data.norm_mean,
                pixel_std=cfg.data.norm_std,
                verbose=False,
            )
            self.cfg = cfg
        except Exception as exc:  # noqa: BLE001
            return False, f"KPR load failed: {exc}"
        finally:
            os.chdir(prev_cwd)

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.backend = f"kpr:swinv2_posetrack_9x512:{self.device}"
        return True, self.backend

    def _load_strongsort(self) -> tuple[bool, str]:
        # StrongSORT / BoxMOT default appearance model is OSNet x0.25 trained on
        # MSMT17. Build via torchreid and load the downloaded reid checkpoint.
        try:
            import torch
            import torchreid
            try:
                from torchreid.utils import load_pretrained_weights
            except ImportError:
                from torchreid.reid.utils import load_pretrained_weights
            from torchvision import transforms
        except Exception as exc:  # noqa: BLE001
            return False, f"torch/torchreid unavailable: {exc}"

        weights = (Path("models") / "strongsort_reid" / "osnet_x0_25_msmt17.pth").resolve()
        if not weights.exists():
            return False, f"StrongSORT weights missing: {weights}"

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        model = torchreid.models.build_model("osnet_x0_25", num_classes=1041, pretrained=False)
        load_pretrained_weights(model, str(weights))
        self.model = model.eval().to(self.device)
        self.transform = transforms.Compose(
            [
                transforms.ToPILImage(),
                transforms.Resize((256, 128)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ]
        )
        self.backend = f"strongsort:osnet_x0_25_msmt17:{self.device}"
        return True, self.backend

    def _load_deepsort(self) -> tuple[bool, str]:
        # DeepSORT PyTorch appearance CNN (ResNet-18-style, Market1501 ckpt.t7).
        # The network definition is reconstructed inline from the checkpoint's
        # module layout so no tracker repo code is required.
        try:
            import torch
            import torch.nn as nn
            import torch.nn.functional as F
        except Exception as exc:  # noqa: BLE001
            return False, f"torch unavailable: {exc}"

        class _BasicBlock(nn.Module):
            def __init__(self, inc, outc, stride=1):
                super().__init__()
                self.conv1 = nn.Conv2d(inc, outc, 3, stride, 1, bias=False)
                self.bn1 = nn.BatchNorm2d(outc)
                self.conv2 = nn.Conv2d(outc, outc, 3, 1, 1, bias=False)
                self.bn2 = nn.BatchNorm2d(outc)
                self.downsample = None
                if stride != 1 or inc != outc:
                    self.downsample = nn.Sequential(
                        nn.Conv2d(inc, outc, 1, stride, bias=False),
                        nn.BatchNorm2d(outc),
                    )

            def forward(self, x):
                identity = x
                out = F.relu(self.bn1(self.conv1(x)))
                out = self.bn2(self.conv2(out))
                if self.downsample is not None:
                    identity = self.downsample(x)
                return F.relu(out + identity)

        class _Net(nn.Module):
            def __init__(self, num_classes=751):
                super().__init__()
                self.conv = nn.Sequential(
                    nn.Conv2d(3, 64, 3, stride=1, padding=1),
                    nn.BatchNorm2d(64),
                    nn.ReLU(inplace=True),
                    nn.MaxPool2d(3, 2, padding=1),
                )
                self.layer1 = nn.Sequential(_BasicBlock(64, 64), _BasicBlock(64, 64))
                self.layer2 = nn.Sequential(_BasicBlock(64, 128, 2), _BasicBlock(128, 128))
                self.layer3 = nn.Sequential(_BasicBlock(128, 256, 2), _BasicBlock(256, 256))
                self.layer4 = nn.Sequential(_BasicBlock(256, 512, 2), _BasicBlock(512, 512))
                self.classifier = nn.Sequential(
                    nn.Linear(512, 256),
                    nn.BatchNorm1d(256),
                    nn.ReLU(inplace=True),
                    nn.Dropout(),
                    nn.Linear(256, num_classes),
                )

            def forward(self, x):
                x = self.conv(x)
                x = self.layer1(x)
                x = self.layer2(x)
                x = self.layer3(x)
                x = self.layer4(x)
                x = F.adaptive_avg_pool2d(x, 1).flatten(1)
                return x  # 512-D pooled appearance feature

        weights = (Path("models") / "deepsort_mars" / "ckpt.t7").resolve()
        if not weights.exists():
            return False, f"DeepSORT weights missing: {weights}"

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        state = torch.load(str(weights), map_location="cpu", weights_only=False)["net_dict"]
        model = _Net(num_classes=751)
        missing, unexpected = model.load_state_dict(state, strict=False)
        # only the final classifier.4 (751-way head) is unused for embeddings
        self.model = model.eval().to(self.device)
        self._deepsort_mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self._deepsort_std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        self.backend = f"deepsort:resnet18_market1501_512:{self.device}"
        return True, self.backend

    def _load_nvidia_tao(self) -> tuple[bool, str]:
        # NVIDIA TAO ReIdentificationNet: ResNet50 ONNX, 256x128 input, 256-D out.
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001
            return False, f"onnxruntime unavailable: {exc}"

        weights = (Path("models") / "nvidia_tao_reid" / "resnet50_market1501_aicity156.onnx").resolve()
        if not weights.exists():
            return False, f"TAO ONNX missing: {weights}"

        avail = ort.get_available_providers()
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if "CUDAExecutionProvider" in avail else ["CPUExecutionProvider"]
        self.model = ort.InferenceSession(str(weights), providers=providers)
        self._tao_input = self.model.get_inputs()[0].name
        self._tao_output = self.model.get_outputs()[0].name
        # TAO ReID preprocessing: ImageNet mean/std on 0-255 scaled to 0-1.
        self._tao_mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self._tao_std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        used = self.model.get_providers()[0]
        self.device = "cuda" if "CUDA" in used else "cpu"
        self.backend = f"nvidia_tao:resnet50_market1501:{self.device}"
        return True, self.backend

    def _load_pass_reid(self) -> tuple[bool, str]:
        # PASS / TransReID-SSL part-aware ViT-B/16 fine-tuned on MSMT17.
        # Built from the cloned PASS_transreid repo; emits a 1536-D descriptor
        # (cls token + mean of 3 part tokens, concatenated).
        try:
            import collections.abc
            import sys
            import types

            import torch
            from torchvision import transforms  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, f"PASS dependencies unavailable: {exc}"

        # PASS code imports torch._six, removed in modern torch.
        _six = types.ModuleType("torch._six")
        _six.container_abcs = collections.abc
        _six.int_classes = int
        _six.string_classes = str
        sys.modules.setdefault("torch._six", _six)

        repo = (Path("repos") / "pass_reid" / "PASS_transreid").resolve()
        if not repo.exists():
            return False, f"PASS repo missing: {repo}"
        weights = (Path("models") / "pass_reid" / "pass_vit_base_msmt17.pth").resolve()
        if not weights.exists():
            return False, f"PASS weights missing: {weights}"

        prev_cwd = os.getcwd()
        try:
            if str(repo) not in sys.path:
                sys.path.insert(0, str(repo))
            os.chdir(repo)
            from config import cfg
            cfg.merge_from_file("configs/msmt17/vit_base.yml")
            cfg.MODEL.PRETRAIN_CHOICE = ""  # skip LUPerson backbone; load full finetuned weights below
            from model import make_model

            model = make_model(cfg, num_class=1041, camera_num=0, view_num=0)
        except Exception as exc:  # noqa: BLE001
            return False, f"PASS model build failed: {exc}"
        finally:
            os.chdir(prev_cwd)

        model.load_param(str(weights))
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = model.eval().to(self.device)
        self._pass_mean = np.array([0.5, 0.5, 0.5], dtype=np.float32)
        self._pass_std = np.array([0.5, 0.5, 0.5], dtype=np.float32)
        self.backend = f"pass_reid:vit_base_msmt17_parts:{self.device}"
        return True, self.backend

    def _load_color_hist(self) -> tuple[bool, str]:
        self._mm_delegate = ColorHistModel()
        ok, info = self._mm_delegate.load()
        if ok:
            self.backend = info
            self.device = "cpu"
        return ok, info

    def _load_face(self) -> tuple[bool, str]:
        self._mm_delegate = FaceReIDModel()
        ok, info = self._mm_delegate.load()
        if ok:
            self.backend = info
            self.device = self._mm_delegate.device
        return ok, info

    def _load_feature_fusion(self) -> tuple[bool, str]:
        spec = MODEL_REGISTRY[self.key]
        if not spec.sub_keys:
            return False, f"feature_fusion model '{self.key}' has no sub_keys in registry"
        self._mm_delegate = FeatureFusionModel(self.key, spec.sub_keys)
        ok, info = self._mm_delegate.load()
        if ok:
            self.backend = info
            self.device = self._mm_delegate.device
        return ok, info

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if self._mm_delegate is not None:
            return self._mm_delegate.embed(crops)
        if self.kind == "fastreid":
            return self._embed_fastreid(crops)
        if self.kind == "bpbreid":
            return self._embed_bpbreid(crops)
        if self.kind == "transreid":
            return self._embed_transreid(crops)
        if self.kind == "openvino":
            return self._embed_openvino(crops)
        if self.kind == "clip_reid":
            return self._embed_clip_reid(crops)
        if self.kind == "fairmot":
            return self._embed_fairmot(crops)
        if self.kind == "kpr":
            return self._embed_kpr(crops)
        if self.kind == "strongsort":
            return self._embed_strongsort(crops)
        if self.kind == "deepsort":
            return self._embed_deepsort(crops)
        if self.kind == "nvidia_tao":
            return self._embed_nvidia_tao(crops)
        if self.kind == "pass_reid":
            return self._embed_pass_reid(crops)
        if not crops:
            return np.empty((0, 1), dtype=np.float32)
        import torch

        batch = []
        for crop in crops:
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            batch.append(self.transform(rgb))
        tensor = torch.stack(batch).to(self.device)
        with torch.no_grad():
            features = self.model(tensor)
        out = features.detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in out])

    def _embed_bpbreid(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1), dtype=np.float32)

        import torch

        rgb_crops = [cv2.cvtColor(crop, cv2.COLOR_BGR2RGB) for crop in crops]
        with torch.no_grad():
            model_output = self.model(rgb_crops)
            embeddings, _visibility_scores, _parts_masks, _pixels_cls_scores = self.extract_test_embeddings(
                model_output,
                self.cfg.model.bpbreid.test_embeddings,
            )
        flat = embeddings.flatten(1).detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in flat])

    def _embed_transreid(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 3840), dtype=np.float32)

        import torch

        batch = []
        for crop in crops:
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            batch.append(self.transform(rgb))
        tensor = torch.stack(batch).to(self.device)
        cam_label = torch.zeros((len(crops),), dtype=torch.long, device=self.device)
        view_label = torch.zeros((len(crops),), dtype=torch.long, device=self.device)
        with torch.no_grad():
            features = self.model(tensor, cam_label=cam_label, view_label=view_label)
        out = features.detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in out])

    def _embed_fairmot(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 128), dtype=np.float32)

        import torch

        batch = []
        for crop in crops:
            resized = cv2.resize(crop, (128, 256), interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            rgb = (rgb - self._fairmot_mean) / self._fairmot_std
            batch.append(torch.from_numpy(rgb.transpose(2, 0, 1)))
        tensor = torch.stack(batch).to(self.device)
        with torch.no_grad():
            output = self.model(tensor)
            head = output[-1] if isinstance(output, (list, tuple)) else output
            id_map = head["id"]
            feat = torch.nn.functional.adaptive_avg_pool2d(id_map, 1).flatten(1)
        out = feat.detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in out])

    def _embed_strongsort(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 512), dtype=np.float32)

        import torch

        batch = []
        for crop in crops:
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            batch.append(self.transform(rgb))
        tensor = torch.stack(batch).to(self.device)
        with torch.no_grad():
            features = self.model(tensor)
        out = features.detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in out])

    def _embed_deepsort(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 512), dtype=np.float32)

        import torch

        batch = []
        for crop in crops:
            resized = cv2.resize(crop, (64, 128), interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            rgb = (rgb - self._deepsort_mean) / self._deepsort_std
            batch.append(torch.from_numpy(rgb.transpose(2, 0, 1)))
        tensor = torch.stack(batch).to(self.device)
        with torch.no_grad():
            features = self.model(tensor)
        out = features.detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in out])

    def _embed_nvidia_tao(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 256), dtype=np.float32)

        batch = []
        for crop in crops:
            resized = cv2.resize(crop, (128, 256), interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            rgb = (rgb - self._tao_mean) / self._tao_std
            batch.append(rgb.transpose(2, 0, 1))
        tensor = np.stack(batch).astype(np.float32)
        out = self.model.run([self._tao_output], {self._tao_input: tensor})[0]
        out = np.asarray(out, dtype=np.float32)
        return np.vstack([l2_normalize(row) for row in out])

    def _embed_pass_reid(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1536), dtype=np.float32)

        import torch

        batch = []
        for crop in crops:
            resized = cv2.resize(crop, (128, 256), interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            rgb = (rgb - self._pass_mean) / self._pass_std
            batch.append(torch.from_numpy(rgb.transpose(2, 0, 1)))
        tensor = torch.stack(batch).to(self.device)
        with torch.no_grad():
            features = self.model(tensor, cam_label=None, view_label=None)
        out = features.detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in out])

    def _embed_kpr(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 4608), dtype=np.float32)

        import torch

        # KPR's getitem expects RGB numpy images (mirrors its read_image path).
        samples = [{"image": cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)} for crop in crops]
        with torch.no_grad():
            _updated, embeddings, _vis, _masks = self.extractor(samples)
        flat = embeddings.flatten(1).detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in flat])

    def _embed_clip_reid(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1280), dtype=np.float32)

        import torch

        batch = []
        for crop in crops:
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            batch.append(self.transform(rgb))
        tensor = torch.stack(batch).to(self.device)
        cam_label = torch.zeros((len(crops),), dtype=torch.long, device=self.device)
        view_label = torch.zeros((len(crops),), dtype=torch.long, device=self.device)
        with torch.no_grad():
            features = self.model(tensor, cam_label=cam_label, view_label=view_label)
        out = features.detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in out])

    def _embed_openvino(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 256), dtype=np.float32)

        outputs = []
        for crop in crops:
            resized = cv2.resize(crop, (128, 256), interpolation=cv2.INTER_LINEAR)
            tensor = resized.astype(np.float32).transpose(2, 0, 1)[None, ...]
            result = self.model([tensor])[self.output_layer]
            vector = np.asarray(result[0], dtype=np.float32)
            outputs.append(l2_normalize(vector))
        return np.vstack(outputs)

    def _embed_fastreid(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1), dtype=np.float32)
        import torch

        size = tuple(self.cfg.INPUT.SIZE_TEST[::-1])
        batch = []
        for crop in crops:
            rgb = crop[:, :, ::-1]
            image = cv2.resize(rgb, size, interpolation=cv2.INTER_CUBIC)
            tensor = torch.as_tensor(image.astype("float32").transpose(2, 0, 1))
            batch.append(tensor)
        inputs = torch.stack(batch)
        features = self.model(inputs)
        out = features.detach().cpu().numpy().astype(np.float32)
        return np.vstack([l2_normalize(row) for row in out])


class Detector:
    def __init__(self, model_name: str, conf: float, iou: float, person_class_id: int) -> None:
        from ultralytics import YOLO
        import torch

        self.model = YOLO(model_name)
        self.device = 0 if torch.cuda.is_available() else "cpu"
        try:
            self.model.to("cuda" if torch.cuda.is_available() else "cpu")
        except Exception:
            pass
        self.conf = conf
        self.iou = iou
        self.person_class_id = person_class_id

    def detect(self, frame: np.ndarray) -> list[np.ndarray]:
        results = self.model.predict(
            frame, conf=self.conf, iou=self.iou, verbose=False, device=self.device
        )
        boxes: list[np.ndarray] = []
        if not results:
            return boxes
        for box in results[0].boxes:
            cls = int(box.cls[0])
            if cls != self.person_class_id:
                continue
            boxes.append(box.xyxy[0].detach().cpu().numpy().astype(np.float32))
        return boxes


def crop_boxes(frame: np.ndarray, boxes: list[np.ndarray]) -> list[np.ndarray]:
    h, w = frame.shape[:2]
    crops = []
    for box in boxes:
        x1, y1, x2, y2 = box.astype(int)
        x1 = max(0, min(w - 1, x1))
        y1 = max(0, min(h - 1, y1))
        x2 = max(0, min(w, x2))
        y2 = max(0, min(h, y2))
        if x2 > x1 and y2 > y1:
            crops.append(frame[y1:y2, x1:x2].copy())
        else:
            crops.append(np.zeros((256, 128, 3), dtype=np.uint8))
    return crops


def draw_tracks(frame: np.ndarray, tracks: list[Track], label: str) -> np.ndarray:
    out = frame.copy()
    cv2.putText(out, label, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 220, 255), 2)
    for track in tracks:
        x1, y1, x2, y2 = track.bbox.astype(int)
        gid = track.global_id if track.global_id is not None else -1
        color = ((gid * 37) % 255, (gid * 17) % 255, (gid * 97) % 255)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            out,
            f"G{gid} L{track.local_id}",
            (x1, max(20, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
        )
    return out


def hstack_resize(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    target_h = min(left.shape[0], right.shape[0])
    def resize(img: np.ndarray) -> np.ndarray:
        scale = target_h / img.shape[0]
        return cv2.resize(img, (int(img.shape[1] * scale), target_h))

    return np.hstack([resize(left), resize(right)])


@dataclass
class StreamState:
    label: str
    capture: cv2.VideoCapture
    tracker: IoUTracker
    frame_idx: int = 0
    fps: float = 20.0
    current_frame: np.ndarray | None = None
    current_tracks: list[Track] = field(default_factory=list)


def process_stream(
    state: StreamState,
    detector: Detector,
    reid: ReIDModel,
    matcher: GlobalMatcher,
    frame_time_seconds: float,
) -> tuple[bool, dict[str, float]]:
    timing: dict[str, float] = {}
    t0 = time.perf_counter()
    ok, frame = state.capture.read()
    timing["read_ms"] = (time.perf_counter() - t0) * 1000
    if not ok:
        return False, timing
    state.frame_idx += 1
    state.current_frame = frame

    t0 = time.perf_counter()
    boxes = detector.detect(frame)
    timing["detector_ms"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    tracks = state.tracker.update(boxes, state.frame_idx)
    timing["tracker_ms"] = (time.perf_counter() - t0) * 1000

    boxes_for_tracks = [track.bbox for track in tracks]
    t0 = time.perf_counter()
    crops = crop_boxes(frame, boxes_for_tracks)
    timing["crop_ms"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    embeddings = reid.embed(crops)
    timing["reid_ms"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    for track, embedding in zip(tracks, embeddings):
        match_emb = smooth_track_embedding(track, embedding)
        gid, _distance = matcher.match(match_emb, frame_time_seconds)
        track.global_id = gid
        track.embedding = match_emb
    timing["matching_ms"] = (time.perf_counter() - t0) * 1000
    timing["detections"] = float(len(boxes))
    timing["tracks"] = float(len(tracks))
    state.current_tracks = tracks
    return True, timing


def open_writer(path: Path, frame: np.ndarray, fps: float) -> cv2.VideoWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    return cv2.VideoWriter(str(path), fourcc, fps, (frame.shape[1], frame.shape[0]))


def run_scenario_score_fusion(model_key: str, scenario: str, config: dict[str, Any], display: bool) -> dict[str, Any]:
    """Variant of run_scenario for score-fusion models.

    Loads N sub-models, maintains a ScoreFusionGallery that averages cosine
    distances from all sub-model embeddings, and writes the same output
    artefacts (video, timing CSV, track-events CSV, summary JSON) as the
    standard runner.
    """
    from .fusion import ScoreFusionGallery, ScoreFusionReIDModel

    spec = MODEL_REGISTRY[model_key]
    reid = ScoreFusionReIDModel(model_key, spec.sub_keys)
    loaded, load_detail = reid.load()
    if not loaded:
        return {
            "model": model_key,
            "scenario": scenario,
            "run_status": "skipped",
            "reason": load_detail,
        }

    bench = config["benchmark"]
    detector = Detector(bench["detector"], bench["confidence"], bench["iou"], bench["person_class_id"])
    sub_thresholds = {k: CALIBRATED_THRESHOLDS.get(k, bench["match_threshold"]) for k in spec.sub_keys}
    matcher = ScoreFusionGallery(spec.sub_keys, sub_thresholds, bench["max_gallery_age_seconds"])

    videos = config["videos"]
    if scenario == "single_delay":
        path_a, path_b = videos["ch9_5min"], videos["ch9_5min"]
        label_a, label_b = "ch9 live", f"ch9 +{bench['delay_seconds']}s"
    elif scenario == "cross_camera":
        path_a, path_b = videos["ch9_5min"], videos["ch10_5min"]
        label_a, label_b = "ch9", "ch10"
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    cap_a = cv2.VideoCapture(path_a)
    cap_b = cv2.VideoCapture(path_b)
    if not cap_a.isOpened() or not cap_b.isOpened():
        return {"model": model_key, "scenario": scenario, "run_status": "failed", "reason": "Could not open input videos."}

    fps = cap_a.get(cv2.CAP_PROP_FPS) or float(bench["output_fps"])
    if scenario == "single_delay":
        cap_b.set(cv2.CAP_PROP_POS_FRAMES, int(round(fps * bench["delay_seconds"])))

    state_a = StreamState(label_a, cap_a, IoUTracker(), fps=fps)
    state_b = StreamState(label_b, cap_b, IoUTracker(), fps=fps)

    model_output_dir = Path(config["paths"]["outputs_dir"]) / model_key
    model_report_dir = Path(config["paths"]["reports_dir"]) / model_key
    model_report_dir.mkdir(parents=True, exist_ok=True)
    timing_path = model_report_dir / f"{scenario}_timing.csv"
    events_path = model_report_dir / f"{scenario}_track_events.csv"
    output_path = model_output_dir / f"{scenario}.mp4"

    writer = None
    rows: list[dict] = []
    events: list[dict] = []
    frame_idx = 0
    process_every = max(1, int(bench.get("process_every_n_frames", 1)))
    max_frames = int(bench.get("max_frames", 0) or 0)
    started = time.perf_counter()

    while True:
        frame_start = time.perf_counter()
        frame_time_s = frame_idx / fps

        # ── read + detect stream A ──
        ok_a, frame_a = state_a.capture.read()
        ok_b, frame_b = state_b.capture.read()
        if not ok_a or not ok_b:
            break
        state_a.frame_idx += 1
        state_b.frame_idx += 1
        state_a.current_frame = frame_a
        state_b.current_frame = frame_b

        t0 = time.perf_counter()
        boxes_a = detector.detect(frame_a)
        boxes_b = detector.detect(frame_b)
        det_ms = (time.perf_counter() - t0) * 1000

        tracks_a = state_a.tracker.update(boxes_a, state_a.frame_idx)
        tracks_b = state_b.tracker.update(boxes_b, state_b.frame_idx)

        crops_a = crop_boxes(frame_a, [t.bbox for t in tracks_a])
        crops_b = crop_boxes(frame_b, [t.bbox for t in tracks_b])

        t0 = time.perf_counter()
        emb_a = reid.embed_multi(crops_a)  # dict[sub_key -> (N, D)]
        emb_b = reid.embed_multi(crops_b)
        reid_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        for i, track in enumerate(tracks_a):
            per_track = {k: emb_a[k][i] for k in reid.sub_keys if i < emb_a[k].shape[0]}
            per_track = smooth_track_embeddings_dict(track, per_track)
            gid, _ = matcher.match(per_track, frame_time_s)
            track.global_id = gid
        for i, track in enumerate(tracks_b):
            per_track = {k: emb_b[k][i] for k in reid.sub_keys if i < emb_b[k].shape[0]}
            per_track = smooth_track_embeddings_dict(track, per_track)
            gid, _ = matcher.match(per_track, frame_time_s)
            track.global_id = gid
        match_ms = (time.perf_counter() - t0) * 1000

        state_a.current_tracks = tracks_a
        state_b.current_tracks = tracks_b

        left = draw_tracks(frame_a, tracks_a, label_a)
        right = draw_tracks(frame_b, tracks_b, label_b)
        combined = hstack_resize(left, right)
        cv2.putText(combined, f"{spec.name} | {scenario}",
                    (12, combined.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        if writer is None:
            model_output_dir.mkdir(parents=True, exist_ok=True)
            writer = open_writer(output_path, combined, float(bench["output_fps"]))
        writer.write(combined)

        if display:
            cv2.imshow(f"Re-ID {model_key} {scenario}", combined)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        total_ms = (time.perf_counter() - frame_start) * 1000
        rows.append({
            "frame": frame_idx, "scenario": scenario, "model": model_key,
            "detector_ms": det_ms, "reid_ms": reid_ms, "matching_ms": match_ms,
            "total_ms": total_ms, "live_fps": 1000.0 / total_ms if total_ms > 0 else 0.0,
            "detections": len(boxes_a) + len(boxes_b),
            "tracks": len(tracks_a) + len(tracks_b),
            "gallery_size": len(matcher.gallery),
        })
        for cam_label, state in ((label_a, state_a), (label_b, state_b)):
            for track in state.current_tracks:
                x1, y1, x2, y2 = track.bbox.astype(float)
                events.append({
                    "frame": frame_idx, "scenario": scenario, "model": model_key,
                    "camera": cam_label, "local_id": track.local_id,
                    "global_id": track.global_id,
                    "x1": round(x1, 2), "y1": round(y1, 2),
                    "x2": round(x2, 2), "y2": round(y2, 2),
                })
        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"{model_key}/{scenario}: {frame_idx} sampled frames", flush=True)
        if max_frames and frame_idx >= max_frames:
            break
        if process_every > 1:
            for _ in range(process_every - 1):
                grabbed_a = state_a.capture.grab()
                grabbed_b = state_b.capture.grab()
                if grabbed_a:
                    state_a.frame_idx += 1
                if grabbed_b:
                    state_b.frame_idx += 1
                if not grabbed_a or not grabbed_b:
                    break

    if writer is not None:
        writer.release()
    cap_a.release()
    cap_b.release()
    if display:
        cv2.destroyAllWindows()

    if rows:
        with timing_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    if events:
        with events_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(events[0].keys()))
            w.writeheader()
            w.writerows(events)

    elapsed = time.perf_counter() - started
    summary = {
        "model": model_key,
        "model_name": spec.name,
        "backend": reid.backend,
        "scenario": scenario,
        "run_status": "passed" if rows else "failed",
        "frames": len(rows),
        "elapsed_seconds": round(elapsed, 3),
        "avg_live_fps": round(float(np.mean([r["live_fps"] for r in rows])) if rows else 0.0, 3),
        "avg_detector_ms": round(float(np.mean([r["detector_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_reid_ms": round(float(np.mean([r["reid_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_total_ms": round(float(np.mean([r["total_ms"] for r in rows])) if rows else 0.0, 3),
        "output_video": str(output_path),
        "timing_csv": str(timing_path),
        "track_events_csv": str(events_path),
        "accuracy_note": "Ground-truth accuracy requires annotations/annotations.csv; this run reports timing and visual/proxy ID consistency only.",
    }
    (model_report_dir / f"{scenario}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _quality_weights_for(model_key: str, sub_keys: tuple, embs: dict, crop_area: float) -> dict:
    """Compute per-modality quality weights for FarSight-style and IDSelect-style."""
    if model_key == "farsight_style":
        face_emb = embs.get("face_reid", np.zeros(1))
        face_detected = float(np.linalg.norm(face_emb)) > 0.5
        area_score = min(1.0, crop_area / 30000.0)
        return {
            "osnet_ain":       0.4 + 0.6 * area_score,  # 0.4 – 1.0 scaling with crop size
            "face_reid":       0.45 if face_detected else 0.0,
            "color_hist_reid": 0.15,                     # always on; low discriminativity
        }
    elif model_key == "idselect_style":
        area_score = min(1.0, crop_area / 30000.0)
        return {
            "osnet_ain":            0.3 + 0.7 * area_score,  # degrades for small crops
            "strongsort_reid":      0.8,                     # consistent baseline
            "openvino_reid_retail": 0.8,                     # consistent baseline
        }
    # Fallback: equal weights
    return {k: 1.0 for k in sub_keys}


def run_scenario_geff(model_key: str, scenario: str, config: dict[str, Any], display: bool) -> dict[str, Any]:
    """GEFF-style run: appearance always used; face enriches when detected."""
    from .fusion import GEFFGallery, ScoreFusionReIDModel

    spec = MODEL_REGISTRY[model_key]
    reid = ScoreFusionReIDModel(model_key, spec.sub_keys)
    loaded, load_detail = reid.load()
    if not loaded:
        return {"model": model_key, "scenario": scenario, "run_status": "skipped", "reason": load_detail}

    bench = config["benchmark"]
    detector = Detector(bench["detector"], bench["confidence"], bench["iou"], bench["person_class_id"])
    appear_threshold = CALIBRATED_THRESHOLDS.get("osnet_ain", bench["match_threshold"])
    matcher = GEFFGallery(appear_threshold, bench["max_gallery_age_seconds"])

    videos = config["videos"]
    if scenario == "single_delay":
        path_a, path_b = videos["ch9_5min"], videos["ch9_5min"]
        label_a, label_b = "ch9 live", f"ch9 +{bench['delay_seconds']}s"
    elif scenario == "cross_camera":
        path_a, path_b = videos["ch9_5min"], videos["ch10_5min"]
        label_a, label_b = "ch9", "ch10"
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    cap_a = cv2.VideoCapture(path_a)
    cap_b = cv2.VideoCapture(path_b)
    if not cap_a.isOpened() or not cap_b.isOpened():
        return {"model": model_key, "scenario": scenario, "run_status": "failed", "reason": "Could not open input videos."}

    fps = cap_a.get(cv2.CAP_PROP_FPS) or float(bench["output_fps"])
    if scenario == "single_delay":
        cap_b.set(cv2.CAP_PROP_POS_FRAMES, int(round(fps * bench["delay_seconds"])))

    state_a = StreamState(label_a, cap_a, IoUTracker(), fps=fps)
    state_b = StreamState(label_b, cap_b, IoUTracker(), fps=fps)

    model_output_dir = Path(config["paths"]["outputs_dir"]) / model_key
    model_report_dir = Path(config["paths"]["reports_dir"]) / model_key
    model_report_dir.mkdir(parents=True, exist_ok=True)
    timing_path = model_report_dir / f"{scenario}_timing.csv"
    events_path = model_report_dir / f"{scenario}_track_events.csv"
    output_path = model_output_dir / f"{scenario}.mp4"

    writer = None
    rows: list[dict] = []
    events: list[dict] = []
    frame_idx = 0
    process_every = max(1, int(bench.get("process_every_n_frames", 1)))
    max_frames = int(bench.get("max_frames", 0) or 0)
    started = time.perf_counter()

    while True:
        frame_start = time.perf_counter()
        frame_time_s = frame_idx / fps

        ok_a, frame_a = state_a.capture.read()
        ok_b, frame_b = state_b.capture.read()
        if not ok_a or not ok_b:
            break
        state_a.frame_idx += 1; state_b.frame_idx += 1
        state_a.current_frame = frame_a; state_b.current_frame = frame_b

        t0 = time.perf_counter()
        boxes_a = detector.detect(frame_a)
        boxes_b = detector.detect(frame_b)
        det_ms = (time.perf_counter() - t0) * 1000

        tracks_a = state_a.tracker.update(boxes_a, state_a.frame_idx)
        tracks_b = state_b.tracker.update(boxes_b, state_b.frame_idx)
        crops_a = crop_boxes(frame_a, [t.bbox for t in tracks_a])
        crops_b = crop_boxes(frame_b, [t.bbox for t in tracks_b])

        t0 = time.perf_counter()
        emb_a = reid.embed_multi(crops_a)
        emb_b = reid.embed_multi(crops_b)
        reid_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        for i, track in enumerate(tracks_a):
            appear = emb_a["osnet_ain"][i] if i < emb_a.get("osnet_ain", np.empty((0,))).shape[0] else np.zeros(512)
            face_raw = emb_a.get("face_reid", np.zeros((len(tracks_a), 512)))
            face_raw = face_raw[i] if i < face_raw.shape[0] else np.zeros(512)
            face_emb = face_raw if float(np.linalg.norm(face_raw)) > 0.5 else None
            gid, _ = matcher.match(appear, face_emb, frame_time_s)
            track.global_id = gid
        for i, track in enumerate(tracks_b):
            appear = emb_b["osnet_ain"][i] if i < emb_b.get("osnet_ain", np.empty((0,))).shape[0] else np.zeros(512)
            face_raw = emb_b.get("face_reid", np.zeros((len(tracks_b), 512)))
            face_raw = face_raw[i] if i < face_raw.shape[0] else np.zeros(512)
            face_emb = face_raw if float(np.linalg.norm(face_raw)) > 0.5 else None
            gid, _ = matcher.match(appear, face_emb, frame_time_s)
            track.global_id = gid
        match_ms = (time.perf_counter() - t0) * 1000

        state_a.current_tracks = tracks_a
        state_b.current_tracks = tracks_b

        left = draw_tracks(frame_a, tracks_a, label_a)
        right = draw_tracks(frame_b, tracks_b, label_b)
        combined = hstack_resize(left, right)
        cv2.putText(combined, f"{spec.name} | {scenario}",
                    (12, combined.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        if writer is None:
            model_output_dir.mkdir(parents=True, exist_ok=True)
            writer = open_writer(output_path, combined, float(bench["output_fps"]))
        writer.write(combined)

        if display:
            cv2.imshow(f"Re-ID {model_key} {scenario}", combined)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        total_ms = (time.perf_counter() - frame_start) * 1000
        rows.append({
            "frame": frame_idx, "scenario": scenario, "model": model_key,
            "detector_ms": det_ms, "reid_ms": reid_ms, "matching_ms": match_ms,
            "total_ms": total_ms, "live_fps": 1000.0 / total_ms if total_ms > 0 else 0.0,
            "detections": len(boxes_a) + len(boxes_b),
            "tracks": len(tracks_a) + len(tracks_b),
            "gallery_size": len(matcher.gallery),
        })
        for cam_label, state in ((label_a, state_a), (label_b, state_b)):
            for track in state.current_tracks:
                x1, y1, x2, y2 = track.bbox.astype(float)
                events.append({
                    "frame": frame_idx, "scenario": scenario, "model": model_key,
                    "camera": cam_label, "local_id": track.local_id,
                    "global_id": track.global_id,
                    "x1": round(x1, 2), "y1": round(y1, 2),
                    "x2": round(x2, 2), "y2": round(y2, 2),
                })
        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"{model_key}/{scenario}: {frame_idx} sampled frames", flush=True)
        if max_frames and frame_idx >= max_frames:
            break
        if process_every > 1:
            for _ in range(process_every - 1):
                grabbed_a = state_a.capture.grab()
                grabbed_b = state_b.capture.grab()
                if grabbed_a: state_a.frame_idx += 1
                if grabbed_b: state_b.frame_idx += 1
                if not grabbed_a or not grabbed_b:
                    break

    if writer is not None:
        writer.release()
    cap_a.release()
    cap_b.release()
    if display:
        cv2.destroyAllWindows()

    if rows:
        with timing_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    if events:
        with events_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(events[0].keys()))
            w.writeheader(); w.writerows(events)

    elapsed = time.perf_counter() - started
    summary = {
        "model": model_key, "model_name": spec.name, "backend": reid.backend,
        "scenario": scenario,
        "run_status": "passed" if rows else "failed",
        "frames": len(rows), "elapsed_seconds": round(elapsed, 3),
        "avg_live_fps": round(float(np.mean([r["live_fps"] for r in rows])) if rows else 0.0, 3),
        "avg_detector_ms": round(float(np.mean([r["detector_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_reid_ms": round(float(np.mean([r["reid_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_total_ms": round(float(np.mean([r["total_ms"] for r in rows])) if rows else 0.0, 3),
        "output_video": str(output_path), "timing_csv": str(timing_path),
        "track_events_csv": str(events_path),
        "accuracy_note": "Ground-truth accuracy requires annotations/annotations.csv; this run reports timing and visual/proxy ID consistency only.",
    }
    (model_report_dir / f"{scenario}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_scenario_quality_fusion(model_key: str, scenario: str, config: dict[str, Any], display: bool) -> dict[str, Any]:
    """Quality-weighted fusion run (FarSight-style and IDSelect-style)."""
    from .fusion import QualityFusionGallery, ScoreFusionReIDModel

    spec = MODEL_REGISTRY[model_key]
    reid = ScoreFusionReIDModel(model_key, spec.sub_keys)
    loaded, load_detail = reid.load()
    if not loaded:
        return {"model": model_key, "scenario": scenario, "run_status": "skipped", "reason": load_detail}

    bench = config["benchmark"]
    detector = Detector(bench["detector"], bench["confidence"], bench["iou"], bench["person_class_id"])
    appear_key = spec.sub_keys[0]
    base_threshold = CALIBRATED_THRESHOLDS.get(appear_key, bench["match_threshold"])
    matcher = QualityFusionGallery(spec.sub_keys, base_threshold, bench["max_gallery_age_seconds"])

    videos = config["videos"]
    if scenario == "single_delay":
        path_a, path_b = videos["ch9_5min"], videos["ch9_5min"]
        label_a, label_b = "ch9 live", f"ch9 +{bench['delay_seconds']}s"
    elif scenario == "cross_camera":
        path_a, path_b = videos["ch9_5min"], videos["ch10_5min"]
        label_a, label_b = "ch9", "ch10"
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    cap_a = cv2.VideoCapture(path_a)
    cap_b = cv2.VideoCapture(path_b)
    if not cap_a.isOpened() or not cap_b.isOpened():
        return {"model": model_key, "scenario": scenario, "run_status": "failed", "reason": "Could not open input videos."}

    fps = cap_a.get(cv2.CAP_PROP_FPS) or float(bench["output_fps"])
    if scenario == "single_delay":
        cap_b.set(cv2.CAP_PROP_POS_FRAMES, int(round(fps * bench["delay_seconds"])))

    state_a = StreamState(label_a, cap_a, IoUTracker(), fps=fps)
    state_b = StreamState(label_b, cap_b, IoUTracker(), fps=fps)

    model_output_dir = Path(config["paths"]["outputs_dir"]) / model_key
    model_report_dir = Path(config["paths"]["reports_dir"]) / model_key
    model_report_dir.mkdir(parents=True, exist_ok=True)
    timing_path = model_report_dir / f"{scenario}_timing.csv"
    events_path = model_report_dir / f"{scenario}_track_events.csv"
    output_path = model_output_dir / f"{scenario}.mp4"

    writer = None
    rows: list[dict] = []
    events: list[dict] = []
    frame_idx = 0
    process_every = max(1, int(bench.get("process_every_n_frames", 1)))
    max_frames = int(bench.get("max_frames", 0) or 0)
    started = time.perf_counter()

    while True:
        frame_start = time.perf_counter()
        frame_time_s = frame_idx / fps

        ok_a, frame_a = state_a.capture.read()
        ok_b, frame_b = state_b.capture.read()
        if not ok_a or not ok_b:
            break
        state_a.frame_idx += 1; state_b.frame_idx += 1
        state_a.current_frame = frame_a; state_b.current_frame = frame_b

        t0 = time.perf_counter()
        boxes_a = detector.detect(frame_a)
        boxes_b = detector.detect(frame_b)
        det_ms = (time.perf_counter() - t0) * 1000

        tracks_a = state_a.tracker.update(boxes_a, state_a.frame_idx)
        tracks_b = state_b.tracker.update(boxes_b, state_b.frame_idx)
        crops_a = crop_boxes(frame_a, [t.bbox for t in tracks_a])
        crops_b = crop_boxes(frame_b, [t.bbox for t in tracks_b])

        t0 = time.perf_counter()
        emb_a = reid.embed_multi(crops_a)
        emb_b = reid.embed_multi(crops_b)
        reid_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        for i, (track, crop) in enumerate(zip(tracks_a, crops_a)):
            per_track = {k: emb_a[k][i] for k in spec.sub_keys if emb_a.get(k) is not None and i < emb_a[k].shape[0]}
            per_track = smooth_track_embeddings_dict(track, per_track)
            area = float(crop.shape[0] * crop.shape[1])
            q_weights = _quality_weights_for(model_key, spec.sub_keys, per_track, area)
            gid, _ = matcher.match(per_track, q_weights, frame_time_s)
            track.global_id = gid
        for i, (track, crop) in enumerate(zip(tracks_b, crops_b)):
            per_track = {k: emb_b[k][i] for k in spec.sub_keys if emb_b.get(k) is not None and i < emb_b[k].shape[0]}
            per_track = smooth_track_embeddings_dict(track, per_track)
            area = float(crop.shape[0] * crop.shape[1])
            q_weights = _quality_weights_for(model_key, spec.sub_keys, per_track, area)
            gid, _ = matcher.match(per_track, q_weights, frame_time_s)
            track.global_id = gid
        match_ms = (time.perf_counter() - t0) * 1000

        state_a.current_tracks = tracks_a
        state_b.current_tracks = tracks_b

        left = draw_tracks(frame_a, tracks_a, label_a)
        right = draw_tracks(frame_b, tracks_b, label_b)
        combined = hstack_resize(left, right)
        cv2.putText(combined, f"{spec.name} | {scenario}",
                    (12, combined.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        if writer is None:
            model_output_dir.mkdir(parents=True, exist_ok=True)
            writer = open_writer(output_path, combined, float(bench["output_fps"]))
        writer.write(combined)

        if display:
            cv2.imshow(f"Re-ID {model_key} {scenario}", combined)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        total_ms = (time.perf_counter() - frame_start) * 1000
        rows.append({
            "frame": frame_idx, "scenario": scenario, "model": model_key,
            "detector_ms": det_ms, "reid_ms": reid_ms, "matching_ms": match_ms,
            "total_ms": total_ms, "live_fps": 1000.0 / total_ms if total_ms > 0 else 0.0,
            "detections": len(boxes_a) + len(boxes_b),
            "tracks": len(tracks_a) + len(tracks_b),
            "gallery_size": len(matcher.gallery),
        })
        for cam_label, state in ((label_a, state_a), (label_b, state_b)):
            for track in state.current_tracks:
                x1, y1, x2, y2 = track.bbox.astype(float)
                events.append({
                    "frame": frame_idx, "scenario": scenario, "model": model_key,
                    "camera": cam_label, "local_id": track.local_id,
                    "global_id": track.global_id,
                    "x1": round(x1, 2), "y1": round(y1, 2),
                    "x2": round(x2, 2), "y2": round(y2, 2),
                })
        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"{model_key}/{scenario}: {frame_idx} sampled frames", flush=True)
        if max_frames and frame_idx >= max_frames:
            break
        if process_every > 1:
            for _ in range(process_every - 1):
                grabbed_a = state_a.capture.grab()
                grabbed_b = state_b.capture.grab()
                if grabbed_a: state_a.frame_idx += 1
                if grabbed_b: state_b.frame_idx += 1
                if not grabbed_a or not grabbed_b:
                    break

    if writer is not None:
        writer.release()
    cap_a.release()
    cap_b.release()
    if display:
        cv2.destroyAllWindows()

    if rows:
        with timing_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    if events:
        with events_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(events[0].keys()))
            w.writeheader(); w.writerows(events)

    elapsed = time.perf_counter() - started
    summary = {
        "model": model_key, "model_name": spec.name, "backend": reid.backend,
        "scenario": scenario,
        "run_status": "passed" if rows else "failed",
        "frames": len(rows), "elapsed_seconds": round(elapsed, 3),
        "avg_live_fps": round(float(np.mean([r["live_fps"] for r in rows])) if rows else 0.0, 3),
        "avg_detector_ms": round(float(np.mean([r["detector_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_reid_ms": round(float(np.mean([r["reid_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_total_ms": round(float(np.mean([r["total_ms"] for r in rows])) if rows else 0.0, 3),
        "output_video": str(output_path), "timing_csv": str(timing_path),
        "track_events_csv": str(events_path),
        "accuracy_note": "Ground-truth accuracy requires annotations/annotations.csv; this run reports timing and visual/proxy ID consistency only.",
    }
    (model_report_dir / f"{scenario}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_scenario_bev_cross_camera(model_key: str, config: dict[str, Any], display: bool) -> dict[str, Any]:
    """Cross-camera scenario using BEV homography for same-timestamp person matching.

    At each frame:
      1. Detect + track persons in ch9 and ch10 independently.
      2. Project ch9 foot-points through the calibrated homography H → ch10 space.
      3. Hungarian-match projected foot-points to ch10 foot-points (distance < threshold).
      4. BEV-matched pairs → assigned the SAME global_id (geometry wins, no embedding needed).
      5. Unmatched persons → fall back to appearance-based gallery matching.
      6. Embeddings are still computed for gallery updates so cross-TIME matching works.
    """
    from .bev_matcher import BEVMatcher

    calib_path = Path("configs/bev_calibration.json")
    if not calib_path.exists():
        return {
            "model": model_key,
            "scenario": "cross_camera",
            "run_status": "skipped",
            "reason": f"BEV calibration not found at {calib_path}. Run: python -m reid_benchmark.calibrate_bev",
        }

    bev = BEVMatcher.from_calibration(calib_path)
    spec = MODEL_REGISTRY[model_key]

    # BEV models wrap a base appearance model; strip the "bev_" prefix to get it
    base_key = model_key[len("bev_"):] if model_key.startswith("bev_") else model_key

    # For score-fusion BEV variants, load via ScoreFusionReIDModel
    if spec.sub_keys:
        from .fusion import ScoreFusionReIDModel
        reid = ScoreFusionReIDModel(base_key, spec.sub_keys)
    else:
        reid = ReIDModel(base_key)

    loaded, load_detail = reid.load()
    if not loaded:
        return {"model": model_key, "scenario": "cross_camera", "run_status": "skipped", "reason": load_detail}

    bench = config["benchmark"]
    detector = Detector(bench["detector"], bench["confidence"], bench["iou"], bench["person_class_id"])
    match_threshold = CALIBRATED_THRESHOLDS.get(model_key, bench["match_threshold"])
    matcher = GlobalMatcher(match_threshold, bench["max_gallery_age_seconds"])

    videos = config["videos"]
    cap_a = cv2.VideoCapture(videos["ch9_5min"])
    cap_b = cv2.VideoCapture(videos["ch10_5min"])
    if not cap_a.isOpened() or not cap_b.isOpened():
        return {"model": model_key, "scenario": "cross_camera", "run_status": "failed", "reason": "Could not open videos."}

    fps = cap_a.get(cv2.CAP_PROP_FPS) or float(bench["output_fps"])
    tracker_a = IoUTracker()
    tracker_b = IoUTracker()

    model_output_dir = Path(config["paths"]["outputs_dir"]) / model_key
    model_report_dir = Path(config["paths"]["reports_dir"]) / model_key
    model_report_dir.mkdir(parents=True, exist_ok=True)
    timing_path = model_report_dir / "cross_camera_bev_timing.csv"
    events_path = model_report_dir / "cross_camera_bev_track_events.csv"
    output_path = model_output_dir / "cross_camera_bev.mp4"

    writer = None
    rows: list[dict] = []
    events: list[dict] = []
    frame_idx = 0
    process_every = max(1, int(bench.get("process_every_n_frames", 1)))
    max_frames = int(bench.get("max_frames", 0) or 0)
    started = time.perf_counter()
    raw_frame_a = 0
    raw_frame_b = 0

    label_a, label_b = "ch9", "ch10"

    while True:
        frame_start = time.perf_counter()
        frame_time = frame_idx / fps

        t0 = time.perf_counter()
        ok_a, frame_a = cap_a.read()
        ok_b, frame_b = cap_b.read()
        read_ms = (time.perf_counter() - t0) * 1000
        if not ok_a or not ok_b:
            break
        raw_frame_a += 1
        raw_frame_b += 1

        # --- Detect in both cameras ---
        t0 = time.perf_counter()
        boxes_a = detector.detect(frame_a)
        boxes_b = detector.detect(frame_b)
        det_ms = (time.perf_counter() - t0) * 1000

        # --- Track in both cameras (local track IDs, no global yet) ---
        t0 = time.perf_counter()
        tracks_a = tracker_a.update(boxes_a, frame_idx)
        tracks_b = tracker_b.update(boxes_b, frame_idx)
        tracker_ms = (time.perf_counter() - t0) * 1000

        # --- Compute embeddings for all tracks ---
        t0 = time.perf_counter()
        crops_a = crop_boxes(frame_a, [t.bbox for t in tracks_a])
        crops_b = crop_boxes(frame_b, [t.bbox for t in tracks_b])

        def _get_embedding(crops: list) -> np.ndarray:
            if not crops:
                return np.empty((0, 1), dtype=np.float32)
            if hasattr(reid, "embed_multi"):
                multi = reid.embed_multi(crops)  # dict[str, np.ndarray] each shape (N, D_k)
                # Sub-models have different embedding dims — use the first valid one
                # (BEV geometric matching is primary; gallery is fallback only)
                for embs in multi.values():
                    if embs.shape[0] == len(crops) and embs.shape[1] > 1:
                        return np.vstack([l2_normalize(embs[i]) for i in range(len(crops))])
                return np.empty((0, 1), dtype=np.float32)
            return reid.embed(crops)

        embs_a = _get_embedding(crops_a)
        embs_b = _get_embedding(crops_b)
        reid_ms = (time.perf_counter() - t0) * 1000

        # --- BEV matching: project ch9 feet → ch10 space, Hungarian assign ---
        t0 = time.perf_counter()
        bev_pairs = bev.match(tracks_a, tracks_b)  # [(idx_a, idx_b), ...]
        matched_a: set[int] = set()
        matched_b: set[int] = set()

        for idx_a, idx_b in bev_pairs:
            emb = embs_a[idx_a] if len(embs_a) > idx_a else embs_b[idx_b]
            gid, _ = matcher.match(emb, frame_time)
            tracks_a[idx_a].global_id = gid
            tracks_b[idx_b].global_id = gid          # geometry-confirmed same person
            # Enrich gallery with ch10 embedding as well (second view of same person)
            if len(embs_b) > idx_b:
                entry = matcher.gallery.get(gid)
                if entry is not None:
                    alpha = 0.85
                    entry.embedding = l2_normalize(
                        alpha * entry.embedding + (1 - alpha) * embs_b[idx_b]
                    )
            matched_a.add(idx_a)
            matched_b.add(idx_b)

        # Unmatched tracks fall back to standard appearance matching
        for i, track in enumerate(tracks_a):
            if i not in matched_a and len(embs_a) > i:
                match_emb = smooth_track_embedding(track, embs_a[i])
                gid, _ = matcher.match(match_emb, frame_time)
                track.global_id = gid

        for j, track in enumerate(tracks_b):
            if j not in matched_b and len(embs_b) > j:
                match_emb = smooth_track_embedding(track, embs_b[j])
                gid, _ = matcher.match(match_emb, frame_time)
                track.global_id = gid

        match_ms = (time.perf_counter() - t0) * 1000

        # --- Draw + write video ---
        t0 = time.perf_counter()
        vis_a = frame_a.copy()
        vis_b = frame_b.copy()

        # Draw BEV-matched pairs differently (yellow) vs appearance-only (normal colors)
        bev_matched_gids = {tracks_a[ia].global_id for ia, _ in bev_pairs}

        def draw_bev(frame_img, tracks, bev_gids, cam_label):
            for tr in tracks:
                x1, y1, x2, y2 = tr.bbox.astype(int)
                gid = tr.global_id if tr.global_id is not None else -1
                is_bev = gid in bev_gids
                color = (0, 220, 255) if is_bev else ((gid * 37) % 255, (gid * 17) % 255, (gid * 97) % 255)
                cv2.rectangle(frame_img, (x1, y1), (x2, y2), color, 2)
                tag = f"G{gid}{'[BEV]' if is_bev else ''}"
                cv2.putText(frame_img, tag, (x1, max(20, y1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
            cv2.putText(frame_img, cam_label, (12, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 220, 255), 2)
            return frame_img

        vis_a = draw_bev(vis_a, tracks_a, bev_matched_gids, label_a)
        vis_b = draw_bev(vis_b, tracks_b, bev_matched_gids, label_b)
        combined = hstack_resize(vis_a, vis_b)
        bev_count = len(bev_pairs)
        cv2.putText(combined, f"{spec.name} | BEV cross_camera | BEV pairs this frame: {bev_count}",
                    (12, combined.shape[0] - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        render_ms = (time.perf_counter() - t0) * 1000

        if writer is None:
            writer = open_writer(output_path, combined, float(bench["output_fps"]))
        writer.write(combined)
        if display:
            cv2.imshow(f"BEV Re-ID {model_key}", combined)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        total_ms = (time.perf_counter() - frame_start) * 1000
        rows.append({
            "frame": frame_idx, "scenario": "cross_camera_bev", "model": model_key,
            "read_ms": read_ms, "detector_ms": det_ms, "tracker_ms": tracker_ms,
            "reid_ms": reid_ms, "matching_ms": match_ms, "render_ms": render_ms,
            "total_ms": total_ms, "live_fps": 1000.0 / total_ms if total_ms > 0 else 0.0,
            "detections": len(boxes_a) + len(boxes_b),
            "tracks": len(tracks_a) + len(tracks_b),
            "bev_pairs": bev_count,
            "gallery_size": len(matcher.gallery),
        })

        for cam_label, these_tracks in ((label_a, tracks_a), (label_b, tracks_b)):
            for tr in these_tracks:
                x1, y1, x2, y2 = tr.bbox.astype(float)
                events.append({
                    "frame": frame_idx, "scenario": "cross_camera_bev", "model": model_key,
                    "camera": cam_label, "local_id": tr.local_id, "global_id": tr.global_id,
                    "bev_matched": int(tr.global_id in bev_matched_gids),
                    "x1": round(x1, 2), "y1": round(y1, 2),
                    "x2": round(x2, 2), "y2": round(y2, 2),
                })

        frame_idx += 1
        if frame_idx % 100 == 0:
            bev_rate = sum(r["bev_pairs"] for r in rows) / len(rows)
            print(f"{model_key}/cross_camera_bev: {frame_idx} frames | avg BEV pairs/frame: {bev_rate:.1f}", flush=True)
        if max_frames and frame_idx >= max_frames:
            break
        if process_every > 1:
            for _ in range(process_every - 1):
                ga = cap_a.grab()
                gb = cap_b.grab()
                if ga:
                    raw_frame_a += 1
                if gb:
                    raw_frame_b += 1
                if not ga or not gb:
                    break

    if writer is not None:
        writer.release()
    cap_a.release()
    cap_b.release()
    if display:
        cv2.destroyAllWindows()

    if rows:
        with timing_path.open("w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    if events:
        with events_path.open("w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=list(events[0].keys()))
            w.writeheader()
            w.writerows(events)

    elapsed = time.perf_counter() - started
    avg_bev = float(np.mean([r["bev_pairs"] for r in rows])) if rows else 0.0
    summary = {
        "model": model_key,
        "model_name": spec.name + " [BEV]",
        "backend": reid.backend + "+bev_homography",
        "scenario": "cross_camera_bev",
        "run_status": "passed" if rows else "failed",
        "frames": len(rows),
        "elapsed_seconds": round(elapsed, 3),
        "avg_live_fps": round(float(np.mean([r["live_fps"] for r in rows])) if rows else 0.0, 3),
        "avg_detector_ms": round(float(np.mean([r["detector_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_reid_ms": round(float(np.mean([r["reid_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_total_ms": round(float(np.mean([r["total_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_bev_pairs_per_frame": round(avg_bev, 2),
        "output_video": str(output_path),
        "timing_csv": str(timing_path),
        "track_events_csv": str(events_path),
        "accuracy_note": "BEV-matched persons share global_id by geometry. [BEV] tag in video = geometry-confirmed same person.",
    }
    (model_report_dir / "cross_camera_bev_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_scenario(model_key: str, scenario: str, config: dict[str, Any], display: bool) -> dict[str, Any]:
    spec = MODEL_REGISTRY[model_key]
    if spec.loader == "bev":
        return run_scenario_bev_cross_camera(model_key, config, display)
    if spec.loader == "score_fusion":
        return run_scenario_score_fusion(model_key, scenario, config, display)
    if spec.loader == "geff":
        return run_scenario_geff(model_key, scenario, config, display)
    if spec.loader in ("farsight", "idselect"):
        return run_scenario_quality_fusion(model_key, scenario, config, display)
    reid = ReIDModel(model_key)
    loaded, load_detail = reid.load()
    if not loaded:
        return {
            "model": model_key,
            "scenario": scenario,
            "run_status": "skipped",
            "reason": load_detail,
        }

    bench = config["benchmark"]
    detector = Detector(bench["detector"], bench["confidence"], bench["iou"], bench["person_class_id"])
    # Per-model calibrated cosine-distance thresholds (1 - midpoint of same-person
    # vs different-person similarity, measured in the verification eval). Each model
    # produces embeddings on a different similarity scale, so a single fixed
    # threshold mis-fits most of them (e.g. CLIP-ReID merges everyone at 0.35).
    match_threshold = CALIBRATED_THRESHOLDS.get(model_key, bench["match_threshold"])
    matcher = GlobalMatcher(match_threshold, bench["max_gallery_age_seconds"])

    videos = config["videos"]
    if scenario == "single_delay":
        path_a = videos["ch9_5min"]
        path_b = videos["ch9_5min"]
        label_a = "ch9 live"
        label_b = f"ch9 +{bench['delay_seconds']}s"
    elif scenario == "cross_camera":
        path_a = videos["ch9_5min"]
        path_b = videos["ch10_5min"]
        label_a = "ch9"
        label_b = "ch10"
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    cap_a = cv2.VideoCapture(path_a)
    cap_b = cv2.VideoCapture(path_b)
    if not cap_a.isOpened() or not cap_b.isOpened():
        return {"model": model_key, "scenario": scenario, "run_status": "failed", "reason": "Could not open input videos."}

    fps = cap_a.get(cv2.CAP_PROP_FPS) or float(bench["output_fps"])
    if scenario == "single_delay":
        cap_b.set(cv2.CAP_PROP_POS_FRAMES, int(round(fps * bench["delay_seconds"])))

    state_a = StreamState(label_a, cap_a, IoUTracker(), fps=fps)
    state_b = StreamState(label_b, cap_b, IoUTracker(), fps=fps)

    model_output_dir = Path(config["paths"]["outputs_dir"]) / model_key
    model_report_dir = Path(config["paths"]["reports_dir"]) / model_key
    model_report_dir.mkdir(parents=True, exist_ok=True)
    timing_path = model_report_dir / f"{scenario}_timing.csv"
    events_path = model_report_dir / f"{scenario}_track_events.csv"
    output_path = model_output_dir / f"{scenario}.mp4"

    writer = None
    rows = []
    events = []
    frame_idx = 0
    process_every = max(1, int(bench.get("process_every_n_frames", 1)))
    max_frames = int(bench.get("max_frames", 0) or 0)
    started = time.perf_counter()
    while True:
        frame_start = time.perf_counter()
        frame_time_seconds = frame_idx / fps
        ok_a, timing_a = process_stream(state_a, detector, reid, matcher, frame_time_seconds)
        ok_b, timing_b = process_stream(state_b, detector, reid, matcher, frame_time_seconds)
        if not ok_a or not ok_b:
            break

        t0 = time.perf_counter()
        left = draw_tracks(state_a.current_frame, state_a.current_tracks, state_a.label)
        right = draw_tracks(state_b.current_frame, state_b.current_tracks, state_b.label)
        combined = hstack_resize(left, right)
        cv2.putText(
            combined,
            f"{spec.name} | {scenario}",
            (12, combined.shape[0] - 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
        )
        render_ms = (time.perf_counter() - t0) * 1000
        if writer is None:
            writer = open_writer(output_path, combined, float(bench["output_fps"]))
        writer.write(combined)
        if display:
            cv2.imshow(f"Re-ID {model_key} {scenario}", combined)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        total_ms = (time.perf_counter() - frame_start) * 1000
        rows.append(
            {
                "frame": frame_idx,
                "scenario": scenario,
                "model": model_key,
                "read_ms": timing_a.get("read_ms", 0) + timing_b.get("read_ms", 0),
                "detector_ms": timing_a.get("detector_ms", 0) + timing_b.get("detector_ms", 0),
                "tracker_ms": timing_a.get("tracker_ms", 0) + timing_b.get("tracker_ms", 0),
                "crop_ms": timing_a.get("crop_ms", 0) + timing_b.get("crop_ms", 0),
                "reid_ms": timing_a.get("reid_ms", 0) + timing_b.get("reid_ms", 0),
                "matching_ms": timing_a.get("matching_ms", 0) + timing_b.get("matching_ms", 0),
                "render_ms": render_ms,
                "total_ms": total_ms,
                "live_fps": 1000.0 / total_ms if total_ms > 0 else 0.0,
                "detections": timing_a.get("detections", 0) + timing_b.get("detections", 0),
                "tracks": timing_a.get("tracks", 0) + timing_b.get("tracks", 0),
                "gallery_size": len(matcher.gallery),
            }
        )
        for camera_label, state in ((label_a, state_a), (label_b, state_b)):
            for track in state.current_tracks:
                x1, y1, x2, y2 = track.bbox.astype(float)
                events.append(
                    {
                        "frame": frame_idx,
                        "scenario": scenario,
                        "model": model_key,
                        "camera": camera_label,
                        "local_id": track.local_id,
                        "global_id": track.global_id,
                        "x1": round(x1, 2),
                        "y1": round(y1, 2),
                        "x2": round(x2, 2),
                        "y2": round(y2, 2),
                    }
                )
        frame_idx += 1
        if frame_idx % 100 == 0:
            print(
                f"{model_key}/{scenario}: processed {frame_idx} sampled frames "
                f"(input frame ~{state_a.frame_idx})",
                flush=True,
            )
        if max_frames and frame_idx >= max_frames:
            break
        if process_every > 1:
            for _ in range(process_every - 1):
                grabbed_a = state_a.capture.grab()
                grabbed_b = state_b.capture.grab()
                if grabbed_a:
                    state_a.frame_idx += 1
                if grabbed_b:
                    state_b.frame_idx += 1
                if not grabbed_a or not grabbed_b:
                    break

    if writer is not None:
        writer.release()
    cap_a.release()
    cap_b.release()
    if display:
        cv2.destroyAllWindows()

    if rows:
        with timing_path.open("w", encoding="utf-8", newline="") as handle:
            writer_csv = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer_csv.writeheader()
            writer_csv.writerows(rows)
    if events:
        with events_path.open("w", encoding="utf-8", newline="") as handle:
            writer_csv = csv.DictWriter(handle, fieldnames=list(events[0].keys()))
            writer_csv.writeheader()
            writer_csv.writerows(events)

    elapsed = time.perf_counter() - started
    summary = {
        "model": model_key,
        "model_name": spec.name,
        "backend": reid.backend,
        "scenario": scenario,
        "run_status": "passed" if rows else "failed",
        "frames": len(rows),
        "elapsed_seconds": round(elapsed, 3),
        "avg_live_fps": round(float(np.mean([r["live_fps"] for r in rows])) if rows else 0.0, 3),
        "avg_detector_ms": round(float(np.mean([r["detector_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_reid_ms": round(float(np.mean([r["reid_ms"] for r in rows])) if rows else 0.0, 3),
        "avg_total_ms": round(float(np.mean([r["total_ms"] for r in rows])) if rows else 0.0, 3),
        "output_video": str(output_path),
        "timing_csv": str(timing_path),
        "track_events_csv": str(events_path),
        "accuracy_note": "Ground-truth accuracy requires annotations/annotations.csv; this run reports timing and visual/proxy ID consistency only.",
    }
    (model_report_dir / f"{scenario}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/benchmark.yaml")
    parser.add_argument("--models", nargs="*", help="Model keys. Defaults to config order.")
    parser.add_argument("--scenario", choices=["single_delay", "cross_camera", "both"], default="both")
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument("--max-frames", type=int, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    ensure_dirs(config)
    if args.max_frames is not None:
        config["benchmark"]["max_frames"] = args.max_frames

    model_keys = args.models or config["models"]
    scenarios = ["single_delay", "cross_camera"] if args.scenario == "both" else [args.scenario]
    summaries = []
    for spec in selected_models(model_keys):
        for scenario in scenarios:
            print(f"Running {spec.key} / {scenario} ...", flush=True)
            summaries.append(run_scenario(spec.key, scenario, config, display=bool(config["benchmark"]["display"]) and not args.no_display))

    report_path = Path(config["paths"]["reports_dir"]) / "benchmark_summary.json"
    report_path.write_text(json.dumps({"runs": summaries}, indent=2), encoding="utf-8")
    print(json.dumps({"runs": summaries}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
