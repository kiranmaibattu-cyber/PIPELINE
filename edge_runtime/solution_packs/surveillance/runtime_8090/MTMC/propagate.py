"""Propagate manual annotations onto MTMC tournament runs via IoU matching.

Reference: the manually-annotated legacy run (osnet_ain / cross_camera).
Targets:   every MTMC/reports/<run_id>/<scenario>_track_events.csv.
Output:    MTMC/reports/annotations_mtmc.csv (model column = run_id) —
           kept separate from annotations/annotations.csv so legacy
           evaluate.py reruns don't clobber it.

Frame alignment verified: MTMC runs use the same detector, sampling and
frame numbering as the legacy runner, so same-frame IoU matching is exact.

Usage:  python -m MTMC.propagate --scenario cross_camera
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reid_benchmark.evaluate import _load_events, propagate_annotations  # noqa: E402

_REPORTS = _ROOT / "MTMC" / "reports"
_OUT = _REPORTS / "annotations_mtmc.csv"
FIELDS = ["scenario", "model", "camera", "global_id", "person_id", "notes"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="cross_camera")
    ap.add_argument("--ref-model", default="osnet_ain")
    ap.add_argument("--iou-threshold", type=float, default=0.40)
    args = ap.parse_args()

    annot_path = _ROOT / "annotations" / "annotations.csv"
    raw = pd.read_csv(annot_path)
    ref_annots = raw[(raw["model"] == args.ref_model)
                     & (raw["scenario"] == args.scenario)
                     & (raw["notes"] == "manual")].copy()
    if ref_annots.empty:
        print("No manual reference annotations found.")
        return 1

    ref_csv = _ROOT / "reports" / args.ref_model / f"{args.scenario}_track_events.csv"
    ref_events = _load_events(ref_csv)

    all_rows: list[dict] = []
    for run_dir in sorted(_REPORTS.iterdir()):
        if not run_dir.is_dir():
            continue
        tgt_csv = run_dir / f"{args.scenario}_track_events.csv"
        if not tgt_csv.exists():
            continue
        tgt_events = _load_events(tgt_csv)
        rows = propagate_annotations(ref_annots, ref_events, tgt_events,
                                     args.scenario, run_dir.name, args.iou_threshold)
        print(f"  {run_dir.name:<55} {len(rows):3d} propagated IDs")
        all_rows.extend(rows)

    with _OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    print(f"\nwrote {len(all_rows)} rows -> {_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
