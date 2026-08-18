"""Annotation tool for Re-ID ground-truth labelling.

Workflow
--------
1. Extracts one representative crop per (camera, global_id) for a reference model.
2. Saves crops to  annotations/crops/<scenario>/  as JPEG files.
3. Writes a  contact-sheet PNG  so you can view all identities side-by-side.
4. Prompts you interactively in the terminal to assign a person label to each
   global_id (e.g.  "1" for the first unique person, "2" for the second …).
5. Writes the resulting labels to  annotations/annotations.csv.

Usage
-----
    python -m reid_benchmark.annotate --scenario cross_camera --ref-model osnet_ain
    python -m reid_benchmark.annotate --scenario single_delay --ref-model osnet_ain

After annotating the reference model, run the evaluator to auto-propagate to
all other models:
    python -m reid_benchmark.evaluate --scenario cross_camera --ref-model osnet_ain
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import cv2
import numpy as np

from .config import load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _best_crop_per_gid(events_path: Path) -> dict[tuple[str, int], dict]:
    """Return the largest-bbox record per (camera, global_id)."""
    best: dict[tuple[str, int], dict] = {}
    with events_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cam = row["camera"]
            gid = int(row["global_id"])
            area = (float(row["x2"]) - float(row["x1"])) * (float(row["y2"]) - float(row["y1"]))
            key = (cam, gid)
            if key not in best or area > best[key]["area"]:
                best[key] = {
                    "frame": int(row["frame"]),
                    "camera": cam,
                    "global_id": gid,
                    "x1": float(row["x1"]), "y1": float(row["y1"]),
                    "x2": float(row["x2"]), "y2": float(row["y2"]),
                    "area": area,
                }
    return best


def _extract_crop(video_path: str, target_sampled_frame: int,
                  x1: float, y1: float, x2: float, y2: float,
                  process_every: int = 10) -> np.ndarray | None:
    """Seek to the raw video frame corresponding to sampled frame index and extract crop."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    raw_frame = target_sampled_frame * process_every
    cap.set(cv2.CAP_PROP_POS_FRAMES, raw_frame)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    h, w = frame.shape[:2]
    x1c, y1c = max(0, int(x1)), max(0, int(y1))
    x2c, y2c = min(w, int(x2)), min(h, int(y2))
    if x2c <= x1c or y2c <= y1c:
        return None
    return frame[y1c:y2c, x1c:x2c]


def _make_contact_sheet(crops_dir: Path, records: list[dict], cameras: list[str]) -> Path:
    """Tile crops into a contact sheet PNG; returns the output path."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.image as mpimg
    except ImportError:
        print("matplotlib not installed – skipping contact sheet.")
        return crops_dir / "contact_sheet.png"

    # Group by camera
    by_cam: dict[str, list[dict]] = {c: [] for c in cameras}
    for r in records:
        by_cam.setdefault(r["camera"], []).append(r)
    for c in cameras:
        by_cam[c].sort(key=lambda r: r["global_id"])

    max_per_cam = max(len(v) for v in by_cam.values()) if by_cam else 1
    n_rows = len(cameras)
    n_cols = max(1, max_per_cam)
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(max(12, n_cols * 1.5), n_rows * 3 + 0.5))
    if n_rows == 1:
        axes = [axes]
    if n_cols == 1:
        axes = [[ax] for ax in axes]

    for row_idx, cam in enumerate(cameras):
        items = by_cam.get(cam, [])
        for col_idx in range(n_cols):
            ax = axes[row_idx][col_idx]
            ax.axis("off")
            if col_idx < len(items):
                rec = items[col_idx]
                img_path = crops_dir / rec["filename"]
                if img_path.exists():
                    img = cv2.imread(str(img_path))
                    if img is not None:
                        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                        ax.imshow(img_rgb)
                        ax.set_title(f"{cam}\nGID {rec['global_id']}", fontsize=7)
            if col_idx == 0:
                ax.set_ylabel(cam, fontsize=8, rotation=0, labelpad=40)

    plt.suptitle("Contact Sheet — assign same person_id to same real person", fontsize=10)
    plt.tight_layout()
    out = crops_dir / "contact_sheet.png"
    plt.savefig(str(out), dpi=120, bbox_inches="tight")
    plt.close()
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Re-ID annotation tool")
    parser.add_argument("--config", default="configs/benchmark.yaml")
    parser.add_argument("--ref-model", default="osnet_ain", help="Reference model to annotate")
    parser.add_argument("--scenario", choices=["single_delay", "cross_camera"], default="cross_camera")
    parser.add_argument("--process-every", type=int, default=10, help="Frames skipped between sampled frames")
    args = parser.parse_args()

    config = load_config(args.config)
    bench = config["benchmark"]
    videos = config["videos"]
    reports_dir = Path(config["paths"]["reports_dir"])
    annot_dir = Path(config["paths"]["annotations_dir"])

    events_path = reports_dir / args.ref_model / f"{args.scenario}_track_events.csv"
    if not events_path.exists():
        print(f"ERROR: track_events not found at {events_path}")
        print(f"       Run benchmark first: python -m reid_benchmark.runner --models {args.ref_model} --scenario {args.scenario} --no-display")
        return 1

    # Determine video paths for this scenario
    if args.scenario == "single_delay":
        video_map = {"ch9 live": videos["ch9_5min"], f"ch9 +{bench['delay_seconds']}s": videos["ch9_5min"]}
    else:
        video_map = {"ch9": videos["ch9_5min"], "ch10": videos["ch10_5min"]}

    # Extract best crops
    best = _best_crop_per_gid(events_path)
    cameras = sorted({k[0] for k in best})
    print(f"\nReference model : {args.ref_model}")
    print(f"Scenario        : {args.scenario}")
    print(f"Cameras         : {cameras}")
    print(f"Unique global IDs: {len(best)}")

    crops_dir = annot_dir / "crops" / args.scenario
    crops_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    for (cam, gid), rec in sorted(best.items()):
        # Find video path for this camera label
        video_path = None
        for label, vpath in video_map.items():
            if label.startswith(cam):
                video_path = vpath
                break
        if video_path is None:
            video_path = list(video_map.values())[0]

        crop = _extract_crop(video_path, rec["frame"], rec["x1"], rec["y1"],
                             rec["x2"], rec["y2"], args.process_every)
        filename = f"{cam}_gid{gid:04d}.jpg"
        if crop is not None:
            cv2.imwrite(str(crops_dir / filename), crop)
        else:
            # Create placeholder
            placeholder = np.zeros((128, 64, 3), dtype=np.uint8)
            cv2.putText(placeholder, f"GID {gid}", (2, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)
            cv2.imwrite(str(crops_dir / filename), placeholder)

        records.append({**rec, "filename": filename})

    # Build contact sheet
    sheet_path = _make_contact_sheet(crops_dir, records, cameras)
    print(f"\nCrops saved to   : {crops_dir}")
    print(f"Contact sheet    : {sheet_path}")
    print("\n>>> Open the contact sheet (or individual crop images) in your image viewer.")
    print("    Each image is labelled with its camera and global_id.")
    print("    Assign the SAME person_id number to global_ids that show the SAME real person.\n")

    # Interactive labelling
    annotations: list[dict] = []
    for cam in cameras:
        cam_records = sorted([r for r in records if r["camera"] == cam], key=lambda r: r["global_id"])
        print(f"--- Camera: {cam} ({len(cam_records)} identities) ---")
        for rec in cam_records:
            gid = rec["global_id"]
            img_path = crops_dir / rec["filename"]
            while True:
                pid = input(f"  {cam} global_id={gid:4d}  →  person_id (number, e.g. 1/2/3, or 's' to skip): ").strip()
                if pid.lower() == "s":
                    break
                if pid.isdigit() and int(pid) >= 1:
                    annotations.append({
                        "scenario": args.scenario,
                        "model": args.ref_model,
                        "camera": cam,
                        "global_id": gid,
                        "person_id": int(pid),
                        "notes": "manual",
                    })
                    break
                print("    Please enter a positive integer or 's' to skip.")
        print()

    if not annotations:
        print("No annotations entered — nothing saved.")
        return 0

    # Write / append to annotations.csv
    annot_path = annot_dir / "annotations.csv"
    fieldnames = ["scenario", "model", "camera", "global_id", "person_id", "notes"]
    existing: list[dict] = []
    if annot_path.exists():
        with annot_path.open(encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
        # Remove any prior entries for same ref_model + scenario to avoid duplicates
        existing = [r for r in existing
                    if not (r["model"] == args.ref_model and r["scenario"] == args.scenario)]

    with annot_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(existing)
        w.writerows(annotations)

    print(f"Saved {len(annotations)} annotation rows to {annot_path}")
    print("\nNext step: run the evaluator to auto-propagate to all other models:")
    print(f"  python -m reid_benchmark.evaluate --scenario {args.scenario} --ref-model {args.ref_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
