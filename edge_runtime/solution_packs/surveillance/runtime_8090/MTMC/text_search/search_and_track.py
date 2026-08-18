"""Search + MTMC demo: text query -> global ID -> highlight that person in
BOTH cameras across the full clip.

Renders side-by-side video: queried person in bright green + query text
banner; everyone else dimmed gray boxes.

Usage:
    python -m MTMC.text_search.search_and_track --run <run_id> \
        --model irra "a woman wearing a bright green saree"
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from MTMC.pipelines import load_mtmc_config  # noqa: E402
from MTMC.text_search.search import search  # noqa: E402
from MTMC.text_search.index_builder import build_index  # noqa: E402

_REPORTS = _ROOT / "MTMC" / "reports"
SCENARIO = "cross_camera"


def render(run_id: str, model_key: str, query: str, top_k_ids: int = 1) -> Path:
    index_path = _REPORTS / "text_search" / f"index__{model_key}__{run_id}.npz"
    if not index_path.exists():
        build_index(run_id, model_key)

    ranked = search(index_path, query, top_k=5)
    target_gids = {r["global_id"] for r in ranked[:top_k_ids]}
    print(f"query: {query!r} -> gid(s) {sorted(target_gids)} "
          f"(score {ranked[0]['score']:.3f})")

    # load track events: frame -> camera -> [(gid, bbox)]
    events: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    with (_REPORTS / run_id / f"{SCENARIO}_track_events.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            events[int(row["frame"])][row["camera"]].append(
                (int(row["global_id"]),
                 (float(row["x1"]), float(row["y1"]), float(row["x2"]), float(row["y2"]))))

    config = load_mtmc_config()
    cap_a = cv2.VideoCapture(str(_ROOT / config["videos"]["ch9_5min"]))
    cap_b = cv2.VideoCapture(str(_ROOT / config["videos"]["ch10_5min"]))
    process_every = int(config["benchmark"].get("process_every_n_frames", 10))

    out_dir = _ROOT / "MTMC" / "outputs" / "search_demos"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in query)[:50]
    out_path = out_dir / f"{model_key}__{safe}.mp4"
    writer = None

    frame_idx = 0
    hits = 0
    while True:
        ok_a, fa = cap_a.read()
        ok_b, fb = cap_b.read()
        if not ok_a or not ok_b:
            break

        def draw(img, cam):
            nonlocal hits
            for gid, (x1, y1, x2, y2) in events.get(frame_idx, {}).get(cam, []):
                p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
                if gid in target_gids:
                    cv2.rectangle(img, p1, p2, (0, 255, 80), 4)
                    cv2.putText(img, "MATCH", (p1[0], max(30, p1[1] - 10)),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 80), 3)
                    hits += 1
                else:
                    cv2.rectangle(img, p1, p2, (120, 120, 120), 1)
            cv2.putText(img, cam, (12, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (40, 220, 255), 2)
            return img

        fa, fb = draw(fa, "ch9"), draw(fb, "ch10")
        h = 540
        fa = cv2.resize(fa, (int(fa.shape[1] * h / fa.shape[0]), h))
        fb = cv2.resize(fb, (int(fb.shape[1] * h / fb.shape[0]), h))
        combined = np.hstack([fa, fb])
        cv2.putText(combined, f"QUERY: {query}", (12, combined.shape[0] - 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        if writer is None:
            writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                     float(config["benchmark"]["output_fps"]),
                                     (combined.shape[1], combined.shape[0]))
        writer.write(combined)

        frame_idx += 1
        for _ in range(process_every - 1):
            if not (cap_a.grab() and cap_b.grab()):
                break

    if writer:
        writer.release()
    cap_a.release(); cap_b.release()
    print(f"demo: {out_path} ({hits} highlighted detections)")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", default="irra")
    ap.add_argument("--top-ids", type=int, default=1)
    ap.add_argument("query")
    args = ap.parse_args()
    render(args.run, args.model, args.query, args.top_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
