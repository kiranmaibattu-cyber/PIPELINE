"""Auto-learn per-camera-pair transition-time windows (no manual measurement).

The system observes its own high-confidence cross-camera matches and derives the
plausible travel-time window from them. Pipeline:

  1. Group a run's track events into per-camera LOCAL tracklets (single-camera,
     continuous — no cross-camera merge contamination).
  2. Embed the best crop(s) of each tracklet with the appearance model.
  3. Find RECIPROCAL-BEST cross-camera tracklet pairs whose appearance distance
     is comfortably below the match threshold — these are distinctive people
     (orange kurta, green saree) almost certainly matched correctly.
  4. From those confident pairs, measure the time gap between the two tracklets
     (non-overlapping only; overlapping = physically impossible, excluded).
  5. Fit a robust window [p10, p90] per camera pair -> learned_transitions.json.

The topology gate then loads these learned windows instead of a hand-set default.

Usage:
    python -m MTMC.learn_transitions --config MTMC/configs/newclips_ch2_ch16.yaml \
        --run transreid_ssl__cross_camera__iou__gquality_topk10__newclip
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent


def _load_tracklets(events_csv: Path, min_len: int) -> dict:
    """(camera, local_id) -> {frames, boxes}."""
    tr: dict = defaultdict(lambda: {"frames": [], "boxes": []})
    with events_csv.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r["camera"], r["local_id"])
            tr[key]["frames"].append(int(r["frame"]))
            tr[key]["boxes"].append((float(r["x1"]), float(r["y1"]),
                                     float(r["x2"]), float(r["y2"])))
    return {k: v for k, v in tr.items() if len(v["frames"]) >= min_len}


def _best_crops(video: str, tracklet: dict, n: int, pe: int) -> list:
    """Extract up to n largest crops spread across a tracklet."""
    order = sorted(range(len(tracklet["frames"])),
                   key=lambda i: -((tracklet["boxes"][i][2] - tracklet["boxes"][i][0])
                                   * (tracklet["boxes"][i][3] - tracklet["boxes"][i][1])))
    picks = order[:n]
    cap = cv2.VideoCapture(video)
    crops = []
    for i in picks:
        cap.set(cv2.CAP_PROP_POS_FRAMES, tracklet["frames"][i] * pe)
        ok, fr = cap.read()
        if not ok:
            continue
        x1, y1, x2, y2 = [int(v) for v in tracklet["boxes"][i]]
        c = fr[max(0, y1):y2, max(0, x1):x2]
        if c.size:
            crops.append(c)
    cap.release()
    return crops


def learn(config_path: str, run_id: str, embedder_key: str = "transreid_ssl",
          min_len: int = 5, crops_per_track: int = 3,
          conf_frac: float = 0.85, min_floor_s: float = 3.0) -> dict:
    import yaml
    from MTMC.adapters import load_embedder, CALIBRATED_THRESHOLDS

    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    labels = config.get("camera_labels", ["ch9", "ch10"])
    cam_a, cam_b = labels[0], labels[1]
    vids = {cam_a: str(_ROOT / config["videos"]["ch9_5min"]),
            cam_b: str(_ROOT / config["videos"]["ch10_5min"])}
    pe = int(config["benchmark"].get("process_every_n_frames", 10))
    fps = 25.0

    calib_path = _ROOT / config.get("calibration_file", "MTMC/reports/calibrated_thresholds.json")
    calib = json.loads(calib_path.read_text(encoding="utf-8")) if calib_path.exists() else {}
    threshold = calib.get(embedder_key, {}).get("threshold", CALIBRATED_THRESHOLDS.get(embedder_key, 0.35))
    strict = conf_frac * threshold

    events_csv = _ROOT / "MTMC" / "reports" / run_id / "cross_camera_track_events.csv"
    tracklets = _load_tracklets(events_csv, min_len)
    print(f"tracklets (>= {min_len} dets): {sum(1 for k in tracklets if k[0]==cam_a)} {cam_a}, "
          f"{sum(1 for k in tracklets if k[0]==cam_b)} {cam_b}")

    embedder, _ = load_embedder(embedder_key, tta_flip=True)
    if embedder is None:
        raise RuntimeError("embedder load failed")

    # embed each tracklet (mean of a few best crops) + record time span
    emb: dict = {}
    span: dict = {}
    for (cam, lid), t in tracklets.items():
        crops = _best_crops(vids[cam], t, crops_per_track, pe)
        if not crops:
            continue
        v = embedder.embed(crops)
        if v.shape[0] == 0 or v.shape[1] <= 1:
            continue
        m = v.mean(axis=0)
        emb[(cam, lid)] = m / (np.linalg.norm(m) + 1e-12)
        fr = t["frames"]
        span[(cam, lid)] = (min(fr) * pe / fps, max(fr) * pe / fps)

    keys_a = [k for k in emb if k[0] == cam_a]
    keys_b = [k for k in emb if k[0] == cam_b]
    if not keys_a or not keys_b:
        print("insufficient tracklets in one camera")
        return {}

    A = np.stack([emb[k] for k in keys_a])
    B = np.stack([emb[k] for k in keys_b])
    D = 1.0 - A @ B.T  # (na, nb) appearance distance

    # reciprocal-best confident pairs
    best_b_for_a = D.argmin(axis=1)
    best_a_for_b = D.argmin(axis=0)
    gaps = []
    pairs = []
    for i, ka in enumerate(keys_a):
        j = best_b_for_a[i]
        if best_a_for_b[j] != i:
            continue                       # not reciprocal
        if D[i, j] > strict:
            continue                       # not confident/distinctive
        aS, aE = span[ka]
        bS, bE = span[keys_b[j]]
        # non-overlapping gap; overlap => physically impossible => skip
        if bS >= aE:
            gap = bS - aE
        elif aS >= bE:
            gap = aS - bE
        else:
            continue                       # temporal overlap: exclude
        gaps.append(gap)
        pairs.append((ka, keys_b[j], round(D[i, j], 3), round(gap, 1)))

    result = {"camera_pair": f"{cam_a}|{cam_b}", "n_confident_pairs": len(gaps),
              "threshold_used": round(strict, 4)}
    if len(gaps) >= 3:
        g = np.array(gaps)
        lo = max(min_floor_s, float(np.percentile(g, 10)))
        hi = float(np.percentile(g, 90))
        result.update({"min_s": round(lo, 1), "max_s": round(hi, 1),
                       "median_s": round(float(np.median(g)), 1),
                       "learned": True})
    else:
        result.update({"learned": False,
                       "reason": f"only {len(gaps)} confident pairs (need >=3)"})

    out = {result["camera_pair"]: result}
    out_path = _ROOT / "MTMC" / "reports" / "learned_transitions.json"
    merged = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    merged.update(out)
    out_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    print(json.dumps(result, indent=2))
    print("confident transition pairs (tracklet_a, tracklet_b, appdist, gap_s):")
    for p in sorted(pairs, key=lambda x: x[3])[:15]:
        print("  ", p)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--embedder", default="transreid_ssl")
    args = ap.parse_args()
    learn(args.config, args.run, args.embedder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
