"""Build the person search index from a tournament run's track events.

For each (camera, global_id): take the largest-bbox detection, extract the
crop from the source video, embed with the chosen text-search image encoder.
Only IMAGE embeddings are stored — text is encoded at query time.

Usage:
    python -m MTMC.text_search.index_builder --run <run_id> --model clip_zeroshot
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reid_benchmark.annotate import _best_crop_per_gid, _extract_crop  # noqa: E402
from MTMC.pipelines import load_mtmc_config  # noqa: E402
from MTMC.text_search.models import load_searcher  # noqa: E402

_REPORTS = _ROOT / "MTMC" / "reports"
SCENARIO = "cross_camera"


def build_index(run_id: str, model_key: str, config_path: str = "") -> Path:
    from pathlib import Path as _P
    config = load_mtmc_config(_P(config_path)) if config_path else load_mtmc_config()
    labels = config.get("camera_labels", ["ch9", "ch10"])
    cam_video = {labels[0]: str(_ROOT / config["videos"]["ch9_5min"]),
                 labels[1]: str(_ROOT / config["videos"]["ch10_5min"])}
    process_every = int(config["benchmark"].get("process_every_n_frames", 10))

    events = _REPORTS / run_id / f"{SCENARIO}_track_events.csv"
    best = _best_crop_per_gid(events)

    crops, meta = [], []
    for (cam, gid), det in sorted(best.items()):
        video = cam_video.get(cam)
        if video is None:
            continue
        crop = _extract_crop(video, det["frame"], det["x1"], det["y1"],
                             det["x2"], det["y2"], process_every)
        if crop is None or crop.shape[0] < 60:
            continue
        crops.append(crop)
        meta.append({"camera": cam, "global_id": gid, "frame": det["frame"]})

    searcher = load_searcher(model_key)
    embs = []
    for s in range(0, len(crops), 32):
        embs.append(searcher.encode_images(crops[s:s + 32]))
    embs = np.vstack(embs)

    out_dir = _REPORTS / "text_search"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"index__{model_key}__{run_id}.npz"
    np.savez_compressed(out, embeddings=embs,
                        meta=json.dumps(meta), run_id=run_id, model=model_key,
                        backend=searcher.backend)
    print(f"index: {len(meta)} entries ({searcher.backend}) -> {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", default="clip_zeroshot", choices=["clip_zeroshot", "irra"])
    ap.add_argument("--config", default="")
    args = ap.parse_args()
    build_index(args.run, args.model, args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
