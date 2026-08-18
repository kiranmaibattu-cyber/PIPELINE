"""Score vehicle runs directly against MTID ground truth (no manual labeling).

For each run detection (frame, camera, bbox, gid): IoU-match to GT boxes of the
same frame+camera; majority-vote GT vehicle ID per (camera, gid); emit
annotation rows and score with MTMC.metrics.

Frame alignment: run frame_idx f (sampled every N raw frames of the mp4)
corresponds to MTID frame number f*N + 1.

Usage:  python -m MTMC.vehicle.score_gt          # scores all vehicle runs
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from MTMC.metrics import compute_metrics  # noqa: E402

_REPORTS = _ROOT / "MTMC" / "reports"
_DATA = _ROOT / "MTMC" / "vehicle" / "data"
PROCESS_EVERY = 10
IOU_THR = 0.4


def _iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def load_gt() -> dict[tuple[int, str], list[tuple[str, tuple]]]:
    gt: dict[tuple[int, str], list] = defaultdict(list)
    for cam in ("drone", "infra"):
        with (_DATA / f"gt_events_{cam}.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                gt[(int(r["frame"]), cam)].append(
                    (r["gt_id"], (float(r["x1"]), float(r["y1"]),
                                  float(r["x2"]), float(r["y2"]))))
    return gt


def score_run(run_dir: Path, gt) -> dict | None:
    ev_path = run_dir / "cross_camera_track_events.csv"
    if not ev_path.exists():
        return None
    # per-detection GT id via IoU
    det_rows = []
    with ev_path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            mtid_frame = int(r["frame"]) * PROCESS_EVERY + 1
            cam = r["camera"]
            box = (float(r["x1"]), float(r["y1"]), float(r["x2"]), float(r["y2"]))
            best_id, best_iou = None, 0.0
            for gt_id, gt_box in gt.get((mtid_frame, cam), []):
                v = _iou(box, gt_box)
                if v > best_iou:
                    best_iou, best_id = v, gt_id
            if best_id is not None and best_iou >= IOU_THR:
                det_rows.append({"frame": int(r["frame"]), "camera": cam,
                                 "gid": int(r["global_id"]), "pid": f"V{best_id}"})
    if not det_rows:
        return None
    for d in det_rows:
        d["coverage_total"] = 0
    result = compute_metrics(det_rows)
    result["run_id"] = run_dir.name
    return result


def main() -> int:
    gt = load_gt()
    results = []
    for run_dir in sorted(_REPORTS.iterdir()):
        if not run_dir.is_dir() or "__v_" not in run_dir.name:
            continue
        r = score_run(run_dir, gt)
        if r:
            results.append(r)
            print(f"{run_dir.name:<60} idf1={r['idf1']} purity={r['purity']} "
                  f"switches={r['id_switches']} inflation={r['gid_inflation']} "
                  f"xp={r['cross_camera_precision']} xr={r['cross_camera_recall']}")
    out = _REPORTS / "vehicle_comparison.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
