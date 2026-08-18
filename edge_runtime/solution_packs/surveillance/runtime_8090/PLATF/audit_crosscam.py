"""Cross-camera link audit via the FACE oracle (non-circular).

The re-id links cameras using the APPEARANCE embedding, so appearance agreement cannot
prove a cross-cam link is real (it is what formed the link). FACE is an INDEPENDENT
modality -- if two cameras' detections of one plugin gid carry faces that agree, the link
is real; if the faces clearly disagree, it is a false merge. Faces are sparse here
(back-view), so many links are unadjudicated -- reported honestly, not hidden.

Join: obs stream (face_emb per camera/frame/local_id) x plugin events CSV (gid per
camera/frame/track_id). Group faces per (gid, camera), score min cross-camera face
distance per multi-cam gid.

    python -m PLATF.audit_crosscam --obs obs_all.jsonl --events platf.csv [--face-thr 0.8045]
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict

import numpy as np


def _cos_dist(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 999.0
    return 1.0 - float(np.dot(a, b) / (na * nb))


def _valid(fe):
    return fe is not None and float(np.linalg.norm(np.asarray(fe, np.float32))) > 1e-6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--face-thr", type=float, default=0.8045)
    a = ap.parse_args()

    # (camera, frame, local_id) -> face_emb
    face_by_key = {}
    with open(a.obs, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            fe = d.get("face_emb")
            k = (str(d["camera"]), str(d["frame"]), str(int(d["local_id"])))
            face_by_key[k] = np.asarray(fe, np.float32) if _valid(fe) else None

    gid_cam_obs = defaultdict(lambda: defaultdict(int))          # gid -> camera -> n
    gid_cam_faces = defaultdict(lambda: defaultdict(list))       # gid -> camera -> [face]
    with open(a.events, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gid = int(r["global_id"])
            cam = str(r["camera"])
            gid_cam_obs[gid][cam] += 1
            fe = face_by_key.get((cam, str(r["frame"]), str(r["track_id"])))
            if fe is not None:
                gid_cam_faces[gid][cam].append(fe)

    multi = sorted([g for g, cams in gid_cam_obs.items() if len(cams) > 1])
    print(f"cross-cam gids = {len(multi)}   face_thr(dist) = {a.face_thr}\n")
    n_conf = n_contra = n_noface = 0
    for g in multi:
        cams = sorted(gid_cam_obs[g])
        counts = ",".join(f"{c}:{gid_cam_obs[g][c]}" for c in cams)
        face_cams = [c for c in cams if gid_cam_faces[g].get(c)]
        best = 999.0
        for i in range(len(face_cams)):
            for j in range(i + 1, len(face_cams)):
                for fa in gid_cam_faces[g][face_cams[i]]:
                    for fb in gid_cam_faces[g][face_cams[j]]:
                        best = min(best, _cos_dist(fa, fb))
        if len(face_cams) < 2:
            verdict = "no-face (unadjudicated)"
            n_noface += 1
        elif best < a.face_thr:
            verdict = f"CONFIRMED  (face d={best:.3f})"
            n_conf += 1
        else:
            verdict = f"CONTRADICTED (face d={best:.3f})"
            n_contra += 1
        print(f"  gid {g:5d}  cams[{counts}]  face_cams={len(face_cams)}  -> {verdict}")

    print(f"\nSUMMARY  confirmed={n_conf}  contradicted={n_contra}  "
          f"no-face={n_noface}  / {len(multi)} cross-cam links")
    print("(no-face links are not judged either way -- back-view footage, sparse faces)")


if __name__ == "__main__":
    main()
