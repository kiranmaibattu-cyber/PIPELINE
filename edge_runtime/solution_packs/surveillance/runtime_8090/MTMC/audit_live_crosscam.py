"""Visual cross-camera audit for the live dashboards.

Why this exists: every automatic number available for the live pipeline measures
RECALL. The duplicate-stream control (one clip fed as two streams) scores a merge
as correct by construction, and match_rate / multi_cam_ids only count links, never
check them. A change that merges more aggressively improves all of them while
precision collapses -- which is exactly what happened once in this project
(92.3% reported, 22% real).

The only honest check is looking at the crops. This makes that cheap and
repeatable so it gets run on every change instead of only when someone insists.

Usage:
    python3 -m MTMC.audit_live_crosscam --port 8081 --out /tmp/audit
    # then LOOK at /tmp/audit/board_*.jpg and count same/different by eye

Output: one crop pair per identity that appears in two different cameras, laid
out big enough to judge clothing and build, plus a summary of which camera pairs
the links come from.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections import defaultdict

import cv2
import numpy as np


def api(port: int, path: str, timeout: float = 10.0):
    with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=timeout) as r:
        return json.load(r)


def crop(port: int, pid: int, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api/crop/{pid}", timeout=timeout) as r:
            b = r.read()
        return b if len(b) > 500 else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--out", default="/tmp/audit")
    ap.add_argument("--max-gid", type=int, default=400)
    ap.add_argument("--per-board", type=int, default=3, help="identity pairs per output image")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    for f in os.listdir(args.out):
        if f.endswith(".jpg"):
            os.remove(os.path.join(args.out, f))

    meta = api(args.port, "/api/metrics")
    # sid -> camera label, taken from the source filename (NVR_<cam>_...)
    sid_cam: dict[int, str] = {}
    for s in meta["streams"]:
        src = os.path.basename(s.get("source", ""))
        cam = src.split("NVR_")[-1].split("_")[0] if "NVR_" in src else f"sid{s['id']}"
        sid_cam[int(s["id"])] = cam

    # DISTINCT cameras only: duplicate copies of one clip share a camera label, and
    # a "link" between two copies of the same video is not a cross-camera link.
    by_cam: dict[str, int] = {}
    for sid, cam in sorted(sid_cam.items()):
        by_cam.setdefault(cam, sid)
    sids = sorted(by_cam.values())
    print(f"cameras: {by_cam}")
    if len(sids) < 2:
        print("need at least two distinct cameras")
        return 1

    # CROP_STORE keys are pid = sid * 100000 + gid, one stored crop per (stream, id)
    seen: dict[int, dict[int, bytes]] = defaultdict(dict)
    for gid in range(1, args.max_gid + 1):
        for sid in sids:
            b = crop(args.port, sid * 100000 + gid)
            if b:
                seen[gid][sid] = b

    shared = {g: d for g, d in seen.items() if len(d) >= 2}
    print(f"identities seen in >=2 distinct cameras: {len(shared)}")
    if not shared:
        return 0

    pair_counts: dict[str, int] = defaultdict(int)
    cells = []
    for gid in sorted(shared):
        entries = sorted(shared[gid].items())[:2]
        (sa, ba), (sb, bb) = entries[0], entries[1]
        pair_counts["|".join(sorted((sid_cam[sa], sid_cam[sb])))] += 1
        imgs = []
        for blob in (ba, bb):
            im = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
            imgs.append(cv2.resize(im, (185, 370)))
        pad = np.full((370, 12, 3), 255, np.uint8)
        body = np.hstack([imgs[0], pad, imgs[1]])
        lab = np.full((24, body.shape[1], 3), 255, np.uint8)
        cv2.putText(lab, f"P{gid}  {sid_cam[sa]}|{sid_cam[sb]}", (4, 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)
        cells.append(np.vstack([lab, body]))

    n = 0
    for i in range(0, len(cells), args.per_board):
        cv2.imwrite(os.path.join(args.out, f"board_{n}.jpg"), np.hstack(cells[i:i + args.per_board]))
        n += 1

    print(f"wrote {n} boards to {args.out}")
    print("camera pairs:", dict(sorted(pair_counts.items(), key=lambda kv: -kv[1])))
    print("NOW LOOK AT THE BOARDS. Count same / different by eye -- no number here is precision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
