"""Per-embedder match-threshold calibration (verification-style, annotation-free).

Pairs are mined from the videos themselves:
  same-person pair  : two crops of the SAME IoU track >= min_gap frames apart
  diff-person pair  : two crops co-occurring in the SAME frame (different tracks)

threshold = 1 - (mean_same_sim + mean_diff_sim) / 2   (midpoint rule used for
the original CALIBRATED_THRESHOLDS). Embeddings are extracted with the same
TTA setting as the tournament, so distance semantics match Stage-1 runs.

Two phases (so each embedder can run in an isolated subprocess — several model
repos collide on top-level module names):

    python -m MTMC.calibrate_thresholds --collect          # once: mine crop pairs
    python -m MTMC.calibrate_thresholds --model osnet_ibn  # per model
    python -m MTMC.calibrate_thresholds --all              # subprocess per model

Results merge into MTMC/reports/calibrated_thresholds.json.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _ROOT / "MTMC" / "cache" / "calib_crops"
_OUT = _ROOT / "MTMC" / "reports" / "calibrated_thresholds.json"
_CONFIG_PATH: str | None = None  # set via --config; forwarded to subprocesses


def _set_domain(tag: str) -> None:
    """Switch cache/output files for a non-person domain (e.g. vehicle)."""
    global _CACHE, _OUT
    if tag:
        _CACHE = _ROOT / "MTMC" / "cache" / f"calib_crops_{tag}"
        _OUT = _ROOT / "MTMC" / "reports" / f"calibrated_thresholds_{tag}.json"


def collect_pairs(
    n_frames: int = 120,
    frame_step: int = 25,
    min_gap_frames: int = 3,
    max_same_pairs: int = 400,
    max_diff_pairs: int = 400,
    min_crop_h: int = 80,
) -> None:
    """Mine same/diff crop pairs from both cameras, save to _CACHE."""
    from MTMC.pipelines import load_mtmc_config
    from MTMC.adapters import MultiClassDetector, IoUTracker, crop_boxes

    config = load_mtmc_config(Path(_CONFIG_PATH)) if _CONFIG_PATH else load_mtmc_config()
    bench = config["benchmark"]
    detector = MultiClassDetector(bench["detector"], bench["confidence"],
                                  bench["iou"], set(bench["class_ids"]))

    same_pairs: list[tuple[np.ndarray, np.ndarray]] = []
    diff_pairs: list[tuple[np.ndarray, np.ndarray]] = []

    for video_key in ("ch9_5min", "ch10_5min"):
        cap = cv2.VideoCapture(str(_ROOT / config["videos"][video_key]))
        tracker = IoUTracker()
        track_crops: dict[int, list[tuple[int, np.ndarray]]] = {}
        sampled = 0
        frame_idx = 0
        while sampled < n_frames:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1
            if frame_idx % frame_step:
                continue
            sampled += 1
            boxes = detector.detect(frame)
            tracks = tracker.update(boxes, sampled)
            crops = crop_boxes(frame, [t.bbox for t in tracks])
            good = [(t, c) for t, c in zip(tracks, crops) if c.shape[0] >= min_crop_h]
            # diff pairs: co-occurring tracks in this frame
            for i in range(len(good)):
                for j in range(i + 1, len(good)):
                    if len(diff_pairs) < max_diff_pairs:
                        diff_pairs.append((good[i][1], good[j][1]))
            for t, c in good:
                track_crops.setdefault(t.local_id, []).append((sampled, c))
        cap.release()
        # same pairs: same track, >= min_gap sampled frames apart
        for crops_list in track_crops.values():
            for a in range(len(crops_list)):
                for b in range(a + 1, len(crops_list)):
                    if crops_list[b][0] - crops_list[a][0] >= min_gap_frames:
                        if len(same_pairs) < max_same_pairs:
                            same_pairs.append((crops_list[a][1], crops_list[b][1]))

    _CACHE.mkdir(parents=True, exist_ok=True)
    for name, pairs in (("same", same_pairs), ("diff", diff_pairs)):
        arr_a = np.array([cv2.resize(p[0], (128, 256)) for p in pairs], dtype=np.uint8)
        arr_b = np.array([cv2.resize(p[1], (128, 256)) for p in pairs], dtype=np.uint8)
        np.savez_compressed(_CACHE / f"{name}_pairs.npz", a=arr_a, b=arr_b)
    print(f"collected: {len(same_pairs)} same pairs, {len(diff_pairs)} diff pairs -> {_CACHE}")


def calibrate_model(key: str, tta_flip: bool = True) -> dict:
    from MTMC.adapters import load_embedder

    same = np.load(_CACHE / "same_pairs.npz")
    diff = np.load(_CACHE / "diff_pairs.npz")

    embedder, detail = load_embedder(key, tta_flip=tta_flip)
    if embedder is None:
        return {"model": key, "status": "skipped", "reason": detail}

    def _sims(npz) -> np.ndarray:
        crops_a = [npz["a"][i] for i in range(npz["a"].shape[0])]
        crops_b = [npz["b"][i] for i in range(npz["b"].shape[0])]
        sims = []
        bs = 32
        for s in range(0, len(crops_a), bs):
            ea = embedder.embed(crops_a[s:s + bs])
            eb = embedder.embed(crops_b[s:s + bs])
            sims.extend(float(np.dot(x, y)) for x, y in zip(ea, eb))
        return np.array(sims)

    same_sims = _sims(same)
    diff_sims = _sims(diff)

    mean_same, mean_diff = float(same_sims.mean()), float(diff_sims.mean())
    threshold = 1.0 - (mean_same + mean_diff) / 2.0
    # AUC via rank statistic
    labels = np.r_[np.ones_like(same_sims), np.zeros_like(diff_sims)]
    scores = np.r_[same_sims, diff_sims]
    order = np.argsort(scores)
    ranks = np.empty_like(order, dtype=float); ranks[order] = np.arange(1, len(scores) + 1)
    n_pos, n_neg = len(same_sims), len(diff_sims)
    auc = (ranks[labels == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)

    result = {
        "model": key, "status": "ok", "tta": tta_flip,
        "mean_same_sim": round(mean_same, 4), "mean_diff_sim": round(mean_diff, 4),
        "threshold": round(threshold, 4), "auc": round(float(auc), 4),
        "n_same": n_pos, "n_diff": n_neg, "backend": detail,
    }
    merged = json.loads(_OUT.read_text(encoding="utf-8")) if _OUT.exists() else {}
    merged[key] = result
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return result


def main() -> int:
    global _CONFIG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--model", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--no-tta", action="store_true")
    ap.add_argument("--config", default="")
    ap.add_argument("--domain", default="", help="suffix for cache/output (e.g. vehicle)")
    args = ap.parse_args()

    if args.config:
        _CONFIG_PATH = args.config
    _set_domain(args.domain)

    if args.collect:
        collect_pairs()
        return 0
    if args.model:
        r = calibrate_model(args.model, tta_flip=not args.no_tta)
        return 0 if r.get("status") == "ok" else 1
    if args.all:
        if not (_CACHE / "same_pairs.npz").exists():
            collect_pairs()
        from MTMC.pipelines import load_mtmc_config
        cfg = load_mtmc_config(Path(args.config)) if args.config else load_mtmc_config()
        roster = cfg["stage1_embedders"]
        done = json.loads(_OUT.read_text(encoding="utf-8")) if _OUT.exists() else {}
        for key in roster:
            if done.get(key, {}).get("status") == "ok":
                print(f"{key}: already calibrated ({done[key]['threshold']})")
                continue
            cmd = [sys.executable, "-m", "MTMC.calibrate_thresholds", "--model", key]
            if args.no_tta:
                cmd.append("--no-tta")
            if args.config:
                cmd += ["--config", args.config]
            if args.domain:
                cmd += ["--domain", args.domain]
            subprocess.run(cmd, cwd=str(_ROOT))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
