"""Honest, ground-truth-FREE re-id metrics for the MTMC pipeline.

The project's annotations_mtmc.csv is contaminated -- its person_id labels were
majority-voted from the pipeline's OWN global_ids, so every metric scored against
it (IDF1, purity, cross-cam precision in metrics.py) is circular: it rewards the
pipeline for reproducing its own errors and HIDES them.

This tool scores the pipeline's raw per-detection output against PHYSICS and
TRACKING, which need no labels and cannot be gamed:

  1. MERGE RATE  (precision, provable)
     One person cannot occupy two boxes in one camera frame. So a global_id that
     appears in >=2 spatially-distinct boxes (IoU < iou_same) in the same
     (camera, frame) is provably merging different people. A hard LOWER BOUND on
     false merges (only catches co-visible ones; cross-frame/cross-cam merges are
     invisible here -- so the true false-merge rate is >= this).

  2. FRAGMENTATION (recall, provable)
     One continuous local track = one person (short-term tracking is reliable).
     If the pipeline assigns >=2 distinct global_ids across a track's life it has
     over-split one person. Reports clean-track rate + gids-per-track.

  3. GID NOISE
     gid lifespan; singleton/short gids (< solid_frames) = likely detection noise.

Input: a per-detection events CSV. Column names are auto-detected; needs at least
camera, frame, global_id and (for merge) a bbox, (for fragmentation) a local track
id. Emits a JSON + a human summary. These numbers are the baseline the accuracy
track improves -- honestly.

Usage: python -m MTMC.honest_metric --events run_events.csv [--iou-same 0.5]
                                    [--solid-frames 15] [--json out.json]
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

try:
    import numpy as np
except Exception:
    np = None


def _pick(fieldnames, *cands):
    low = {f.lower(): f for f in fieldnames}
    for c in cands:
        if c in low:
            return low[c]
    return None


def _iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def load_events(path: Path):
    if path.is_dir():
        rows, has = [], {"track": False, "box": False}
        files = sorted(p for p in path.glob("*.csv") if not p.name.startswith("face_"))
        if not files:
            raise SystemExit(f"no event *.csv in {path}")
        for fp in files:
            r, h = _load_one(fp)
            rows.extend(r)
            has["track"] |= h["track"]; has["box"] |= h["box"]
        return rows, has
    return _load_one(path)


def _load_one(path: Path):
    with path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        fn = r.fieldnames or []
        c_cam = _pick(fn, "camera", "cam")
        c_frame = _pick(fn, "frame", "frame_idx", "f")
        c_gid = _pick(fn, "global_id", "gid", "global_id_stab")
        c_track = _pick(fn, "track_id", "track", "local_id", "tid")
        c_x1 = _pick(fn, "x1", "bx1", "left")
        c_y1 = _pick(fn, "y1", "by1", "top")
        c_x2 = _pick(fn, "x2", "bx2", "right")
        c_y2 = _pick(fn, "y2", "by2", "bottom")
        c_label = _pick(fn, "label", "lab", "drawn")
        if not (c_cam and c_frame and c_gid):
            raise SystemExit(f"events CSV needs camera, frame, global_id (have {fn})")
        rows = []
        for d in r:
            try:
                gid = int(float(d[c_gid]))
            except (ValueError, KeyError, TypeError):
                continue
            # keep gid<0 (unassigned/T-state) rows -- the merge/frag/noise metrics
            # filter them, but FLICKER needs them (the T->P transition is the visible one)
            rec = {"cam": d[c_cam], "frame": int(float(d[c_frame])), "gid": gid,
                   "label": (d.get(c_label) if c_label else None) or (f"P{gid}" if gid >= 0 else "T")}
            if c_track:
                try: rec["track"] = int(float(d[c_track]))
                except (ValueError, TypeError): rec["track"] = None
            if c_x1 and c_x2:
                try:
                    rec["box"] = (float(d[c_x1]), float(d[c_y1]), float(d[c_x2]), float(d[c_y2]))
                except (ValueError, TypeError):
                    rec["box"] = None
            rows.append(rec)
    return rows, {"track": bool(c_track), "box": bool(c_x1 and c_x2)}


def merge_rate(rows, iou_same=0.5):
    """Provable false-merge lower bound via same (cam, frame) same-gid distinct boxes."""
    by_cf = defaultdict(list)   # (cam, frame) -> [(gid, box)]
    for r in rows:
        by_cf[(r["cam"], r["frame"])].append((r["gid"], r.get("box")))
    merged_gids = set()
    violation_frames = 0
    total_gids = {r["gid"] for r in rows}
    for (_cam, _fr), dets in by_cf.items():
        by_gid = defaultdict(list)
        for gid, box in dets:
            by_gid[gid].append(box)
        for gid, boxes in by_gid.items():
            if len(boxes) < 2:
                continue
            # provable only if the boxes are spatially DISTINCT (not one person
            # double-detected). If no boxes, count multiplicity conservatively.
            distinct = False
            if all(b is not None for b in boxes):
                for i in range(len(boxes)):
                    for j in range(i + 1, len(boxes)):
                        if _iou(boxes[i], boxes[j]) < iou_same:
                            distinct = True
            else:
                distinct = True  # no boxes -> assume distinct (conservative)
            if distinct:
                merged_gids.add(gid)
                violation_frames += 1
    return {
        "total_gids": len(total_gids),
        "provably_merged_gids": len(merged_gids),
        "merge_rate": round(len(merged_gids) / max(1, len(total_gids)), 4),
        "violation_frames": violation_frames,
        "_merged_ids": sorted(merged_gids),
    }


def fragmentation(rows):
    """Provable over-split: distinct gids per continuous local track (want 1)."""
    by_track = defaultdict(set)   # (cam, track) -> {gids}
    have = 0
    for r in rows:
        if r.get("track") is None:
            continue
        have += 1
        by_track[(r["cam"], r["track"])].add(r["gid"])
    if not by_track:
        return {"status": "no_track_id_in_events"}
    n = len(by_track)
    clean = sum(1 for gids in by_track.values() if len(gids) == 1)
    gids_per = [len(g) for g in by_track.values()]
    return {
        "tracks": n,
        "clean_track_rate": round(clean / n, 4),
        "fragmented_tracks": n - clean,
        "mean_gids_per_track": round(sum(gids_per) / n, 3),
        "max_gids_on_one_track": max(gids_per),
    }


def gid_noise(rows, solid_frames=15):
    life = defaultdict(set)      # gid -> {frames}
    cams = defaultdict(set)      # gid -> {cameras}
    for r in rows:
        life[r["gid"]].add((r["cam"], r["frame"]))
        cams[r["gid"]].add(r["cam"])
    spans = {g: len(fr) for g, fr in life.items()}
    singleton = sum(1 for s in spans.values() if s < solid_frames)
    solid = [g for g, s in spans.items() if s >= solid_frames]
    multicam = [g for g, c in cams.items() if len(c) > 1]
    return {
        "total_gids": len(spans),
        "solid_gids": len(solid),
        "singleton_or_short_gids": singleton,
        "noise_fraction": round(singleton / max(1, len(spans)), 4),
        "multi_camera_gids": len(multicam),
        "_multicam_ids": sorted(multicam),
    }


def flicker(rows, iou_link=0.3, max_gap=2, min_len=3, fps=5.0):
    """Measure ID FLICKER the way the eye sees it, independent of the tracker.

    Re-link raw detections across frames by IoU into PHYSICAL trajectories (pure
    geometry, ignoring the pipeline's own track ids), then walk each trajectory and
    count how often the drawn global id changes. Decompose every change:
      - it coincides with a LOCAL-TRACK change -> TRACKER broke (re-acquired as a new
        track, re-id then landed a different gid) = a tracking problem.
      - the local track id held but the gid changed -> RE-ID churn.
    Also reports local-tracks-per-trajectory: >1 means the tracker is fragmenting one
    person (the thing a gids-per-track metric hides)."""
    have = [r for r in rows if r.get("box") is not None and r.get("track") is not None]
    if not have:
        return {"status": "needs box+track"}
    by_cam = defaultdict(list)
    for r in have:
        by_cam[r["cam"]].append(r)
    trajs = []
    for _cam, dets in by_cam.items():
        frames = defaultdict(list)
        for r in dets:
            frames[r["frame"]].append(r)
        active = []
        for fr in sorted(frames):
            cur = frames[fr]
            cands = []
            for di, d in enumerate(cur):
                for ti, a in enumerate(active):
                    if fr - a["last"] > max_gap:
                        continue
                    v = _iou(d["box"], a["box"])
                    if v >= iou_link:
                        cands.append((v, di, ti))
            cands.sort(reverse=True)
            assigned = [False] * len(cur)
            used = set()
            for _v, di, ti in cands:
                if assigned[di] or ti in used:
                    continue
                assigned[di] = True; used.add(ti)
                a, d = active[ti], cur[di]
                a["last"] = fr; a["box"] = d["box"]
                a["seq"].append((d["track"], d.get("label", d["gid"])))
            for di, d in enumerate(cur):
                if not assigned[di]:
                    active.append({"last": fr, "box": d["box"],
                                   "seq": [(d["track"], d.get("label", d["gid"]))]})
            keep = []
            for a in active:
                (trajs if fr - a["last"] > max_gap else keep).append(a["seq"] if fr - a["last"] > max_gap else a)
            active = keep
        for a in active:
            trajs.append(a["seq"])
    trajs = [s for s in trajs if len(s) >= min_len]
    if not trajs:
        return {"status": "no_trajectories"}
    n = len(trajs)
    clean = tracks_sum = changes = tracker_caused = assign_trans = churn = frames_sum = 0
    for seq in trajs:
        tracks_sum += len({t for t, _ in seq})
        frames_sum += len(seq)
        pt = pl = None
        gc = 0
        for t, lab in seq:
            lab = str(lab)
            if pl is not None and lab != pl:
                gc += 1
                if t != pt:
                    tracker_caused += 1                       # re-acquired as a new track
                elif pl.startswith("T") or lab.startswith("T"):
                    assign_trans += 1                          # T<->P: async gid latency
                else:
                    churn += 1                                 # P<a> -> P<b>: real re-id churn
            pt, pl = t, lab
        changes += gc
        if gc == 0:
            clean += 1
    secs = frames_sum / fps
    pc = lambda x: round(100 * x / changes, 1) if changes else None
    return {
        "trajectories": n,
        "clean_trajectories_pct": round(100 * clean / n, 1),
        "mean_local_tracks_per_trajectory": round(tracks_sum / n, 2),
        "label_changes_total": changes,
        "label_changes_per_trajectory": round(changes / n, 2),
        "flicker_per_min": round(changes / secs * 60, 1) if secs else None,
        "pct_TRACKER_break": pc(tracker_caused),
        "pct_TtoP_assign_latency": pc(assign_trans),
        "pct_REID_churn": pc(churn),
    }


def _norm(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else v


def load_faces(path: Path):
    """face_*.csv rows (camera, gid, face=';'-joined floats) -> gid -> cam -> [vec].

    Face is an INDEPENDENT modality from the appearance embedding that does the
    cross-cam linking, so cross-camera face (dis)agreement is a NON-circular check
    of whether a multi-cam gid is really one person."""
    faces = defaultdict(lambda: defaultdict(list))
    if np is None:
        return faces
    files = sorted(path.glob("face_*.csv")) if path.is_dir() else \
        ([path] if path.name.startswith("face_") else [])
    for fp in files:
        with fp.open(encoding="utf-8") as f:
            for d in csv.DictReader(f):
                try:
                    gid = int(float(d["gid"]))
                    v = np.array([float(x) for x in d["face"].split(";")], np.float32)
                except (ValueError, KeyError, TypeError):
                    continue
                if v.size:
                    faces[gid][d["camera"]].append(v)
    return faces


def cross_cam_precision(faces, same_thr=0.5, diff_thr=0.7):
    """For each gid seen (with a face) in >=2 cameras, compare its per-camera mean
    face. maxpair < same_thr -> one person (link confirmed); maxpair > diff_thr ->
    provably different people (FALSE cross-cam merge); between -> ambiguous.
    Two thresholds, gap left deliberately, so borderline faces aren't over-called."""
    if np is None:
        return {"status": "numpy_unavailable"}
    confirmed = disproven = ambiguous = covered = 0
    bad = []
    xdists = []
    # VALIDATION: intra-camera face spread. Same gid + same camera = same person +
    # same view, so its face samples MUST agree. If this spread is high the faces
    # are GARBAGE (back-view / tiny / INT8 noise) and the cross-cam verdict is not
    # trustworthy. Low intra-cam spread + high cross-cam = the oracle is reliable.
    intra = []
    for gid, cams in faces.items():
        for c, vs in cams.items():
            if len(vs) < 2:
                continue
            nv = [_norm(x) for x in vs]
            for i in range(len(nv)):
                for j in range(i + 1, len(nv)):
                    intra.append(1.0 - float(np.dot(nv[i], nv[j])))
    for gid, cams in faces.items():
        means = {c: _norm(np.mean([_norm(x) for x in vs], axis=0))
                 for c, vs in cams.items() if vs}
        if len(means) < 2:
            continue
        covered += 1
        cs = list(means.values())
        maxd = 0.0
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                maxd = max(maxd, 1.0 - float(np.dot(cs[i], cs[j])))
        xdists.append(round(maxd, 3))
        if maxd > diff_thr:
            disproven += 1; bad.append([gid, round(maxd, 3), sorted(means)])
        elif maxd < same_thr:
            confirmed += 1
        else:
            ambiguous += 1
    tot = confirmed + disproven
    return {
        "multicam_gids_with_face_2plus_cams": covered,
        "face_confirmed_same": confirmed,
        "face_disproven_different": disproven,
        "ambiguous": ambiguous,
        "cross_cam_face_precision": round(confirmed / tot, 4) if tot else None,
        "intra_cam_face_spread_mean": round(float(np.mean(intra)), 3) if intra else None,
        "intra_cam_face_spread_p50": round(float(np.median(intra)), 3) if intra else None,
        "cross_cam_dist_distribution": sorted(xdists),
        "_reliable": (float(np.median(intra)) < same_thr) if intra else None,
        "_false_merges": bad,
    }


def score(path: Path, iou_same=0.5, solid_frames=15):
    rows, has = load_events(path)
    assigned = [r for r in rows if r["gid"] >= 0]   # provable merge/frag/noise ignore T-state
    out = {
        "events": len(rows),
        "has_track_id": has["track"],
        "has_bbox": has["box"],
        "merge_precision": merge_rate(assigned, iou_same),
        "fragmentation_recall": fragmentation(assigned),
        "flicker": flicker(rows),           # ALL rows incl. T-state -> visible flicker
        "gid_noise": gid_noise(assigned, solid_frames),
    }
    faces = load_faces(path)
    if faces:
        out["cross_cam_face_oracle"] = cross_cam_precision(faces)
    return out


def main():
    ap = argparse.ArgumentParser(description="Honest, GT-free re-id metrics")
    ap.add_argument("--events", required=True, help="per-detection CSV (camera,frame,global_id,track,x1..y2)")
    ap.add_argument("--iou-same", type=float, default=0.5)
    ap.add_argument("--solid-frames", type=int, default=15)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    res = score(Path(a.events), a.iou_same, a.solid_frames)

    m, fr, nz = res["merge_precision"], res["fragmentation_recall"], res["gid_noise"]
    print("=== HONEST re-id metrics (no ground truth; physics + tracking only) ===")
    print(f"events={res['events']}  bbox={res['has_bbox']}  track_id={res['has_track_id']}")
    print(f"\nPRECISION (provable false merges -- same person can't be 2 boxes in 1 frame):")
    print(f"  provably-merged gids = {m['provably_merged_gids']}/{m['total_gids']}  "
          f"(merge_rate {m['merge_rate']*100:.1f}%)   violation_frames={m['violation_frames']}")
    print(f"  NOTE: lower bound -- only co-visible merges are provable; true rate is higher.")
    if fr.get("status") == "no_track_id_in_events":
        print(f"\nRECALL (fragmentation): SKIPPED -- events have no local track id.")
    else:
        print(f"\nRECALL (provable over-split -- one continuous track = one person):")
        print(f"  clean-track rate = {fr['clean_track_rate']*100:.1f}%  "
              f"({fr['fragmented_tracks']}/{fr['tracks']} tracks over-split)  "
              f"mean gids/track={fr['mean_gids_per_track']}  max={fr['max_gids_on_one_track']}")
    fl = res.get("flicker", {})
    if fl.get("status"):
        print(f"\nFLICKER: {fl['status']}")
    elif fl:
        print(f"\nFLICKER (LABEL changes along IoU-linked physical trajectories -- what the eye sees):")
        print(f"  {fl['trajectories']} trajectories  clean(no change)={fl['clean_trajectories_pct']}%  "
              f"local-tracks/trajectory={fl['mean_local_tracks_per_trajectory']} (>1 = TRACKER fragmenting)")
        print(f"  label changes: {fl['label_changes_total']} total, {fl['label_changes_per_trajectory']}/traj, "
              f"{fl['flicker_per_min']}/min")
        print(f"  CAUSE: TRACKER-break {fl['pct_TRACKER_break']}%  |  "
              f"T->P assign-latency {fl['pct_TtoP_assign_latency']}%  |  re-id churn {fl['pct_REID_churn']}%")
    print(f"\nGID NOISE:")
    print(f"  {nz['solid_gids']} solid / {nz['total_gids']} total gids  "
          f"({nz['noise_fraction']*100:.0f}% singleton/short)   multi-cam gids={nz['multi_camera_gids']}")
    xc = res.get("cross_cam_face_oracle")
    if xc and xc.get("status") != "numpy_unavailable":
        print(f"\nCROSS-CAM face oracle (independent modality -- non-circular):")
        p = xc.get("cross_cam_face_precision")
        print(f"  multi-cam gids with face in >=2 cams = {xc['multicam_gids_with_face_2plus_cams']}")
        print(f"  face-confirmed same = {xc['face_confirmed_same']}   "
              f"face-DISPROVEN (different people merged) = {xc['face_disproven_different']}   "
              f"ambiguous = {xc['ambiguous']}")
        print(f"  cross-cam face precision = {p*100:.1f}%" if p is not None
              else "  cross-cam face precision = n/a (no confirmable pairs)")
        isp = xc.get("intra_cam_face_spread_p50")
        print(f"  VALIDATION intra-cam face spread (same person+view -> must be low): "
              f"median={isp} mean={xc.get('intra_cam_face_spread_mean')}  "
              f"-> faces {'RELIABLE' if xc.get('_reliable') else 'GARBAGE (verdict untrustworthy)'}")
        print(f"  cross-cam dist distribution = {xc.get('cross_cam_dist_distribution')}")
    elif res.get("cross_cam_face_oracle"):
        print("\nCROSS-CAM face oracle: skipped (numpy unavailable)")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
