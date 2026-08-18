"""Unified Search+MTMC pipeline: persons AND vehicles in one pass.

One YOLO detection pass -> class routing:
  person (class 0)          -> person winner: TransReID-SSL (+TTA+smoothing),
                               quality-top-K gallery, IDs namespaced p_*
  vehicle (classes 2,3,5,7) -> vehicle winner: veri_sbs, quality-top-K gallery,
                               IDs namespaced v_*

Renders side-by-side annotated video (persons cyan, vehicles orange) and
writes namespaced track events usable by the text-search index builder
(person index via IRRA, vehicle index via CLIP).

Usage:
    python -m MTMC.unified_pipeline --config MTMC/configs/vehicle.yaml
    python -m MTMC.unified_pipeline --config MTMC/configs/pipelines.yaml  # hospital scene
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from MTMC.adapters import MultiClassDetector, IoUTracker, crop_boxes, load_embedder, l2_normalize  # noqa: E402
from MTMC.gallery import MultiEmbeddingGallery, crop_quality  # noqa: E402
from MTMC.pipelines import load_mtmc_config, _smooth  # noqa: E402

PERSON_CLASSES = {0}
VEHICLE_CLASSES = {2, 3, 5, 7}

PERSON_EMBEDDER = "transreid_ssl"
VEHICLE_EMBEDDER = "veri_sbs"


class _Router:
    """Per-domain tracker+embedder+gallery with namespaced global IDs."""

    def __init__(self, prefix: str, embedder_key: str, threshold: float) -> None:
        self.prefix = prefix
        emb, detail = load_embedder(embedder_key, tta_flip=True)
        if emb is None:
            raise RuntimeError(f"{embedder_key}: {detail}")
        self.embedder = emb
        self.trackers: dict[str, IoUTracker] = {}
        self.gallery = MultiEmbeddingGallery(
            threshold=threshold, policy="quality_topk", k=10, match="min",
            max_age_seconds=180.0,
        )

    def step(self, cam: str, frame, boxes, frame_idx: int, t: float) -> list[tuple]:
        tracker = self.trackers.setdefault(cam, IoUTracker())
        tracks = tracker.update(boxes, frame_idx)
        crops = crop_boxes(frame, [tr.bbox for tr in tracks])
        embs = self.embedder.embed(crops)
        out = []
        for i, tr in enumerate(tracks):
            if i >= len(embs) or embs.shape[1] <= 1:
                continue
            emb = _smooth(tr, embs[i], 5)
            gid, _, _ = self.gallery.match_embedding(emb, t, crop_quality(crops[i]), cam)
            out.append((f"{self.prefix}{gid}", tr.bbox))
        return out


def run(config_path: str, max_frames: int = 0) -> dict:
    config = load_mtmc_config(Path(config_path))
    bench = config["benchmark"]
    labels = config.get("camera_labels", ["ch9", "ch10"])

    detector = MultiClassDetector(bench["detector"], bench["confidence"], bench["iou"],
                                  PERSON_CLASSES | VEHICLE_CLASSES)
    # class-aware detect: need classes per box; extend inline
    yolo = detector._inner.model
    device = detector._inner.device

    def detect(frame):
        res = yolo.predict(frame, conf=bench["confidence"], iou=bench["iou"],
                           verbose=False, device=device)
        persons, vehicles = [], []
        if res and res[0].boxes is not None:
            for b in res[0].boxes:
                cls = int(b.cls.item())
                box = b.xyxy[0].cpu().numpy()
                if cls in PERSON_CLASSES:
                    persons.append(box)
                elif cls in VEHICLE_CLASSES:
                    vehicles.append(box)
        return persons, vehicles

    # thresholds from the domain calibration files
    p_cal = json.loads((_ROOT / "MTMC/reports/calibrated_thresholds.json").read_text())
    v_cal_path = _ROOT / "MTMC/reports/calibrated_thresholds_vehicle.json"
    v_cal = json.loads(v_cal_path.read_text()) if v_cal_path.exists() else {}
    p_thr = p_cal.get(PERSON_EMBEDDER, {}).get("threshold", 0.14)
    v_thr = v_cal.get(VEHICLE_EMBEDDER, {}).get("threshold", 0.56)

    person_router = _Router("p_", PERSON_EMBEDDER, p_thr)
    vehicle_router = _Router("v_", VEHICLE_EMBEDDER, v_thr)

    cap_a = cv2.VideoCapture(str(_ROOT / config["videos"]["ch9_5min"]))
    cap_b = cv2.VideoCapture(str(_ROOT / config["videos"]["ch10_5min"]))
    process_every = max(1, int(bench.get("process_every_n_frames", 1)))

    out_dir = _ROOT / "MTMC" / "outputs" / "unified"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir = _ROOT / "MTMC" / "reports" / "unified"
    rep_dir.mkdir(parents=True, exist_ok=True)
    writer = None
    events = []
    frame_idx = 0
    fps = cap_a.get(cv2.CAP_PROP_FPS) or 30.0
    started = time.perf_counter()

    while True:
        ok_a, fa = cap_a.read()
        ok_b, fb = cap_b.read()
        if not ok_a or not ok_b:
            break
        t = frame_idx * process_every / fps

        rendered = []
        for cam, frame in ((labels[0], fa), (labels[1], fb)):
            persons, vehicles = detect(frame)
            results = (person_router.step(cam, frame, persons, frame_idx, t)
                       + vehicle_router.step(cam, frame, vehicles, frame_idx, t))
            img = frame.copy()
            for gid, bbox in results:
                x1, y1, x2, y2 = bbox.astype(int)
                color = (255, 200, 40) if gid.startswith("p_") else (40, 140, 255)
                cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
                cv2.putText(img, gid, (x1, max(20, y1 - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
                events.append({"frame": frame_idx, "camera": cam, "global_id": gid,
                               "x1": round(float(x1), 1), "y1": round(float(y1), 1),
                               "x2": round(float(x2), 1), "y2": round(float(y2), 1)})
            cv2.putText(img, cam, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 220, 255), 2)
            h = 540
            rendered.append(cv2.resize(img, (int(img.shape[1] * h / img.shape[0]), h)))

        combined = np.hstack(rendered)
        cv2.putText(combined, "UNIFIED: persons (cyan) + vehicles (orange)",
                    (12, combined.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        if writer is None:
            writer = cv2.VideoWriter(str(out_dir / "unified_demo.mp4"),
                                     cv2.VideoWriter_fourcc(*"mp4v"), 3.0,
                                     (combined.shape[1], combined.shape[0]))
        writer.write(combined)

        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"unified: {frame_idx} frames | p_gallery={len(person_router.gallery.gallery)} "
                  f"v_gallery={len(vehicle_router.gallery.gallery)}", flush=True)
        if max_frames and frame_idx >= max_frames:
            break
        for _ in range(process_every - 1):
            if not (cap_a.grab() and cap_b.grab()):
                break

    if writer:
        writer.release()
    cap_a.release(); cap_b.release()

    with (rep_dir / "unified_track_events.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(events[0].keys()))
        w.writeheader(); w.writerows(events)

    summary = {
        "frames": frame_idx,
        "elapsed_s": round(time.perf_counter() - started, 1),
        "person_gallery": person_router.gallery.stats(),
        "vehicle_gallery": vehicle_router.gallery.stats(),
        "video": str(out_dir / "unified_demo.mp4"),
    }
    (rep_dir / "unified_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="MTMC/configs/vehicle.yaml")
    ap.add_argument("--max-frames", type=int, default=0)
    args = ap.parse_args()
    run(args.config, args.max_frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
