"""Identity metrics from ground-truth annotations + track events.

Extends reid_benchmark.score_annotations (purity-style) with:
  - IDF1-style precision/recall/F1 via optimal GID<->person bipartite matching
  - ID switches (per person, per camera, time-ordered)
  - GID inflation (predicted unique IDs / true persons)
  - cross-camera link precision/recall (same-frame co-occurrence pairs)

Inputs
------
annotations.csv rows: scenario, model, camera, global_id, person_id, notes
track_events CSV rows: frame, scenario, model, camera, local_id, global_id, x1..y2

Usage
-----
    python -m MTMC.metrics --events reports/osnet_ain/cross_camera_track_events.csv \
        --annotations annotations/annotations.csv --model osnet_ain --scenario cross_camera
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

try:
    from scipy.optimize import linear_sum_assignment
    _HAVE_SCIPY = True
except Exception:  # noqa: BLE001
    _HAVE_SCIPY = False


def load_annotation_lookup(
    annotations_csv: Path, model: str, scenario: str
) -> dict[tuple[str, int], str]:
    """(camera, global_id) -> person_id for one model/scenario."""
    lookup: dict[tuple[str, int], str] = {}
    with annotations_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("model") != model or row.get("scenario") != scenario:
                continue
            pid = str(row.get("person_id", "")).strip()
            if not pid or pid.lower() in ("nan", "none", ""):
                continue
            lookup[(row["camera"], int(float(row["global_id"])))] = pid
    return lookup


def load_labeled_detections(
    events_csv: Path, lookup: dict[tuple[str, int], str]
) -> list[dict]:
    """Track-event rows joined with person labels. Unlabeled GIDs are dropped
    (coverage is reported separately)."""
    dets: list[dict] = []
    total = 0
    with events_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += 1
            gid = int(float(row["global_id"]))
            cam = row["camera"]
            pid = lookup.get((cam, gid))
            if pid is None:
                continue
            dets.append({
                "frame": int(row["frame"]),
                "camera": cam,
                "gid": gid,
                "pid": pid,
            })
    for d in dets:
        d["coverage_total"] = total  # stashed once; read from dets[0]
    return dets


def _bipartite_idtp(dets: list[dict]) -> int:
    """Optimal 1-1 GID<->person assignment maximizing shared detections."""
    gids = sorted({d["gid"] for d in dets})
    pids = sorted({d["pid"] for d in dets})
    overlap = np.zeros((len(gids), len(pids)), dtype=np.int64)
    gidx = {g: i for i, g in enumerate(gids)}
    pidx = {p: i for i, p in enumerate(pids)}
    for d in dets:
        overlap[gidx[d["gid"]], pidx[d["pid"]]] += 1
    if _HAVE_SCIPY:
        r, c = linear_sum_assignment(-overlap)
        return int(overlap[r, c].sum())
    # greedy fallback
    taken_g: set[int] = set()
    taken_p: set[int] = set()
    idtp = 0
    for _ in range(min(len(gids), len(pids))):
        best = (-1, -1, 0)
        for i in range(len(gids)):
            if i in taken_g:
                continue
            for j in range(len(pids)):
                if j in taken_p:
                    continue
                if overlap[i, j] > best[2]:
                    best = (i, j, int(overlap[i, j]))
        if best[2] == 0:
            break
        taken_g.add(best[0]); taken_p.add(best[1]); idtp += best[2]
    return idtp


def compute_metrics(dets: list[dict]) -> dict:
    if not dets:
        return {"status": "no_labeled_detections"}

    n = len(dets)
    gids = {d["gid"] for d in dets}
    pids = {d["pid"] for d in dets}

    # IDF1-style (pred and GT detection sets coincide -> IDP == IDR == IDF1)
    idtp = _bipartite_idtp(dets)
    idf1 = idtp / n

    # Purity: majority person fraction per GID, detection-weighted
    by_gid: dict[int, Counter] = defaultdict(Counter)
    for d in dets:
        by_gid[d["gid"]][d["pid"]] += 1
    pure = sum(c.most_common(1)[0][1] for c in by_gid.values())
    purity = pure / n

    # ID switches: per (person, camera) time-ordered GID changes
    traj: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for d in dets:
        traj[(d["pid"], d["camera"])].append((d["frame"], d["gid"]))
    switches = 0
    for seq in traj.values():
        seq.sort()
        for (_, g1), (_, g2) in zip(seq, seq[1:]):
            if g1 != g2:
                switches += 1

    # GID inflation
    inflation = len(gids) / max(1, len(pids))

    # Cross-camera links (same frame, different camera)
    by_frame: dict[int, list[dict]] = defaultdict(list)
    for d in dets:
        by_frame[d["frame"]].append(d)
    tp = fp = fn = 0
    for frame_dets in by_frame.values():
        for i in range(len(frame_dets)):
            for j in range(i + 1, len(frame_dets)):
                a, b = frame_dets[i], frame_dets[j]
                if a["camera"] == b["camera"]:
                    continue
                same_gid = a["gid"] == b["gid"]
                same_pid = a["pid"] == b["pid"]
                if same_gid and same_pid:
                    tp += 1
                elif same_gid and not same_pid:
                    fp += 1
                elif same_pid and not same_gid:
                    fn += 1
    xcam_p = tp / (tp + fp) if (tp + fp) else None
    xcam_r = tp / (tp + fn) if (tp + fn) else None

    coverage = n / dets[0]["coverage_total"] if dets[0].get("coverage_total") else None

    return {
        "status": "scored",
        "labeled_detections": n,
        "label_coverage": round(coverage, 3) if coverage else None,
        "unique_persons": len(pids),
        "unique_gids": len(gids),
        "idf1": round(idf1, 4),
        "purity": round(purity, 4),
        "id_switches": switches,
        "gid_inflation": round(inflation, 2),
        "cross_camera_precision": round(xcam_p, 4) if xcam_p is not None else None,
        "cross_camera_recall": round(xcam_r, 4) if xcam_r is not None else None,
    }


def score_run(events_csv: Path, annotations_csv: Path, model: str, scenario: str) -> dict:
    lookup = load_annotation_lookup(annotations_csv, model, scenario)
    if not lookup:
        return {"status": "no_annotations", "model": model, "scenario": scenario}
    dets = load_labeled_detections(events_csv, lookup)
    result = compute_metrics(dets)
    result.update({"model": model, "scenario": scenario})
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Score one run against annotations")
    ap.add_argument("--events", required=True)
    ap.add_argument("--annotations", default="annotations/annotations.csv")
    ap.add_argument("--model", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    result = score_run(Path(args.events), Path(args.annotations), args.model, args.scenario)
    print(json.dumps(result, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result.get("status") == "scored" else 1


if __name__ == "__main__":
    raise SystemExit(main())
