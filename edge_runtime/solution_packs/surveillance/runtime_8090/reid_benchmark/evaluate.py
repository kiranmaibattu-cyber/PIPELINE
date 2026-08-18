"""Auto-propagate ground-truth annotations to all models via bbox IoU matching.

Because every model runs the same YOLO detector, detections at the same
(frame, camera) are identical across models.  We can therefore:

  1. Load the reference model's annotations (annotated once by hand).
  2. For each other model, match its bboxes to the reference bboxes using IoU.
  3. Copy the person_id from the matched reference detection.
  4. Score all models using score_annotations.score().

Usage
-----
    python -m reid_benchmark.evaluate --scenario cross_camera --ref-model osnet_ain
    python -m reid_benchmark.evaluate --scenario both --ref-model osnet_ain
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import pandas as pd

from .config import load_config
from .score_annotations import score


# ---------------------------------------------------------------------------
# IoU helpers
# ---------------------------------------------------------------------------

def _iou(a: dict, b: dict) -> float:
    """Compute IoU between two bbox dicts with keys x1,y1,x2,y2."""
    ix1 = max(a["x1"], b["x1"])
    iy1 = max(a["y1"], b["y1"])
    ix2 = min(a["x2"], b["x2"])
    iy2 = min(a["y2"], b["y2"])
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (a["x2"] - a["x1"]) * (a["y2"] - a["y1"])
    area_b = (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])
    return inter / (area_a + area_b - inter)


# ---------------------------------------------------------------------------
# Core propagation
# ---------------------------------------------------------------------------

def _load_events(path: Path) -> dict[tuple[int, str], list[dict]]:
    """Load track_events CSV into {(frame, camera): [row_dicts]}."""
    index: dict[tuple[int, str], list[dict]] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (int(row["frame"]), row["camera"])
            index.setdefault(key, []).append({
                "global_id": int(row["global_id"]),
                "x1": float(row["x1"]), "y1": float(row["y1"]),
                "x2": float(row["x2"]), "y2": float(row["y2"]),
            })
    return index


def propagate_annotations(
    ref_annots: pd.DataFrame,
    ref_events: dict[tuple[int, str], list[dict]],
    tgt_events: dict[tuple[int, str], list[dict]],
    scenario: str,
    tgt_model: str,
    iou_threshold: float = 0.40,
) -> list[dict]:
    """Return annotation rows for tgt_model derived from ref_annots via IoU matching."""

    # Build (camera, global_id) → person_id lookup for the reference
    ref_lookup: dict[tuple[str, int], str] = {}
    for _, row in ref_annots.iterrows():
        ref_lookup[(row["camera"], int(row["global_id"]))] = str(row["person_id"])

    # Build (frame, camera, global_id) → person_id for reference using track events
    ref_det_pid: dict[tuple[int, str, int], str] = {}
    for (frame, cam), dets in ref_events.items():
        for det in dets:
            gid = det["global_id"]
            pid = ref_lookup.get((cam, gid))
            if pid:
                ref_det_pid[(frame, cam, gid)] = pid

    # For each target detection, find the best-matching reference detection
    matched: dict[tuple[str, int], list[str]] = {}  # (camera, tgt_gid) → [pid]
    for (frame, cam), tgt_dets in tgt_events.items():
        ref_dets = ref_events.get((frame, cam), [])
        if not ref_dets:
            continue
        for tgt in tgt_dets:
            best_iou = 0.0
            best_pid: str | None = None
            for ref in ref_dets:
                iou_val = _iou(tgt, ref)
                if iou_val > best_iou:
                    pid = ref_det_pid.get((frame, cam, ref["global_id"]))
                    if pid:
                        best_iou = iou_val
                        best_pid = pid
            if best_iou >= iou_threshold and best_pid is not None:
                key = (cam, tgt["global_id"])
                matched.setdefault(key, []).append(best_pid)

    # Majority-vote person_id for each (camera, global_id)
    rows: list[dict] = []
    for (cam, gid), pids in sorted(matched.items()):
        pid = max(set(pids), key=pids.count)
        rows.append({
            "scenario": scenario,
            "model": tgt_model,
            "camera": cam,
            "global_id": gid,
            "person_id": pid,
            "notes": "auto-propagated",
        })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-evaluate all Re-ID models")
    parser.add_argument("--config", default="configs/benchmark.yaml")
    parser.add_argument("--ref-model", default="osnet_ain")
    parser.add_argument("--scenario", default="cross_camera",
                        help="cross_camera | single_delay | both")
    parser.add_argument("--iou-threshold", type=float, default=0.40)
    args = parser.parse_args()

    config = load_config(args.config)
    reports_dir = Path(config["paths"]["reports_dir"])
    annot_dir = Path(config["paths"]["annotations_dir"])
    annot_path = annot_dir / "annotations.csv"
    all_models: list[str] = config["models"]

    if not annot_path.exists() or annot_path.stat().st_size < 20:
        print(f"ERROR: {annot_path} is empty.")
        print("       Run the annotation tool first:")
        print(f"         python -m reid_benchmark.annotate --ref-model {args.ref_model} --scenario cross_camera")
        return 1

    raw_annots = pd.read_csv(annot_path)
    raw_annots = raw_annots.dropna(subset=["scenario", "model", "global_id", "person_id"])

    scenarios = ["cross_camera", "single_delay"] if args.scenario == "both" else [args.scenario]

    all_new_rows: list[dict] = []
    fieldnames = ["scenario", "model", "camera", "global_id", "person_id", "notes"]

    for scenario in scenarios:
        ref_csv = reports_dir / args.ref_model / f"{scenario}_track_events.csv"
        if not ref_csv.exists():
            print(f"[SKIP] No track events for {args.ref_model}/{scenario}")
            continue

        ref_annots = raw_annots[
            (raw_annots["model"] == args.ref_model) &
            (raw_annots["scenario"] == scenario)
        ].copy()

        if ref_annots.empty:
            print(f"[SKIP] No annotations for {args.ref_model}/{scenario} — annotate it first.")
            continue

        ref_events = _load_events(ref_csv)
        n_ref_gids = ref_annots[["camera", "global_id"]].drop_duplicates().shape[0]
        print(f"\n=== Scenario: {scenario} | Ref: {args.ref_model} ({n_ref_gids} annotated IDs) ===")

        for model_key in all_models:
            if model_key == args.ref_model:
                continue
            tgt_csv = reports_dir / model_key / f"{scenario}_track_events.csv"
            if not tgt_csv.exists():
                print(f"  [SKIP] {model_key} — no track events")
                continue

            tgt_events = _load_events(tgt_csv)
            rows = propagate_annotations(
                ref_annots, ref_events, tgt_events,
                scenario, model_key, args.iou_threshold,
            )
            n_matched = len(rows)
            print(f"  {model_key:<35} → {n_matched:3d} propagated IDs")
            all_new_rows.extend(rows)

    if not all_new_rows:
        print("\nNothing propagated — check that reference annotations exist for the chosen scenario.")
        return 1

    # Merge: keep existing manual rows, replace any auto-propagated rows
    existing: list[dict] = []
    if annot_path.exists():
        with annot_path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("notes") == "auto-propagated":
                    continue  # will be replaced
                existing.append(r)

    with annot_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(existing)
        w.writerows(all_new_rows)

    total_written = len(existing) + len(all_new_rows)
    print(f"\nWrote {total_written} total rows to {annot_path}")
    print(f"  manual: {len(existing)}   auto-propagated: {len(all_new_rows)}")

    # Score everything
    print("\n" + "=" * 70)
    print("ACCURACY SCORES")
    print("=" * 70)
    full_df = pd.read_csv(annot_path)
    result = score(full_df)

    if result["status"] != "scored":
        print(result)
        return 1

    runs = sorted(result["runs"], key=lambda r: (r["scenario"], -r["global_id_purity"]))

    hdr = f"{'Model':<35} {'Scenario':<14} {'People':>6} {'GIDs':>5} {'ID-Purity':>9} {'ID-Consistency':>14} {'Splits':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in runs:
        print(
            f"{r['model']:<35} {r['scenario']:<14} "
            f"{r['unique_people']:>6} {r['unique_global_ids']:>5} "
            f"{r['global_id_purity']:>9.1%} {r['person_consistency_accuracy']:>14.1%} "
            f"{r['split_error_count']:>6}"
        )

    # Save JSON
    out_path = Path(config["paths"]["reports_dir"]) / "annotation_accuracy.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nFull results saved to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
