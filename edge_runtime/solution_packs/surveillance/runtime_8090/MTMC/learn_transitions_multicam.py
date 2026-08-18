"""Learn per-pair transition windows for ALL camera pairs from a multi-camera run.

Generalizes learn_transitions.py from one pair to every pair. For each camera
pair it mines reciprocal-best, high-confidence cross-camera tracklet matches
(distinctive people) and derives a window [max(0, p5), p95] of their travel-time
gaps. ADJACENT cameras (e.g. waiting-area ch9/ch10 next to lift ch2) naturally
get a near-zero minimum, so fast real transitions are no longer rejected;
DISTANT pairs (ch2<->ch16) keep a protective minimum. Overlapping pairs are
forced to min 0.

Usage:
    python -m MTMC.learn_transitions_multicam --config MTMC/configs/multicam_5.yaml \
        --events NEW/reports/track_events.csv
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np

from MTMC.learn_transitions import _best_crops

_ROOT = Path(__file__).resolve().parent.parent


def learn_all(config_path: str, events_csv: str, embedder_key: str = "transreid_ssl",
              min_len: int = 5, crops_per_track: int = 3, conf_frac: float = 0.85) -> dict:
    import csv
    import yaml
    from MTMC.adapters import load_embedder, CALIBRATED_THRESHOLDS

    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    vids = {c["label"]: str(_ROOT / c["video"]) for c in config["cameras"]}
    pe = int(config["benchmark"].get("process_every_n_frames", 10))
    fps = 25.0
    calib = json.loads((_ROOT / config["calibration_file"]).read_text(encoding="utf-8"))
    threshold = calib.get(embedder_key, {}).get("threshold", CALIBRATED_THRESHOLDS.get(embedder_key, 0.35))
    strict = conf_frac * threshold
    overlapping = {frozenset(p) for p in config.get("topology", {}).get("overlapping_pairs", [])}

    # group tracklets by (camera, local_id)
    tr = defaultdict(lambda: {"frames": [], "boxes": []})
    with open(_ROOT / events_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r["camera"], r["local_id"])
            tr[k]["frames"].append(int(r["frame"]))
            tr[k]["boxes"].append((float(r["x1"]), float(r["y1"]), float(r["x2"]), float(r["y2"])))
    tracklets = {k: v for k, v in tr.items() if len(v["frames"]) >= min_len}

    embedder, _ = load_embedder(embedder_key, tta_flip=True)
    emb, span = {}, {}
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

    cams = [c["label"] for c in config["cameras"]]
    result = {}
    for ca, cb in combinations(cams, 2):
        ka = [k for k in emb if k[0] == ca]
        kb = [k for k in emb if k[0] == cb]
        if not ka or not kb:
            continue
        A = np.stack([emb[k] for k in ka]); B = np.stack([emb[k] for k in kb])
        D = 1.0 - A @ B.T
        bb = D.argmin(axis=1); ba = D.argmin(axis=0)
        gaps = []
        for i, kai in enumerate(ka):
            j = bb[i]
            if ba[j] != i or D[i, j] > strict:
                continue
            aS, aE = span[kai]; bS, bE = span[kb[j]]
            if bS >= aE:
                gaps.append(bS - aE)
            elif aS >= bE:
                gaps.append(aS - bE)
            else:
                gaps.append(0.0)  # temporal overlap = adjacent/overlapping -> 0 gap
        key = f"{ca}|{cb}"
        if frozenset((ca, cb)) in overlapping:
            result[key] = {"learned": True, "min_s": 0.0, "max_s": float(config["gallery"]["max_age_seconds"]),
                           "n_confident_pairs": len(gaps), "adjacency": "overlapping"}
        elif len(gaps) >= 3:
            g = np.array(gaps)
            med = float(np.median(g))
            # Adjacency by MEDIAN travel time (robust to false-match near-0
            # outliers): ADJACENT cameras have a small median -> fast real
            # transitions -> min 0. DISTANT cameras have a large median; there a
            # <3s gap is physically impossible = a false match, so exclude those
            # and set a protective minimum that blocks concurrent merges.
            adjacent = med < 30.0
            if adjacent:
                min_s = 0.0
            else:
                real = g[g >= 3.0]
                min_s = round(max(5.0, float(np.percentile(real, 10))), 1) if len(real) else 5.0
            result[key] = {"learned": True, "min_s": min_s,
                           "max_s": round(float(np.percentile(g, 95)), 1),
                           "median_s": round(med, 1),
                           "adjacency": "adjacent" if adjacent else "distant",
                           "n_confident_pairs": len(gaps)}
        else:
            result[key] = {"learned": False, "n_confident_pairs": len(gaps),
                           "reason": "insufficient confident pairs"}

    out = _ROOT / "MTMC" / "reports" / "learned_transitions.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: {kk: v[kk] for kk in ("min_s", "max_s", "median_s", "n_confident_pairs")
                          if kk in v} for k, v in result.items() if v.get("learned")}, indent=2))
    n_learned = sum(1 for v in result.values() if v.get("learned"))
    print(f"\nlearned windows for {n_learned}/{len(result)} camera pairs -> {out}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="MTMC/configs/multicam_5.yaml")
    ap.add_argument("--events", default="NEW/reports/track_events.csv")
    args = ap.parse_args()
    learn_all(args.config, args.events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
