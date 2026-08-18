"""Replay an observation stream through the platform and score it with honest_metric.

This is the Phase B gate: it proves the platform's re-id plugin path produces the same
kind of per-detection identity output the live two-tier does, scored by the GT-free
`MTMC/honest_metric.py` (merge rate / fragmentation) -- no contaminated labels.

Observation stream = JSONL, one object per detection (a recorded TrackObservation):
    {"camera","local_id","bbox":[x1,y1,x2,y2],"t","frame","quality",
     "app_emb":[...], "face_emb":[...]?, "gait_emb":[...]?}

Run:
    python -m PLATF.replay --stream obs.jsonl --gallery fake --out events.csv
    python -m PLATF.replay --stream obs.jsonl --gallery real --out events.csv
    python -m MTMC.honest_metric --events events.csv --json score.json

`fake` = cosine-NN test double (no box). `real` = the live MultiModalGallery via
MMGalleryAdapter.from_config (needs the MTMC models/config).
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from PLATF.core import EventBus, PersonStore, PluginHost, TrackObservation
from PLATF.plugins.reid import ReIDPlugin


def _emb(v):
    return np.asarray(v, np.float32) if v is not None else None


def load_stream(path: str) -> list:
    obs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            raw_lid = int(d["local_id"])
            # bind on the break-stable id (backbone reconciliation) when present, so a
            # tracker break does not mint a new person; keep the raw track id for output
            # + the honest_metric join (fragmentation is scored per raw track).
            key_lid = int(d.get("stable_id", raw_lid))
            gid = d.get("gid")
            obs.append(TrackObservation(
                camera=str(d["camera"]), local_id=key_lid,
                bbox=tuple(d["bbox"]), t=float(d["t"]), frame_idx=int(d["frame"]),
                quality=float(d.get("quality", 1.0)),
                gid=int(gid) if gid is not None else None,
                app_emb=_emb(d.get("app_emb")), face_emb=_emb(d.get("face_emb")),
                gait_emb=_emb(d.get("gait_emb")), meta={"raw_lid": raw_lid}))
    return obs


def build_gallery(kind: str):
    """none  -> None: pure identity-INGEST (trust the backbone gid on each obs; no
                      second engine). The live/single-app path.
       fake  -> cosine-NN test double (reconstruct a gid for gid-less streams).
       real  -> the MTMC gallery via MMGalleryAdapter (legacy offline reconstruction)."""
    if kind in ("none", None, ""):
        return None
    if kind == "real":
        from PLATF.plugins.reid_gallery import MMGalleryAdapter
        return MMGalleryAdapter.from_config()
    from PLATF.plugins.reid_gallery import FakeGallery
    return FakeGallery()


def replay(observations: list, gallery, out_csv: str) -> dict:
    """Feed the stream through the plugin host in time order (co-temporal batches so
    the same-frame guard sees co-visible tracks), then write a honest_metric CSV of
    the resulting (camera, frame, track_id, global_id, bbox) per detection."""
    store = PersonStore(max_age_s=1e9)
    bus = EventBus()
    n_ident = [0]
    bus.subscribe("identity", lambda e: n_ident.__setitem__(0, n_ident[0] + 1))
    host = PluginHost(store, bus, [ReIDPlugin(gallery)])

    batches = defaultdict(list)
    for o in observations:
        batches[o.frame_idx].append(o)

    rows = 0
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["camera", "frame", "track_id", "global_id", "x1", "y1", "x2", "y2"])
        for frame in sorted(batches):
            batch = batches[frame]
            host.process(batch)
            for o in batch:
                gid = store.resolve_local(o.camera, o.local_id)
                if gid is None:
                    continue
                x1, y1, x2, y2 = o.bbox
                raw_lid = (o.meta or {}).get("raw_lid", o.local_id)
                w.writerow([o.camera, o.frame_idx, raw_lid, gid, x1, y1, x2, y2])
                rows += 1

    return {"detections": len(observations), "rows_written": rows,
            "persons": len(store.all()), "identity_events": n_ident[0],
            "out": out_csv}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", required=True)
    ap.add_argument("--gallery", choices=["none", "fake", "real"], default="fake")
    ap.add_argument("--out", default="platf_events.csv")
    a = ap.parse_args()
    obs = load_stream(a.stream)
    res = replay(obs, build_gallery(a.gallery), a.out)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
