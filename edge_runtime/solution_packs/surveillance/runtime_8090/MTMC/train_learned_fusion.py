"""Fit logistic-regression fusion weights from propagated MTMC annotations."""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "MTMC" / "reports" / "learned_fusion_weights.json"


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def _load_threshold(embedder: str) -> float:
    p = _ROOT / "MTMC" / "reports" / "calibrated_thresholds.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get(embedder, {}).get("status") == "ok":
            return float(data[embedder]["threshold"])
    from MTMC.adapters import CALIBRATED_THRESHOLDS

    return float(CALIBRATED_THRESHOLDS.get(embedder, 0.35))


def _ensure_annotations(scenario: str) -> Path:
    p = _ROOT / "MTMC" / "reports" / "annotations_mtmc.csv"
    if not p.exists() or p.stat().st_size < 80:
        subprocess.run([sys.executable, "-m", "MTMC.propagate", "--scenario", scenario], cwd=str(_ROOT), check=True)
    return p


def _load_labels(path: Path, model: str, scenario: str) -> dict[tuple[str, int], str]:
    labels = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["model"] == model and row["scenario"] == scenario:
                labels[(row["camera"], int(float(row["global_id"])))] = row["person_id"]
    return labels


def _sample_crops(events_csv: Path, labels: dict[tuple[str, int], str], per_gid: int = 4) -> dict[tuple[str, int], list[np.ndarray]]:
    from MTMC.pipelines import load_mtmc_config

    cfg = load_mtmc_config()
    step = int(cfg["benchmark"].get("process_every_n_frames", 1))
    by_cam_frame: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    with events_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cam = row["camera"]
            gid = int(float(row["global_id"]))
            if (cam, gid) not in labels:
                continue
            key = (cam, gid)
            by_cam_frame[cam][int(row["frame"])].append(
                {
                    "key": key,
                    "box": [float(row["x1"]), float(row["y1"]), float(row["x2"]), float(row["y2"])],
                }
            )

    video_for_cam = {
        "ch9": _ROOT / cfg["videos"]["ch9_5min"],
        "ch10": _ROOT / cfg["videos"]["ch10_5min"],
    }
    crops: dict[tuple[str, int], list[np.ndarray]] = defaultdict(list)
    for cam, frame_rows in by_cam_frame.items():
        cap = cv2.VideoCapture(str(video_for_cam[cam]))
        for frame_idx in sorted(frame_rows):
            wanted = [r for r in frame_rows[frame_idx] if len(crops[r["key"]]) < per_gid]
            if not wanted:
                continue
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx * step)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            for r in wanted:
                x1, y1, x2, y2 = [int(round(v)) for v in r["box"]]
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(w, x2), min(h, y2)
                if y2 - y1 >= 80 and x2 > x1:
                    crops[r["key"]].append(frame[y1:y2, x1:x2].copy())
        cap.release()
    return crops


def fit(run_id: str, scenario: str = "cross_camera", embedder_key: str = "transreid_ssl") -> dict:
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError as exc:
        raise RuntimeError("scikit-learn is required; install with `python -m pip install scikit-learn`.") from exc

    from MTMC.adapters import load_embedder
    from MTMC.face_embedder import AdaFaceEmbedder

    ann = _ensure_annotations(scenario)
    labels = _load_labels(ann, run_id, scenario)
    if len(labels) < 4:
        raise RuntimeError(f"not enough propagated labels for {run_id}: {len(labels)}")

    events_csv = _ROOT / "MTMC" / "reports" / run_id / f"{scenario}_track_events.csv"
    crops = _sample_crops(events_csv, labels)
    embedder, backend = load_embedder(embedder_key, tta_flip=True)
    if embedder is None:
        raise RuntimeError(f"appearance embedder load failed: {backend}")
    face = AdaFaceEmbedder()

    reps = {}
    for key, ims in crops.items():
        if not ims:
            continue
        app = np.vstack([_l2(v) for v in embedder.embed(ims)])
        face_embs = face.embed(ims)
        valid_face = face_embs[np.linalg.norm(face_embs, axis=1) > 1e-6]
        reps[key] = {
            "person_id": labels[key],
            "app": _l2(app.mean(axis=0)),
            "face": _l2(valid_face.mean(axis=0)) if len(valid_face) else None,
        }

    app_thr = _load_threshold(embedder_key)
    face_thr = 0.8045
    face_path = _ROOT / "MTMC" / "reports" / "stage2_face_screen.json"
    if face_path.exists():
        face_thr = json.loads(face_path.read_text(encoding="utf-8")).get("adaface_ir101", {}).get("threshold", face_thr)

    keys = list(reps)
    x, y = [], []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = reps[keys[i]], reps[keys[j]]
            d_app = float(1.0 - np.dot(a["app"], b["app"])) / app_thr
            has_face = a["face"] is not None and b["face"] is not None
            d_face = float(1.0 - np.dot(a["face"], b["face"])) / face_thr if has_face else 0.0
            x.append([d_app, d_face, float(has_face)])
            y.append(int(a["person_id"] == b["person_id"]))

    clf = LogisticRegression(class_weight="balanced", random_state=0, max_iter=1000)
    clf.fit(np.asarray(x, dtype=np.float32), np.asarray(y, dtype=np.int32))
    probs = clf.predict_proba(np.asarray(x, dtype=np.float32))[:, 1]
    pred = probs >= 0.5
    acc = float((pred == np.asarray(y, dtype=bool)).mean())

    result = {
        "status": "ok",
        "source_run_id": run_id,
        "embedder": embedder_key,
        "backend": backend,
        "features": ["normalized_app_distance", "normalized_face_distance_or_0", "has_face_flag"],
        "coef": [float(v) for v in clf.coef_[0]],
        "intercept": float(clf.intercept_[0]),
        "training_accuracy": round(acc, 4),
        "n_pairs": len(y),
        "n_positive": int(sum(y)),
        "n_negative": int(len(y) - sum(y)),
        "n_track_representatives": len(reps),
        "app_threshold": app_thr,
        "face_threshold": face_thr,
    }
    _OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="transreid_ssl__cross_camera__iou__gquality_topk10__f_camera_aware")
    ap.add_argument("--scenario", default="cross_camera")
    ap.add_argument("--embedder", default="transreid_ssl")
    args = ap.parse_args()
    try:
        fit(args.run_id, args.scenario, args.embedder)
        return 0
    except Exception as exc:  # noqa: BLE001
        result = {"status": "skipped", "reason": str(exc)[:300]}
        _OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
