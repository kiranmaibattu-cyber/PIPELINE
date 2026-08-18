"""Standalone PAR screen using full-body crops and PA-100K attributes.

Collects same/different full-body crop pairs like the calibration screen, then
uses a 26-dimensional PA-100K attribute vector as a soft-biometric embedding.
Results merge into MTMC/reports/stage2_par_screen.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _ROOT / "MTMC" / "cache" / "par_crops"
_OUT = _ROOT / "MTMC" / "reports" / "stage2_par_screen.json"
_REPO = _ROOT / "repos" / "Rethinking_of_PAR"
_CKPT_DIR = _ROOT / "models" / "par"


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def collect_pairs(
    n_frames: int = 120,
    frame_step: int = 25,
    min_gap_frames: int = 3,
    max_same_pairs: int = 300,
    max_diff_pairs: int = 300,
    min_crop_h: int = 80,
) -> None:
    from MTMC.calibrate_thresholds import collect_pairs as collect_body_pairs

    old_cache = __import__("MTMC.calibrate_thresholds", fromlist=["_CACHE"])
    old_cache._CACHE = _CACHE
    collect_body_pairs(n_frames, frame_step, min_gap_frames, max_same_pairs, max_diff_pairs, min_crop_h)


def _find_checkpoint() -> Path | None:
    for pat in ("*.pth", "*.pt", "*.ckpt"):
        found = sorted(_CKPT_DIR.glob(pat))
        if found:
            return found[0]
    return None


def _ensure_repo() -> None:
    if _REPO.exists():
        return
    _REPO.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "https://github.com/valencebond/Rethinking_of_PAR.git", str(_REPO)],
                   cwd=str(_ROOT), check=True)


def _ensure_checkpoint() -> Path:
    ckpt = _find_checkpoint()
    if ckpt is not None:
        return ckpt
    gid = os.environ.get("PAR_GDRIVE_ID", "").strip()
    if gid:
        import gdown

        _CKPT_DIR.mkdir(parents=True, exist_ok=True)
        out = _CKPT_DIR / "rethinking_par_pa100k_resnet50.pth"
        gdown.download(id=gid, output=str(out), quiet=False)
        if out.exists() and out.stat().st_size > 1_000_000:
            return out
    raise FileNotFoundError(
        "PA-100K ResNet50 checkpoint unavailable: current Rethinking_of_PAR README has an empty Google Drive link; "
        "place a .pth/.pt/.ckpt under models/par or set PAR_GDRIVE_ID."
    )


def _load_model():
    import torch
    from torchvision import models

    _ensure_repo()
    ckpt = _ensure_checkpoint()
    model = models.resnet50(weights=None)
    model.fc = torch.nn.Linear(model.fc.in_features, 26)
    state = torch.load(ckpt, map_location="cpu", weights_only=False)
    if isinstance(state, dict):
        for key in ("state_dict", "model", "model_state_dict"):
            if key in state and isinstance(state[key], dict):
                state = state[key]
                break
    clean = {}
    for k, v in state.items():
        k = k.removeprefix("module.").removeprefix("model.")
        clean[k] = v
    missing, unexpected = model.load_state_dict(clean, strict=False)
    if len(missing) > 20:
        raise RuntimeError(f"checkpoint did not match ResNet50-26 (missing={len(missing)}, unexpected={len(unexpected)})")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.eval().to(device)

    def embed(crops: list[np.ndarray]) -> np.ndarray:
        vals = []
        bs = 32
        for s in range(0, len(crops), bs):
            batch = []
            for crop in crops[s:s + bs]:
                img = cv2.resize(crop, (192, 256)).astype(np.float32)[:, :, ::-1] / 255.0
                img = (img - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
                    [0.229, 0.224, 0.225], dtype=np.float32
                )
                batch.append(img.transpose(2, 0, 1))
            tensor = torch.from_numpy(np.stack(batch)).float().to(device)
            with torch.no_grad():
                logits = model(tensor)
                probs = torch.sigmoid(logits).cpu().numpy()
            vals.extend(_l2(v) for v in probs)
        return np.vstack(vals)

    return embed, f"Rethinking_of_PAR:resnet50_pa100k:{device}:{ckpt.name}"


def _auc(same_sims: np.ndarray, diff_sims: np.ndarray) -> float:
    labels = np.r_[np.ones_like(same_sims), np.zeros_like(diff_sims)]
    scores = np.r_[same_sims, diff_sims]
    order = np.argsort(scores)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    n_pos, n_neg = len(same_sims), len(diff_sims)
    return float((ranks[labels == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def evaluate() -> dict:
    try:
        embed, backend = _load_model()
        if not (_CACHE / "same_pairs.npz").exists() or not (_CACHE / "diff_pairs.npz").exists():
            collect_pairs()
        same = np.load(_CACHE / "same_pairs.npz")
        diff = np.load(_CACHE / "diff_pairs.npz")

        def sims(npz) -> np.ndarray:
            out = []
            for s in range(0, npz["a"].shape[0], 32):
                crops_a = [npz["a"][i] for i in range(s, min(s + 32, npz["a"].shape[0]))]
                crops_b = [npz["b"][i] for i in range(s, min(s + 32, npz["b"].shape[0]))]
                ea, eb = embed(crops_a), embed(crops_b)
                out.extend(float(np.dot(a, b)) for a, b in zip(ea, eb))
            return np.asarray(out)

        same_sims, diff_sims = sims(same), sims(diff)
        result = {
            "model": "rethinking_par_resnet50_pa100k",
            "status": "ok",
            "backend": backend,
            "auc": round(_auc(same_sims, diff_sims), 4),
            "mean_same_sim": round(float(same_sims.mean()), 4),
            "mean_diff_sim": round(float(diff_sims.mean()), 4),
            "n_same": int(len(same_sims)),
            "n_diff": int(len(diff_sims)),
        }
    except Exception as exc:  # noqa: BLE001
        result = {"model": "rethinking_par_resnet50_pa100k", "status": "skipped", "reason": str(exc)[:400]}
    _OUT.write_text(json.dumps({"rethinking_par_resnet50_pa100k": result}, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--eval", action="store_true")
    args = ap.parse_args()
    if args.collect:
        collect_pairs()
        return 0
    if args.eval:
        r = evaluate()
        return 0 if r.get("status") == "ok" else 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
