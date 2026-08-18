"""N-camera MTMC runner with a shared gallery, topology gate, grid video, and
FAISS vector-DB export.

One YOLO + TransReID-SSL embedder, per-camera IoU trackers, ONE shared
MultiEmbeddingGallery (quality-top-K) with the topology / transition-time gate
(per-pair windows: overlapping pairs -> min 0, learned pairs -> learned window,
others -> concurrent-exclusion default). Renders a grid video of all cameras and,
at the end, exports the gallery to a local FAISS index + metadata + thumbnails.

Usage:
    python -m MTMC.multicam_pipeline --config MTMC/configs/multicam_5.yaml
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import yaml

from MTMC.adapters import MultiClassDetector, IoUTracker, crop_boxes, load_embedder, l2_normalize
from MTMC.gallery import MultiEmbeddingGallery, crop_quality
from MTMC.fusion_gallery import MultiModalGallery

_ROOT = Path(__file__).resolve().parent.parent


def _smooth(track, emb, window=5):
    buf = getattr(track, "_mtmc_buf", None)
    if buf is None:
        buf = []
        track._mtmc_buf = buf
    buf.append(emb)
    if len(buf) > window:
        buf.pop(0)
    return emb if len(buf) < 2 else l2_normalize(np.mean(np.stack(buf), axis=0))


def _grid(frames: list, labels: list, cell=(640, 360), cols=3) -> np.ndarray:
    rows = (len(frames) + cols - 1) // cols
    canvas = np.zeros((rows * cell[1], cols * cell[0], 3), dtype=np.uint8)
    for i, (fr, lab) in enumerate(zip(frames, labels)):
        r, c = divmod(i, cols)
        cell_img = cv2.resize(fr, cell)
        cv2.putText(cell_img, lab, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 220, 255), 2)
        canvas[r * cell[1]:(r + 1) * cell[1], c * cell[0]:(c + 1) * cell[0]] = cell_img
    return canvas


def run(config_path: str) -> dict:
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    cams = config["cameras"]
    bench = config["benchmark"]
    out_dir = _ROOT / config["paths"]["out_dir"]
    # clear stale per-run artifacts so outputs never mix IDs across runs
    # (the in-memory gallery/trackers are already fresh; this cleans the FILES).
    import shutil
    for sub in ("crops", "gallery"):
        d = out_dir / sub
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    vid = out_dir / "video" / "mtmc_5cam.mp4"
    if vid.exists():
        try:
            vid.unlink()
        except PermissionError:
            pass  # locked (viewer/previous writer); VideoWriter overwrites it anyway
    (out_dir / "reports").mkdir(parents=True, exist_ok=True)
    (out_dir / "video").mkdir(parents=True, exist_ok=True)
    (out_dir / "crops").mkdir(parents=True, exist_ok=True)
    (out_dir / "gallery").mkdir(parents=True, exist_ok=True)

    # threshold
    calib = json.loads((_ROOT / config["calibration_file"]).read_text(encoding="utf-8"))
    emb_key = config["embedder"]
    threshold = calib.get(emb_key, {}).get("threshold", 0.35)

    # per-pair topology windows: learned file + overlapping pairs (min 0)
    topo = config.get("topology", {})
    windows = {}
    lt_file = _ROOT / topo.get("learned_transitions_file", "") if topo.get("learned_transitions_file") else None
    if lt_file and lt_file.exists():
        windows.update(json.loads(lt_file.read_text(encoding="utf-8")))
    max_age = float(config["gallery"]["max_age_seconds"])
    for a, b in topo.get("overlapping_pairs", []):
        windows[f"{a}|{b}"] = {"learned": True, "min_s": 0.0, "max_s": max_age}

    # OpenVINO backend (Intel NPU/iGPU): swaps the 4 heavy models, devices pinned.
    ovc = config.get("openvino") or {}
    ov_on = bool(ovc.get("enabled"))
    _ov_dir = Path(ovc.get("model_dir", str(_ROOT / "models")))
    def _ovp(name):
        return str(_ov_dir / name)

    if ov_on:
        from MTMC.ov_backends import OVReidEmbedder, OVDetector
        from MTMC.adapters import TTAEmbedder
        _rw = OVReidEmbedder(_ovp(ovc.get("appearance", f"{emb_key}_int8.xml")),
                             device=ovc.get("appearance_device", "NPU"), key=emb_key)
        embedder, backend = TTAEmbedder(_rw, flip=True), _rw.backend
        detector = OVDetector(_ovp(ovc.get("detector", "yolov8n.xml")),
                              bench["confidence"], bench["iou"], set(bench["class_ids"]),
                              device=ovc.get("detector_device", "GPU"))
    else:
        embedder, backend = load_embedder(emb_key, tta_flip=True)
        if embedder is None:
            raise RuntimeError(f"embedder load failed: {backend}")
        detector = MultiClassDetector(bench["detector"], bench["confidence"],
                                      bench["iou"], set(bench["class_ids"]))

    fusion = config.get("fusion", "none")
    use_fusion = fusion != "none"
    face_emb = gait_emb = None
    if use_fusion:
        from MTMC.face_embedder import AdaFaceEmbedder
        if ov_on:
            face_emb = AdaFaceEmbedder(backend="openvino",
                                       ov_xml=_ovp(ovc.get("face", "adaface_ir101_int8.xml")),
                                       ov_device=ovc.get("face_device", "NPU"))
        else:
            face_emb = AdaFaceEmbedder()
        if fusion == "camera_aware_gait":
            from MTMC.gait_embedder import OnlineGaitEmbedder
            if ov_on:
                gait_emb = OnlineGaitEmbedder(backend="openvino",
                                              ov_xml=_ovp(ovc.get("gait", "gaitbase_int8.xml")),
                                              ov_device=ovc.get("gait_device", "NPU"),
                                              ov_seg_xml=_ovp(ovc.get("seg", "yolov8n_seg.xml")),
                                              ov_seg_device=ovc.get("seg_device", "GPU"))
            else:
                gait_emb = OnlineGaitEmbedder()
        # per-modality thresholds from the Stage-2 screens
        s2f = _ROOT / "MTMC" / "reports" / "stage2_face_screen.json"
        face_thr = json.loads(s2f.read_text()).get("adaface_ir101", {}).get("threshold", 0.8045) if s2f.exists() else 0.8045
        s2g = _ROOT / "MTMC" / "reports" / "stage2_gait_screen.json"
        gait_thr = 0.3496
        if s2g.exists():
            gd = json.loads(s2g.read_text()).get("gaitbase_gait3d", {})
            gait_thr = round(1.0 - (gd.get("mean_same_sim", 0.75) + gd.get("mean_diff_sim", 0.55)) / 2.0, 4)
        gallery = MultiModalGallery(
            app_threshold=threshold, strategy=fusion,
            frontal_cameras=tuple(config.get("frontal_cameras", ["ch10"])),
            max_age_seconds=max_age, k=int(config["gallery"]["k"]),
            face_threshold=face_thr, gait_threshold=gait_thr,
            topology=bool(topo.get("enabled", True)),
            topo_min_transition_s=float(topo.get("min_transition_s", 5.0)),
            topo_max_transition_s=float(topo.get("max_transition_s", max_age)),
            learned_transitions=windows,
        )
    else:
        gallery = MultiEmbeddingGallery(
            threshold=threshold, max_age_seconds=max_age,
            policy=config["gallery"]["policy"], k=int(config["gallery"]["k"]),
            match=config["gallery"]["match"],
            topology=bool(topo.get("enabled", True)),
            topo_min_transition_s=float(topo.get("min_transition_s", 5.0)),
            topo_max_transition_s=float(topo.get("max_transition_s", max_age)),
            learned_transitions=windows,
        )

    caps = {c["label"]: cv2.VideoCapture(str(_ROOT / c["video"])) for c in cams}
    fps = 25.0
    pe = int(bench.get("process_every_n_frames", 10))
    labels = [c["label"] for c in cams]

    # --- TIME ALIGNMENT ---
    # The NVR clips start at slightly different wall-clock times (encoded in the
    # filename: NVR_chX_main_YYYYMMDDHHMMSS_...). Processing by raw frame index
    # would treat frame 0 of every camera as simultaneous, which is wrong by up
    # to 7s and corrupts the topology/transition-time gate. Seek each camera so
    # they all BEGIN at the same real moment (the latest start), then a shared
    # frame_idx maps to the same real time across cameras.
    import re as _re
    def _start_epoch(video_path: str) -> float:
        m = _re.search(r"_(\d{14})_", Path(video_path).name)
        if not m:
            return 0.0
        import datetime
        return datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").timestamp()
    if config.get("time_alignment", False):
        starts = {c["label"]: _start_epoch(c["video"]) for c in cams}
        latest = max(starts.values())
        align_skip = {lab: int(round((latest - starts[lab]) * fps)) for lab in labels}
        for lab in labels:
            if align_skip[lab] > 0:
                caps[lab].set(cv2.CAP_PROP_POS_FRAMES, align_skip[lab])
        print(f"time alignment (frames skipped to sync real start {latest:.0f}): {align_skip}", flush=True)

    app_track = bool(config.get("appearance_tracker", False))
    if app_track:
        from MTMC.appearance_tracker import AppearanceIoUTracker
        trackers = {c["label"]: AppearanceIoUTracker() for c in cams}
    else:
        trackers = {c["label"]: IoUTracker() for c in cams}

    writer = None
    events = []
    best_crop = {}   # global_id -> (quality, crop, camera, frame)
    # tracklet-level ID lock with hysteresis: a continuous within-camera tracklet
    # keeps its global_id; a per-frame gallery match to a DIFFERENT id only wins
    # if it persists for HYST consecutive sampled frames. Prevents the frame-by-frame
    # id oscillation (e.g. doctor flipping between his own id and a neighbour's).
    tracklet_gid: dict = {}       # (camera, local_id) -> locked global_id
    tracklet_pending: dict = {}   # (camera, local_id) -> (challenger_gid, count)
    HYST = int(config.get("gid_hysteresis_frames", 4))
    frame_idx = 0
    started = time.perf_counter()
    max_frames = int(bench.get("max_frames", 0) or 0)

    def color(gid):
        return ((gid * 37) % 255, (gid * 17) % 255, (gid * 97) % 255)

    import time as _time, os as _os
    from collections import defaultdict as _dd
    _PROF = bool(_os.environ.get("PROFILE"))
    _PT, _PN = _dd(float), _dd(int)
    def _acc(k, t0):
        if _PROF:
            _PT[k] += _time.perf_counter() - t0; _PN[k] += 1
    _loop_start = _time.perf_counter()

    while True:
        _t0 = _time.perf_counter()
        reads = {lab: caps[lab].read() for lab in labels}
        _acc("decode", _t0)
        if not all(ok for ok, _ in reads.values()):
            break
        t = frame_idx * pe / fps
        vis_frames = []
        for lab in labels:
            frame = reads[lab][1]
            _t0 = _time.perf_counter(); boxes = detector.detect(frame); _acc("detect", _t0)
            if app_track:
                # embed ALL detections first, then appearance-aware association
                _t0 = _time.perf_counter(); det_crops = crop_boxes(frame, boxes); _acc("crop", _t0)
                _t0 = _time.perf_counter(); det_embs = embedder.embed(det_crops) if det_crops else np.empty((0, 1)); _acc("appearance", _t0)
                _t0 = _time.perf_counter(); tracks = trackers[lab].update(boxes, det_embs, frame_idx); _acc("track", _t0)
            else:
                _t0 = _time.perf_counter(); tracks = trackers[lab].update(boxes, frame_idx); _acc("track", _t0)
            _t0 = _time.perf_counter(); crops = crop_boxes(frame, [tr.bbox for tr in tracks]); _acc("crop", _t0)
            # gallery appearance emb: reuse tracker's current-frame emb if available
            if app_track and all(getattr(tr, "cur_embedding", None) is not None for tr in tracks):
                embs = np.stack([tr.cur_embedding for tr in tracks]) if tracks else np.empty((0, 1))
            else:
                _t0 = _time.perf_counter(); embs = embedder.embed(crops) if crops else np.empty((0, 1)); _acc("appearance", _t0)
            _t0 = _time.perf_counter(); faces = face_emb.embed(crops) if (use_fusion and crops) else None; _acc("face", _t0)
            _t0 = _time.perf_counter(); gaits = gait_emb.embed_tracks(frame, tracks, lab) if (gait_emb and tracks) else None; _acc("gait", _t0)
            vis = frame.copy()
            for i, tr in enumerate(tracks):
                if i >= len(embs) or embs.shape[1] <= 1:
                    continue
                q = crop_quality(crops[i])
                sm = _smooth(tr, embs[i])
                fe = ge = None
                if use_fusion:
                    fe = faces[i] if faces is not None and i < len(faces) else None
                    ge = gaits[i] if gaits is not None and i < len(gaits) else None
                    raw_gid, _ = gallery.match(sm, fe, lab, t, ge)
                else:
                    raw_gid, _, _ = gallery.match_embedding(sm, t, q, lab)

                # --- tracklet-level ID lock with hysteresis ---
                key = (lab, tr.local_id)
                locked = tracklet_gid.get(key)
                if locked is None:
                    tracklet_gid[key] = raw_gid
                    gid = raw_gid
                elif raw_gid == locked:
                    tracklet_pending.pop(key, None)
                    gid = locked
                else:
                    cg, cnt = tracklet_pending.get(key, (raw_gid, 0))
                    cnt = cnt + 1 if cg == raw_gid else 1
                    tracklet_pending[key] = (raw_gid, cnt)
                    if cnt >= HYST:                     # sustained challenge -> accept switch
                        tracklet_gid[key] = raw_gid
                        tracklet_pending.pop(key, None)
                        gid = raw_gid
                    else:                               # keep locked id; enrich ITS entry
                        gid = locked
                        if use_fusion:
                            gallery.force_assign(locked, sm, fe, lab, t, ge)
                        else:
                            gallery.force_assign(locked, sm, t, q, lab)

                tr.global_id = gid
                if q > best_crop.get(gid, (0,))[0]:
                    best_crop[gid] = (q, crops[i].copy(), lab, frame_idx)
                x1, y1, x2, y2 = tr.bbox.astype(int)
                cv2.rectangle(vis, (x1, y1), (x2, y2), color(gid), 3)
                cv2.putText(vis, f"G{gid}", (x1, max(20, y1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color(gid), 2)
                events.append({"frame": frame_idx, "camera": lab, "local_id": tr.local_id,
                               "global_id": gid, "x1": round(float(x1), 1), "y1": round(float(y1), 1),
                               "x2": round(float(x2), 1), "y2": round(float(y2), 1)})
            vis_frames.append(vis)

        _t0 = _time.perf_counter()
        grid = _grid(vis_frames, labels)
        cv2.putText(grid, f"MTMC 5-cam | live IDs: {len(gallery.gallery)} | total: {gallery.next_global_id-1}",
                    (10, grid.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        if writer is None:
            writer = cv2.VideoWriter(str(out_dir / "video" / "mtmc_5cam.mp4"),
                                     cv2.VideoWriter_fourcc(*"mp4v"),
                                     float(bench["output_fps"]), (grid.shape[1], grid.shape[0]))
        writer.write(grid)
        _acc("render", _t0)

        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"{frame_idx} frames | live={len(gallery.gallery)} total={gallery.next_global_id-1} "
                  f"topo_rej={gallery.topo_rejections}", flush=True)
        if max_frames and frame_idx >= max_frames:
            break
        _t0 = _time.perf_counter()
        for lab in labels:
            for _ in range(pe - 1):
                caps[lab].grab()
        _acc("decode", _t0)

    if writer:
        writer.release()
    for c in caps.values():
        c.release()

    if _PROF:
        _wall = _time.perf_counter() - _loop_start
        _steps = max(1, frame_idx)
        _order = ["decode", "detect", "crop", "appearance", "track", "face", "gait", "render"]
        _summed = sum(_PT.values())
        _rest = max(0.0, _wall - _summed)
        print("\n==== PER-STAGE PROFILE (PROFILE=1) ====", flush=True)
        print(f"{'stage':<14}{'total_s':>10}{'ms/frame-step':>16}{'% wall':>9}{'calls':>9}", flush=True)
        for k in _order + [x for x in _PT if x not in _order]:
            if k in _PT:
                print(f"{k:<14}{_PT[k]:>10.1f}{_PT[k]/_steps*1000:>16.2f}{_PT[k]/_wall*100:>8.1f}%{_PN[k]:>9}", flush=True)
        print(f"{'fusion+rest':<14}{_rest:>10.1f}{_rest/_steps*1000:>16.2f}{_rest/_wall*100:>8.1f}%{'':>9}", flush=True)
        print(f"{'TOTAL wall':<14}{_wall:>10.1f}{_wall/_steps*1000:>16.2f}{100.0:>8.1f}%   frame-steps={_steps}", flush=True)
        print(f"throughput: {_steps/_wall:.2f} frame-steps/s over {len(labels)} cams "
              f"= {_steps*len(labels)/_wall:.1f} cam-frames/s", flush=True)

    # ---- write events + telemetry ----
    with (out_dir / "reports" / "track_events.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(events[0].keys())); w.writeheader(); w.writerows(events)
    if gallery.telemetry:
        with (out_dir / "reports" / "gallery_telemetry.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(gallery.telemetry[0].keys()))
            w.writeheader(); w.writerows(gallery.telemetry)

    # ---- export gallery to FAISS + metadata + thumbnails ----
    faiss_info = _export_faiss(gallery, best_crop, out_dir, backend, emb_key)

    elapsed = time.perf_counter() - started
    # per-camera / cross-camera stats
    gid_cams = defaultdict(set)
    for e in events:
        gid_cams[e["global_id"]].add(e["camera"])
    xcam = sum(1 for c in gid_cams.values() if len(c) > 1)
    summary = {
        "config": config_path, "backend": backend, "cameras": labels,
        "frames_per_camera": frame_idx, "elapsed_s": round(elapsed, 1),
        "avg_fps": round(frame_idx * len(labels) / elapsed, 2),
        "total_ids_created": gallery.next_global_id - 1,
        "live_ids_at_end": len(gallery.gallery),
        "cross_camera_ids": xcam,
        "topology_rejections": gallery.topo_rejections,
        "gallery_stats": gallery.stats(),
        "faiss": faiss_info,
        "video": str(out_dir / "video" / "mtmc_5cam.mp4"),
    }
    (out_dir / "reports" / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    return summary


def _export_faiss(gallery, best_crop, out_dir: Path, backend: str, emb_key: str) -> dict:
    """Store every gallery embedding in a FAISS index (cosine via inner product
    on L2-normalized vectors) with a parallel global_id map + per-ID metadata +
    thumbnails. This is the local vector-DB the questions ask about."""
    import faiss

    vectors, row_gid = [], []
    meta = {}
    for gid, entry in gallery.gallery.items():
        app_embs = getattr(entry, "embeddings", None) or getattr(entry, "app_embs", [])
        for v in app_embs:
            vectors.append(np.asarray(v, dtype=np.float32))
            row_gid.append(gid)
        # thumbnail
        thumb_path = ""
        if gid in best_crop:
            thumb_path = str(out_dir / "crops" / f"g{gid:04d}.jpg")
            cv2.imwrite(thumb_path, best_crop[gid][1])
        meta[str(gid)] = {
            "global_id": gid,
            "cameras": sorted(entry.camera_set),
            "n_app_embeddings": len(app_embs),
            "n_face_embeddings": len(getattr(entry, "face_embs", [])),
            "n_gait_embeddings": len(getattr(entry, "gait_embs", [])),
            "seen_count": entry.seen_count,
            "first_seen_s": round(getattr(entry, "created_time", 0.0), 1),
            "last_seen_s": round(entry.last_seen_time, 1),
            "camera_last_seen": {c: round(t, 1) for c, t in getattr(entry, "camera_last_seen", {}).items()},
            "thumbnail": thumb_path,
        }
    if not vectors:
        return {"status": "empty"}
    mat = np.vstack(vectors).astype(np.float32)
    faiss.normalize_L2(mat)  # ensure unit norm (cosine == inner product)
    dim = mat.shape[1]
    index = faiss.IndexFlatIP(dim)          # exact; swap for IVF/HNSW at >50k vectors
    index = faiss.IndexIDMap2(index)
    index.add_with_ids(mat, np.array(row_gid, dtype=np.int64))
    faiss.write_index(index, str(out_dir / "gallery" / "gallery.faiss"))
    (out_dir / "gallery" / "gallery_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    np.save(out_dir / "gallery" / "row_global_ids.npy", np.array(row_gid, dtype=np.int64))
    return {
        "status": "ok", "backend": backend, "embedder": emb_key,
        "index_type": "IndexIDMap2(IndexFlatIP)", "dim": dim,
        "vectors_stored": int(mat.shape[0]), "unique_ids": len(meta),
        "avg_vectors_per_id": round(mat.shape[0] / len(meta), 2),
        "index_file": str(out_dir / "gallery" / "gallery.faiss"),
        "meta_file": str(out_dir / "gallery" / "gallery_meta.json"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="MTMC/configs/multicam_5.yaml")
    args = ap.parse_args()
    run(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
