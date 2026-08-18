"""Gait spike: can GaitBase (OpenGait, Gait3D-pretrained) separate identities
on this footage? Verification AUC over silhouette-sequence pairs.

Pipeline:
  --collect : YOLOv8n-seg on ch9+ch10 -> person masks -> IoU tracking ->
              OpenGait-standard 64x44 silhouettes per track (>= min_len frames)
  --eval    : GaitBase inference (single-process gloo patch for Windows),
              same-person pairs = first half vs second half of one track,
              diff-person pairs = tracks co-occurring in the same frame.

Result merges into MTMC/reports/stage2_gait_screen.json — this is the
empirical go/no-go the plan calls for.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _ROOT / "MTMC" / "cache" / "gait_sils"
_OUT = _ROOT / "MTMC" / "reports" / "stage2_gait_screen.json"
_CKPT = _ROOT / "models" / "opengait" / "GaitBase_DA-60000.pt"
_REPO = _ROOT / "repos" / "opengait"


# ------------------------------------------------------------------ pretreatment

def opengait_pretreat(mask: np.ndarray, target_h: int = 64, target_w: int = 44) -> np.ndarray | None:
    """OpenGait-standard silhouette normalization (pretreatment.py logic)."""
    if mask.max() == 0:
        return None
    ys = np.where(mask.any(axis=1))[0]
    if len(ys) < 10:
        return None
    mask = mask[ys[0]: ys[-1] + 1]
    h, w = mask.shape
    ratio = target_h / h
    mask = cv2.resize(mask, (max(1, int(w * ratio)), target_h), interpolation=cv2.INTER_NEAREST)
    # center by x center-of-mass, pad/cut to target_w
    xs = mask.sum(axis=0)
    if xs.sum() == 0:
        return None
    cx = int(round((xs * np.arange(len(xs))).sum() / xs.sum()))
    half = target_w // 2
    padded = np.zeros((target_h, len(xs) + 2 * target_w), dtype=mask.dtype)
    padded[:, target_w: target_w + len(xs)] = mask
    out = padded[:, cx + target_w - half: cx + target_w + half]
    if out.shape != (target_h, target_w):
        return None
    return (out > 0).astype(np.uint8) * 255


# ------------------------------------------------------------------ collect

def collect(min_len: int = 20, frame_step: int = 3, max_raw_frames: int = 3600) -> None:
    from ultralytics import YOLO
    from MTMC.pipelines import load_mtmc_config
    from MTMC.adapters import IoUTracker

    config = load_mtmc_config()
    seg = YOLO("yolov8n-seg.pt")

    track_sils: dict[str, list[np.ndarray]] = defaultdict(list)      # "cam_tid" -> [sil]
    cooccur: dict[str, set] = defaultdict(set)                        # frame key -> track keys

    for cam_key in ("ch9_5min", "ch10_5min"):
        cam = cam_key.split("_")[0]
        cap = cv2.VideoCapture(str(_ROOT / config["videos"][cam_key]))
        tracker = IoUTracker()
        raw = 0
        sampled = 0
        while raw < max_raw_frames:
            ok, frame = cap.read()
            if not ok:
                break
            raw += 1
            if raw % frame_step:
                continue
            sampled += 1
            res = seg.predict(frame, classes=[0], conf=0.4, verbose=False)[0]
            if res.masks is None:
                continue
            boxes = [b.xyxy[0].cpu().numpy() for b in res.boxes]
            tracks = tracker.update(boxes, sampled)
            # match masks to tracks by box order (same order as res.boxes)
            masks = res.masks.data.cpu().numpy()  # (n, mh, mw) at model scale
            mh, mw = masks.shape[1:]
            fh, fw = frame.shape[:2]
            for tr in tracks:
                # find the detection index whose box matches this track's bbox
                best_i, best_iou = -1, 0.0
                for i, b in enumerate(boxes):
                    x1 = max(b[0], tr.bbox[0]); y1 = max(b[1], tr.bbox[1])
                    x2 = min(b[2], tr.bbox[2]); y2 = min(b[3], tr.bbox[3])
                    inter = max(0, x2 - x1) * max(0, y2 - y1)
                    ua = ((b[2]-b[0])*(b[3]-b[1]) + (tr.bbox[2]-tr.bbox[0])*(tr.bbox[3]-tr.bbox[1]) - inter)
                    iou = inter / ua if ua > 0 else 0
                    if iou > best_iou:
                        best_iou, best_i = iou, i
                if best_i < 0 or best_iou < 0.5:
                    continue
                m = cv2.resize(masks[best_i], (fw, fh), interpolation=cv2.INTER_NEAREST)
                x1, y1, x2, y2 = tr.bbox.astype(int)
                sub = (m[max(0, y1):y2, max(0, x1):x2] > 0.5).astype(np.uint8) * 255
                sil = opengait_pretreat(sub)
                if sil is not None:
                    key = f"{cam}_{tr.local_id}"
                    track_sils[key].append(sil)
                    cooccur[f"{cam}_{sampled}"].add(key)
        cap.release()
        print(f"{cam}: {len([k for k in track_sils if k.startswith(cam)])} tracks so far", flush=True)

    keep = {k: v for k, v in track_sils.items() if len(v) >= min_len}
    _CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(_CACHE / "sequences.npz",
                        **{k: np.stack(v[:60]) for k, v in keep.items()})
    co_pairs = set()
    for keys in cooccur.values():
        ks = [k for k in keys if k in keep]
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                co_pairs.add(tuple(sorted((ks[i], ks[j]))))
    (_CACHE / "cooccur.json").write_text(json.dumps(sorted(co_pairs)), encoding="utf-8")
    print(f"kept {len(keep)} tracks (>= {min_len} sils) | {len(co_pairs)} co-occurring diff pairs")


# ------------------------------------------------------------------ eval

def _build_gaitbase():
    """Instantiate GaitBase (Baseline) outside OpenGait's training framework."""
    import torch
    import torch.distributed as dist
    import yaml

    os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
    if not dist.is_initialized():
        # pick a free ephemeral port so back-to-back runs never collide (a
        # killed run can leave the fixed port in TIME_WAIT)
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        os.environ["MASTER_PORT"] = str(s.getsockname()[1])
        s.close()
        dist.init_process_group("gloo", rank=0, world_size=1)

    sys.path.insert(0, str(_REPO / "opengait"))
    from utils import config_loader, get_msg_mgr

    msg_mgr = get_msg_mgr()
    save_path = _ROOT / "MTMC" / "cache" / "gait_log"
    save_path.mkdir(parents=True, exist_ok=True)
    msg_mgr.init_manager(str(save_path), False, 100, 0)

    cwd = os.getcwd()
    os.chdir(_REPO)  # config_loader reads ./configs/default.yaml relative to CWD
    try:
        cfgs = config_loader(str(Path("configs") / "gaitbase" / "gaitbase_da_gait3d.yaml"))
    finally:
        os.chdir(cwd)
    cfgs["evaluator_cfg"]["restore_hint"] = 0

    from modeling import models as og_models
    from modeling.base_model import BaseModel
    Baseline = getattr(og_models, "Baseline")

    # inference-only: we feed tensors directly, no dataset loader needed
    BaseModel.get_loader = lambda self, *a, **k: None

    model = Baseline(cfgs, training=False)
    ckpt = torch.load(_CKPT, map_location="cpu", weights_only=False)
    state = ckpt.get("model", ckpt)
    model.load_state_dict(state, strict=False)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.eval().to(device)
    return model, device


def _embed_sequence(model, device, sils: np.ndarray) -> np.ndarray:
    import torch
    s = sils.astype(np.float32) / 255.0            # (T, 64, 44)
    ipts = torch.from_numpy(s).unsqueeze(0).to(device)  # (1, T, H, W)
    seqL = None
    labs = torch.zeros(1, dtype=torch.long).to(device)
    with torch.no_grad():
        retval = model(([ipts], labs, None, None, seqL))
    feat = retval["inference_feat"]["embeddings"]  # (1, C, P)
    f = feat[0].cpu().numpy()
    f = f / (np.linalg.norm(f, axis=0, keepdims=True) + 1e-12)  # per-part L2
    return f  # (C, P)


def _pair_sim(fa: np.ndarray, fb: np.ndarray) -> float:
    return float(np.mean(np.sum(fa * fb, axis=0)))  # mean per-part cosine


def evaluate() -> dict:
    try:
        model, device = _build_gaitbase()
    except Exception as exc:  # noqa: BLE001
        result = {"model": "gaitbase_gait3d", "status": "skipped", "reason": str(exc)[:300]}
        _merge(result)
        print(json.dumps(result, indent=2))
        return result

    data = np.load(_CACHE / "sequences.npz")
    co_pairs = {tuple(p) for p in json.loads((_CACHE / "cooccur.json").read_text(encoding="utf-8"))}
    keys = list(data.keys())
    print(f"{len(keys)} tracks | embedding...", flush=True)

    feats_full = {k: _embed_sequence(model, device, data[k]) for k in keys}
    feats_h1 = {k: _embed_sequence(model, device, data[k][: len(data[k]) // 2]) for k in keys}
    feats_h2 = {k: _embed_sequence(model, device, data[k][len(data[k]) // 2:]) for k in keys}

    same_sims = [_pair_sim(feats_h1[k], feats_h2[k]) for k in keys]
    diff_sims = [_pair_sim(feats_full[a], feats_full[b]) for a, b in co_pairs
                 if a in feats_full and b in feats_full]

    if not same_sims or not diff_sims:
        result = {"model": "gaitbase_gait3d", "status": "skipped",
                  "reason": f"insufficient pairs (same={len(same_sims)}, diff={len(diff_sims)})"}
        _merge(result); print(json.dumps(result, indent=2)); return result

    same_arr, diff_arr = np.array(same_sims), np.array(diff_sims)
    labels = np.r_[np.ones_like(same_arr), np.zeros_like(diff_arr)]
    scores = np.r_[same_arr, diff_arr]
    order = np.argsort(scores)
    ranks = np.empty_like(order, dtype=float); ranks[order] = np.arange(1, len(scores) + 1)
    n_pos, n_neg = len(same_arr), len(diff_arr)
    auc = (ranks[labels == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)

    result = {
        "model": "gaitbase_gait3d", "status": "ok",
        "mean_same_sim": round(float(same_arr.mean()), 4),
        "mean_diff_sim": round(float(diff_arr.mean()), 4),
        "auc": round(float(auc), 4),
        "n_same": n_pos, "n_diff": n_neg, "n_tracks": len(keys),
    }
    _merge(result)
    print(json.dumps(result, indent=2))
    return result


def _merge(result: dict) -> None:
    merged = json.loads(_OUT.read_text(encoding="utf-8")) if _OUT.exists() else {}
    merged[result["model"]] = result
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--eval", action="store_true")
    args = ap.parse_args()
    if args.collect:
        collect()
        return 0
    if args.eval:
        r = evaluate()
        return 0 if r.get("status") == "ok" else 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
