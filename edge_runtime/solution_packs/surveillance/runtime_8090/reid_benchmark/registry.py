from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ModelSpec:
    key: str
    name: str
    priority: int
    target: str
    loader: str
    repo_url: str | None = None
    weights_url: str | None = None
    notes: str = ""
    sub_keys: tuple[str, ...] = ()  # for fusion models


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "kpr": ModelSpec(
        "kpr",
        "KPR / Keypoint Promptable Re-ID",
        1,
        "person/occlusion",
        "kpr",
        repo_url="https://github.com/VlSomers/keypoint_promptable_reidentification",
        notes="Swin-V2 body-part Re-ID; image-only (disable_inference_prompting), 9 parts x 512 = 4608-D descriptor.",
    ),
    "bpbreid": ModelSpec(
        "bpbreid",
        "BPBreID",
        2,
        "person/occlusion",
        "bpbreid",
        repo_url="https://github.com/VlSomers/bpbreid",
        notes="Body-part baseline for occluded Re-ID. Uses official Occluded-Duke HRNet32 checkpoint.",
    ),
    "osnet_ain": ModelSpec(
        "osnet_ain",
        "OSNet-AIN x1.0",
        3,
        "person",
        "torchreid",
        notes="Primary practical cross-domain model. Auto-downloads through torchreid when torchreid is installed.",
    ),
    "osnet_x1_0": ModelSpec("osnet_x1_0", "OSNet x1.0", 4, "person", "torchreid"),
    "osnet_x0_75": ModelSpec("osnet_x0_75", "OSNet x0.75", 5, "person", "torchreid"),
    "osnet_x0_5": ModelSpec("osnet_x0_5", "OSNet x0.5", 6, "person", "torchreid"),
    "fastreid_sbs_bot_agw": ModelSpec(
        "fastreid_sbs_bot_agw",
        "FastReID SBS / BoT / AGW",
        7,
        "person",
        "fastreid",
        repo_url="https://github.com/JDAI-CV/fast-reid",
        notes="Uses MSMT17 SBS R50-IBN checkpoint for cross-camera/domain robustness.",
    ),
    "transreid": ModelSpec(
        "transreid",
        "TransReID",
        8,
        "person/vehicle",
        "transreid",
        repo_url="https://github.com/damo-cv/TransReID",
        notes="Transformer Re-ID. Uses official MSMT17 ViT TransReID stride checkpoint.",
    ),
    "pass_reid": ModelSpec(
        "pass_reid",
        "PASS-ReID / TransReID-SSL",
        9,
        "person",
        "pass_reid",
        repo_url="https://github.com/CASIA-IVA-Lab/PASS-reID",
        notes="ViT-B/16 part-aware self-supervised (LUPerson) fine-tuned on MSMT17; cls+3 part tokens, 1536-D descriptor.",
    ),
    "nvidia_tao_reid": ModelSpec(
        "nvidia_tao_reid",
        "NVIDIA TAO ReIdentificationNet",
        10,
        "person/object",
        "nvidia_tao",
        repo_url="https://catalog.ngc.nvidia.com/orgs/nvidia/teams/tao/models/reidentificationnet",
        notes="ResNet50 Market1501/AICity ONNX (deployable_v1.2), 256-D embedding; runs via onnxruntime.",
    ),
    "openvino_reid_retail": ModelSpec(
        "openvino_reid_retail",
        "OpenVINO person-reidentification-retail",
        11,
        "person",
        "openvino",
        notes="Good deployment target if OpenVINO model downloader is installed.",
    ),
    "botsort_reid": ModelSpec(
        "botsort_reid",
        "BoT-SORT Re-ID",
        12,
        "person",
        "fastreid",
        repo_url="https://github.com/NirAharon/BoT-SORT",
        notes="Uses BoT-SORT MOT17-SBS-S50 ReID checkpoint.",
    ),
    "strongsort_reid": ModelSpec(
        "strongsort_reid",
        "StrongSORT Re-ID",
        13,
        "person",
        "strongsort",
        repo_url="https://github.com/dyhBUPT/StrongSORT",
        notes="OSNet x0.25 MSMT17 appearance model (StrongSORT/BoxMOT default), 512-D descriptor.",
    ),
    "deepsort_mars": ModelSpec(
        "deepsort_mars",
        "DeepSORT MARS descriptor",
        14,
        "person",
        "deepsort",
        repo_url="https://github.com/nwojke/deep_sort",
        notes="ResNet-18-style appearance CNN (Market1501 ckpt.t7), 512-D pooled descriptor; speed baseline.",
    ),
    "fairmot": ModelSpec(
        "fairmot",
        "FairMOT",
        15,
        "person MOT",
        "fairmot",
        repo_url="https://github.com/ifzhang/FairMOT",
        notes="DLA34 joint MOT+ReID; id head sampled as 128-D crop embedding. DCNv2 shimmed via torchvision deform_conv2d.",
    ),
    "clip_reid": ModelSpec(
        "clip_reid",
        "CLIP-ReID",
        16,
        "person/vehicle",
        "clip_reid",
        repo_url="https://github.com/Syliz517/CLIP-ReID",
        notes="ViT-B-16 CLIP-ReID SIE+OLP MSMT17 checkpoint; stride 12 overlapping patches, 1280-D descriptor.",
    ),
    # ── Multi-modal & fusion models ──────────────────────────────────────────
    "face_reid": ModelSpec(
        "face_reid",
        "Face Re-ID (InsightFace ArcFace)",
        17,
        "person/face",
        "face",
        notes="SCRFD face detector + ArcFace buffalo_s embedding (512-D). Falls back to zero vector when no face found in crop.",
    ),
    "color_hist_reid": ModelSpec(
        "color_hist_reid",
        "Color Histogram Re-ID",
        18,
        "person",
        "color_hist",
        notes="HSV color histogram over 3 body bands (head/torso/legs), 16+8 bins per band = 72-D. No model weights needed.",
    ),
    "fusion_appear_face": ModelSpec(
        "fusion_appear_face",
        "Feature Fusion: Appearance + Face",
        19,
        "person",
        "feature_fusion",
        notes="Concatenates OSNet-AIN (1024-D) + ArcFace (512-D) = 1536-D, then L2-normalised.",
        sub_keys=("osnet_ain", "face_reid"),
    ),
    "fusion_appear_color": ModelSpec(
        "fusion_appear_color",
        "Feature Fusion: Appearance + Color Histogram",
        20,
        "person",
        "feature_fusion",
        notes="Concatenates OSNet-AIN (1024-D) + HSV color (72-D) = 1096-D, then L2-normalised.",
        sub_keys=("osnet_ain", "color_hist_reid"),
    ),
    "fusion_appear_face_color": ModelSpec(
        "fusion_appear_face_color",
        "Feature Fusion: Appearance + Face + Color",
        21,
        "person",
        "feature_fusion",
        notes="Concatenates OSNet-AIN (1024-D) + ArcFace (512-D) + HSV color (72-D) = 1608-D.",
        sub_keys=("osnet_ain", "face_reid", "color_hist_reid"),
    ),
    "score_fusion_top3": ModelSpec(
        "score_fusion_top3",
        "Score Fusion: OSNet-AIN + OpenVINO + StrongSORT",
        22,
        "person",
        "score_fusion",
        notes="Averages cosine distances from three independent appearance galleries. Threshold is averaged across sub-models.",
        sub_keys=("osnet_ain", "openvino_reid_retail", "strongsort_reid"),
    ),
    "score_fusion_fast": ModelSpec(
        "score_fusion_fast",
        "Score Fusion: DeepSORT + StrongSORT + OpenVINO (fast tier)",
        23,
        "person",
        "score_fusion",
        notes="Averages distances from three fast models (all >2.5 FPS individually). Best speed/accuracy trade-off for edge.",
        sub_keys=("deepsort_mars", "strongsort_reid", "openvino_reid_retail"),
    ),
    # ── Published fused-pipeline approximations ──────────────────────────────
    "geff_reid": ModelSpec(
        "geff_reid",
        "GEFF: Gallery-Enriched Appearance + Face",
        24,
        "person",
        "geff",
        notes="GEFF paper style: OSNet-AIN appearance always used; ArcFace face enriches gallery when detected. Face weight scales with detection reliability. +33% on clothes-changing Re-ID in original paper.",
        sub_keys=("osnet_ain", "face_reid"),
    ),
    "farsight_style": ModelSpec(
        "farsight_style",
        "FarSight-style: Appearance + Face + Color (quality-gated)",
        25,
        "person",
        "farsight",
        notes="3-modality quality-weighted fusion inspired by FarSight. Appearance weight scales with crop area; face weight drops to 0 when no face detected; color histogram always contributes at low weight.",
        sub_keys=("osnet_ain", "face_reid", "color_hist_reid"),
    ),
    # ── BEV (Bird's Eye View) Geometry-Guided models ──────────────────────────
    # These run ONLY on cross_camera (BEV requires 2 simultaneous cameras).
    # Persons visible in both cameras at the same timestamp are matched by
    # foot-point homography projection, not appearance. Appearance is still
    # used as fallback for persons visible in only one camera.
    "bev_osnet_ain": ModelSpec(
        "bev_osnet_ain",
        "BEV + OSNet-AIN (geometry-guided cross-camera)",
        27,
        "person",
        "bev",
        notes="OSNet-AIN appearance for gallery + BEV homography for simultaneous cross-camera matching. Requires configs/bev_calibration.json.",
    ),
    "bev_transreid": ModelSpec(
        "bev_transreid",
        "BEV + TransReID (geometry-guided cross-camera)",
        28,
        "person",
        "bev",
        notes="TransReID appearance for gallery + BEV homography for simultaneous cross-camera matching. Requires configs/bev_calibration.json.",
    ),
    "bev_idselect_style": ModelSpec(
        "bev_idselect_style",
        "BEV + IDSelect-style (geometry + multi-model fusion)",
        29,
        "person",
        "bev",
        notes="IDSelect-style adaptive fusion for appearance gallery + BEV for simultaneous matching. Requires configs/bev_calibration.json.",
        sub_keys=("osnet_ain", "strongsort_reid", "openvino_reid_retail"),
    ),
    "idselect_style": ModelSpec(
        "idselect_style",
        "IDSelect-style: Crop-Adaptive Model Selection",
        26,
        "person",
        "idselect",
        notes="Rule-based approximation of IDSelect RL selection. Crop area determines osnet_ain weight (degrades for small crops); strongsort and openvino remain as baseline contributors.",
        sub_keys=("osnet_ain", "strongsort_reid", "openvino_reid_retail"),
    ),
}


def selected_models(keys: list[str]) -> list[ModelSpec]:
    specs = [MODEL_REGISTRY[k] for k in keys if k in MODEL_REGISTRY]
    return sorted(specs, key=lambda item: item.priority)


def model_dir(models_root: Path, key: str) -> Path:
    return models_root / key
