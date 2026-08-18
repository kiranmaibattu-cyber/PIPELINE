"""Prepare MTID for the vehicle tournament.

1. Render synchronized mp4s from the Drone and Infrastructure frame folders
   (first --n-frames frames, both views cover the same intersection).
2. Parse the chunked annotations.csv files (polygons + persistent Object IDs)
   into ground-truth events (frame, camera, gt_id, bbox) + a GT annotations
   file mapping (camera, gt_id) -> vehicle person_id. Object IDs are treated
   per-view; cross-view identity uses the shared ID space if consistent.

Usage:  python -m MTMC.vehicle.prep_mtid --n-frames 7500
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2

_ROOT = Path(__file__).resolve().parent.parent.parent
_MTID = _ROOT / "models" / "mtid"
_OUT = _ROOT / "MTMC" / "vehicle" / "data"


def render_video(view: str, n_frames: int, fps: float = 30.0) -> Path:
    frames_dir = _MTID / view / "frames"
    prefix = "seq3-drone" if view == "Drone" else "seq3-infra"
    out = _OUT / f"{view.lower()}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    written = 0
    for i in range(1, n_frames + 1):
        p = frames_dir / f"{prefix}_{i:07d}.jpg"
        if not p.exists():
            continue
        img = cv2.imread(str(p))
        if img is None:
            continue
        if writer is None:
            writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"),
                                     fps, (img.shape[1], img.shape[0]))
        writer.write(img)
        written += 1
    if writer:
        writer.release()
    print(f"{view}: {written} frames -> {out}")
    return out


def parse_annotations(view: str, n_frames: int) -> None:
    """Chunk dirs (0, 1000, ...) each hold annotations.csv rows:
    maskfile;rgbfile;ObjectID;Tag;Occluded;<flag>;x1 y1 x2 y2 ... (polygon)"""
    cam = "drone" if view == "Drone" else "infra"
    rows_out: list[dict] = []
    ids = set()
    for chunk in sorted((_MTID / view).iterdir()):
        if not chunk.is_dir() or chunk.name == "frames":
            continue
        ann = chunk / "annotations.csv"
        if not ann.exists():
            continue
        with ann.open(encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter=";")
            header = next(reader, None)
            for parts in reader:
                if len(parts) < 7:
                    continue
                rgb = parts[1]
                try:
                    frame_no = int(rgb.split("_")[-1].split(".")[0])
                except ValueError:
                    continue
                if frame_no > n_frames:
                    continue
                gt_id = parts[2]
                tag = parts[3]
                poly = parts[-1].split()
                try:
                    xs = [float(v) for v in poly[0::2]]
                    ys = [float(v) for v in poly[1::2]]
                except ValueError:
                    continue
                if not xs or not ys:
                    continue
                rows_out.append({
                    "frame": frame_no, "camera": cam, "gt_id": gt_id, "tag": tag,
                    "x1": round(min(xs), 1), "y1": round(min(ys), 1),
                    "x2": round(max(xs), 1), "y2": round(max(ys), 1),
                })
                ids.add(gt_id)
    out = _OUT / f"gt_events_{cam}.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["frame", "camera", "gt_id", "tag",
                                          "x1", "y1", "x2", "y2"])
        w.writeheader()
        w.writerows(rows_out)
    print(f"{view}: {len(rows_out)} GT boxes, {len(ids)} unique vehicle IDs -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-frames", type=int, default=7500)
    args = ap.parse_args()
    for view in ("Drone", "Infrastructure"):
        render_video(view, args.n_frames)
        parse_annotations(view, args.n_frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
