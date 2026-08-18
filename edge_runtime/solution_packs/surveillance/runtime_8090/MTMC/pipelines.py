"""Parameterized MTMC pipeline runner — one loop, every axis a config knob.

A RunSpec picks: embedder, tracker, TTA, smoothing window, gallery policy,
spatial gate (BEV), post-run re-ranking. Outputs per run (under MTMC/outputs
and MTMC/reports): video, timing CSV, track-events CSV, gallery telemetry CSV,
summary JSON. Metrics are computed separately by MTMC.metrics once
annotations exist.

Usage
-----
    python -m MTMC.pipelines --embedder osnet_ain --scenario cross_camera
    python -m MTMC.pipelines --embedder osnet_ain --scenario cross_camera \
        --gallery-policy ring --gallery-k 5 --gate bev --rerank k_reciprocal
    python -m MTMC.pipelines --stage1            # run the full Stage-1 roster
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

from MTMC.adapters import (
    CALIBRATED_THRESHOLDS,
    MultiClassDetector,
    Track,
    crop_boxes,
    hstack_resize,
    l2_normalize,
    load_embedder,
    make_tracker,
)
from MTMC.gallery import MultiEmbeddingGallery, crop_quality
from MTMC.rerank import merge_ids_by_rerank

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG = Path(__file__).resolve().parent / "configs" / "pipelines.yaml"


@dataclass
class RunSpec:
    embedder: str
    scenario: str = "cross_camera"            # cross_camera | single_delay
    tracker: str = "iou"
    tta_flip: bool = True
    smoothing_window: int = 5
    gallery_policy: str = "ema"               # ema | ring | quality_topk
    gallery_k: int = 5
    gallery_match: str = "min"                # min | mean | top3
    max_age_seconds: float = 180.0
    max_entries: int = 0
    eviction: str = "lru"
    spatial_gate: str = "none"                # none | bev
    rerank: str = "none"                      # none | k_reciprocal | ca_jaccard | aqe
    fusion: str = "none"                      # none | camera_aware | quality | geff | learned | rank | camera_aware_gait
    threshold: float | None = None            # None -> CALIBRATED_THRESHOLDS fallback
    tag: str = ""                             # distinguishes runs of same embedder

    def run_id(self) -> str:
        parts = [self.embedder, self.scenario, self.tracker,
                 f"g{self.gallery_policy}{self.gallery_k if self.gallery_policy != 'ema' else ''}",
                 self.spatial_gate, self.rerank,
                 f"f_{self.fusion}" if self.fusion != "none" else ""]
        if self.tag:
            parts.append(self.tag)
        return "__".join(p for p in parts if p and p != "none")


def _smooth(track: Track, emb: np.ndarray, window: int) -> np.ndarray:
    buf = getattr(track, "_mtmc_buf", None)
    if buf is None:
        buf = []
        track._mtmc_buf = buf  # type: ignore[attr-defined]
    buf.append(emb)
    if len(buf) > window:
        buf.pop(0)
    if len(buf) < 2:
        return emb
    return l2_normalize(np.mean(np.stack(buf), axis=0))


def run_pipeline(spec: RunSpec, config: dict[str, Any], display: bool = False) -> dict[str, Any]:
    bench = config["benchmark"]
    videos = config["videos"]

    embedder, backend = load_embedder(spec.embedder, tta_flip=spec.tta_flip)
    if embedder is None:
        return {"run_id": spec.run_id(), "run_status": "skipped", "reason": backend, **asdict(spec)}

    detector = MultiClassDetector(
        bench["detector"], bench["confidence"], bench["iou"], set(bench["class_ids"])
    )

    # threshold priority: explicit > MTMC calibration (TTA-matched) > legacy > default
    if spec.threshold is not None:
        threshold = spec.threshold
    else:
        mtmc_calib_path = _ROOT / config.get(
            "calibration_file", "MTMC/reports/calibrated_thresholds.json")
        mtmc_calib = {}
        if mtmc_calib_path.exists():
            data = json.loads(mtmc_calib_path.read_text(encoding="utf-8"))
            mtmc_calib = {k: v["threshold"] for k, v in data.items() if v.get("status") == "ok"}
        threshold = mtmc_calib.get(spec.embedder, CALIBRATED_THRESHOLDS.get(spec.embedder, 0.35))
    face = None
    gait = None
    if spec.fusion != "none":
        from MTMC.face_embedder import AdaFaceEmbedder
        from MTMC.fusion_gallery import MultiModalGallery
        try:
            face = AdaFaceEmbedder()
            if spec.fusion == "camera_aware_gait":
                from MTMC.gait_embedder import OnlineGaitEmbedder
                gait = OnlineGaitEmbedder()
        except Exception as exc:  # noqa: BLE001
            return {"run_id": spec.run_id(), "run_status": "skipped",
                    "reason": f"fusion embedder load failed: {exc}", **asdict(spec)}
        face_thr = 0.8045
        stage2_path = _ROOT / "MTMC" / "reports" / "stage2_face_screen.json"
        if stage2_path.exists():
            s2 = json.loads(stage2_path.read_text(encoding="utf-8"))
            face_thr = s2.get("adaface_ir101", {}).get("threshold", face_thr)
        gallery = MultiModalGallery(
            app_threshold=threshold,
            strategy=spec.fusion,
            max_age_seconds=spec.max_age_seconds,
            k=spec.gallery_k,
            face_threshold=face_thr,
        )
    else:
        topo_cfg = config.get("topology", {})
        learned_tr = None
        lt_path = _ROOT / "MTMC" / "reports" / "learned_transitions.json"
        if spec.spatial_gate == "topology" and topo_cfg.get("use_learned", True) and lt_path.exists():
            learned_tr = json.loads(lt_path.read_text(encoding="utf-8"))
        gallery = MultiEmbeddingGallery(
            threshold=threshold,
            max_age_seconds=spec.max_age_seconds,
            policy=spec.gallery_policy,
            k=spec.gallery_k,
            match=spec.gallery_match,
            max_entries=spec.max_entries,
            eviction=spec.eviction,
            topology=(spec.spatial_gate == "topology"),
            topo_min_transition_s=float(topo_cfg.get("min_transition_s", 5.0)),
            topo_max_transition_s=float(topo_cfg.get("max_transition_s", spec.max_age_seconds)),
            overlapping_cameras=bool(topo_cfg.get("overlapping", False)),
            learned_transitions=learned_tr,
        )

    try:
        tracker_a = make_tracker(spec.tracker)
        tracker_b = make_tracker(spec.tracker)
    except RuntimeError as exc:
        return {"run_id": spec.run_id(), "run_status": "skipped", "reason": str(exc), **asdict(spec)}

    bev = None
    if spec.spatial_gate == "bev":
        from reid_benchmark.bev_matcher import BEVMatcher
        calib = _ROOT / config["bev_calibration"]
        if not calib.exists():
            return {"run_id": spec.run_id(), "run_status": "skipped",
                    "reason": f"missing {calib}", **asdict(spec)}
        bev = BEVMatcher.from_calibration(calib)

    cam_labels = config.get("camera_labels", ["ch9", "ch10"])
    if spec.scenario == "single_delay":
        path_a = path_b = str(_ROOT / videos["ch9_5min"])
        label_a, label_b = f"{cam_labels[0]} live", f"{cam_labels[0]} +{bench['delay_seconds']}s"
    else:
        path_a = str(_ROOT / videos["ch9_5min"])
        path_b = str(_ROOT / videos["ch10_5min"])
        label_a, label_b = cam_labels[0], cam_labels[1]

    cap_a, cap_b = cv2.VideoCapture(path_a), cv2.VideoCapture(path_b)
    if not cap_a.isOpened() or not cap_b.isOpened():
        return {"run_id": spec.run_id(), "run_status": "failed",
                "reason": "cannot open videos", **asdict(spec)}
    fps = cap_a.get(cv2.CAP_PROP_FPS) or float(bench["output_fps"])
    if spec.scenario == "single_delay":
        cap_b.set(cv2.CAP_PROP_POS_FRAMES, int(round(fps * bench["delay_seconds"])))

    run_id = spec.run_id()
    out_dir = _ROOT / config["paths"]["outputs_dir"] / run_id
    rep_dir = _ROOT / config["paths"]["reports_dir"] / run_id
    rep_dir.mkdir(parents=True, exist_ok=True)

    writer = None
    rows: list[dict] = []
    events: list[dict] = []
    # per-track embedding archive for post-run re-ranking:
    track_embs: dict[tuple[str, int], list[np.ndarray]] = {}

    process_every = max(1, int(bench.get("process_every_n_frames", 1)))
    max_frames = int(bench.get("max_frames", 0) or 0)
    frame_idx = 0
    started = time.perf_counter()
    raw_frame_a = 0

    while True:
        frame_start = time.perf_counter()
        frame_time = frame_idx * process_every / fps

        ok_a, frame_a = cap_a.read()
        ok_b, frame_b = cap_b.read()
        if not ok_a or not ok_b:
            break
        raw_frame_a += 1

        t0 = time.perf_counter()
        boxes_a = detector.detect(frame_a)
        boxes_b = detector.detect(frame_b)
        det_ms = (time.perf_counter() - t0) * 1000

        tracks_a = tracker_a.update(boxes_a, frame_idx)
        tracks_b = tracker_b.update(boxes_b, frame_idx)
        crops_a = crop_boxes(frame_a, [t.bbox for t in tracks_a])
        crops_b = crop_boxes(frame_b, [t.bbox for t in tracks_b])

        t0 = time.perf_counter()
        embs_a = embedder.embed(crops_a)
        embs_b = embedder.embed(crops_b)
        if face is not None:
            face_a = face.embed(crops_a)
            face_b = face.embed(crops_b)
        else:
            face_a = face_b = None
        if gait is not None:
            gait_a = gait.embed_tracks(frame_a, tracks_a, label_a)
            gait_b = gait.embed_tracks(frame_b, tracks_b, label_b)
        else:
            gait_a = gait_b = None
        reid_ms = (time.perf_counter() - t0) * 1000

        def _match(emb, cam, quality, face_emb, gait_emb=None):
            if face is not None:
                gid, dist = gallery.match(emb, face_emb, cam, frame_time, gait_emb)
                return gid
            gid, _, _ = gallery.match_embedding(emb, frame_time, quality, cam)
            return gid

        def _assign(gid, emb, cam, quality, face_emb, gait_emb=None):
            if face is not None:
                gallery.force_assign(gid, emb, face_emb, cam, frame_time, gait_emb)
            else:
                gallery.force_assign(gid, emb, frame_time, quality, cam)

        t0 = time.perf_counter()
        matched_a: set[int] = set()
        matched_b: set[int] = set()
        n_bev_pairs = 0

        if bev is not None and tracks_a and tracks_b:
            pairs = bev.match(tracks_a, tracks_b)
            n_bev_pairs = len(pairs)
            for ia, ib in pairs:
                if ia >= len(embs_a):
                    continue
                emb = _smooth(tracks_a[ia], embs_a[ia], spec.smoothing_window)
                gid = _match(emb, label_a, crop_quality(crops_a[ia]),
                             face_a[ia] if face_a is not None else None,
                             gait_a[ia] if gait_a is not None and ia < len(gait_a) else None)
                tracks_a[ia].global_id = gid
                tracks_b[ib].global_id = gid
                if ib < len(embs_b):
                    emb_b = _smooth(tracks_b[ib], embs_b[ib], spec.smoothing_window)
                    _assign(gid, emb_b, label_b, crop_quality(crops_b[ib]),
                            face_b[ib] if face_b is not None else None,
                            gait_b[ib] if gait_b is not None and ib < len(gait_b) else None)
                matched_a.add(ia)
                matched_b.add(ib)

        for i, track in enumerate(tracks_a):
            if i in matched_a or i >= len(embs_a):
                continue
            emb = _smooth(track, embs_a[i], spec.smoothing_window)
            track.global_id = _match(emb, label_a, crop_quality(crops_a[i]),
                                     face_a[i] if face_a is not None else None,
                                     gait_a[i] if gait_a is not None and i < len(gait_a) else None)
        for j, track in enumerate(tracks_b):
            if j in matched_b or j >= len(embs_b):
                continue
            emb = _smooth(track, embs_b[j], spec.smoothing_window)
            track.global_id = _match(emb, label_b, crop_quality(crops_b[j]),
                                     face_b[j] if face_b is not None else None,
                                     gait_b[j] if gait_b is not None and j < len(gait_b) else None)
        match_ms = (time.perf_counter() - t0) * 1000

        # archive per-track smoothed embeddings for re-ranking
        for cam, tracks, embs in ((label_a, tracks_a, embs_a), (label_b, tracks_b, embs_b)):
            for i, tr in enumerate(tracks):
                if i < len(embs) and embs.shape[1] > 1:
                    track_embs.setdefault((cam, tr.local_id), []).append(embs[i])

        # draw + record
        def _draw(frame_img, tracks, cam_label):
            out = frame_img.copy()
            cv2.putText(out, cam_label, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 220, 255), 2)
            for tr in tracks:
                x1, y1, x2, y2 = tr.bbox.astype(int)
                gid = tr.global_id if tr.global_id is not None else -1
                color = ((gid * 37) % 255, (gid * 17) % 255, (gid * 97) % 255)
                cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
                cv2.putText(out, f"G{gid}", (x1, max(20, y1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
            return out

        combined = hstack_resize(_draw(frame_a, tracks_a, label_a), _draw(frame_b, tracks_b, label_b))
        cv2.putText(combined, f"{run_id}", (12, combined.shape[0] - 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        if writer is None:
            out_dir.mkdir(parents=True, exist_ok=True)
            writer = cv2.VideoWriter(
                str(out_dir / f"{spec.scenario}.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                float(bench["output_fps"]), (combined.shape[1], combined.shape[0]),
            )
        writer.write(combined)
        if display:
            cv2.imshow(run_id, combined)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        total_ms = (time.perf_counter() - frame_start) * 1000
        rows.append({
            "frame": frame_idx, "detector_ms": round(det_ms, 2), "reid_ms": round(reid_ms, 2),
            "matching_ms": round(match_ms, 2), "total_ms": round(total_ms, 2),
            "live_fps": round(1000.0 / total_ms, 2) if total_ms > 0 else 0.0,
            "tracks": len(tracks_a) + len(tracks_b), "gallery_size": len(gallery.gallery),
            "bev_pairs": n_bev_pairs,
        })
        for cam, tracks in ((label_a, tracks_a), (label_b, tracks_b)):
            for tr in tracks:
                x1, y1, x2, y2 = tr.bbox.astype(float)
                events.append({
                    "frame": frame_idx, "scenario": spec.scenario, "model": run_id,
                    "camera": cam, "local_id": tr.local_id, "global_id": tr.global_id,
                    "x1": round(x1, 2), "y1": round(y1, 2), "x2": round(x2, 2), "y2": round(y2, 2),
                })

        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"{run_id}: {frame_idx} frames | gallery={len(gallery.gallery)}", flush=True)
        if max_frames and frame_idx >= max_frames:
            break
        for _ in range(process_every - 1):
            if not (cap_a.grab() and cap_b.grab()):
                break

    if writer is not None:
        writer.release()
    cap_a.release(); cap_b.release()
    if display:
        cv2.destroyAllWindows()

    # ---- post-run re-ranking: merge over-split IDs ----
    n_merged = 0
    if spec.rerank != "none" and track_embs:
        keys = list(track_embs.keys())
        feats = np.stack([l2_normalize(np.mean(np.stack(track_embs[k]), axis=0)) for k in keys])
        last_gid: dict[tuple[str, int], int] = {}
        for ev in events:
            last_gid[(ev["camera"], ev["local_id"])] = ev["global_id"]
        gids = [last_gid.get(k, -1) for k in keys]
        cams = [k[0] for k in keys]
        mapping = merge_ids_by_rerank(gids, feats, cams, method=spec.rerank,
                                      merge_threshold=threshold * 0.8)
        n_merged = sum(1 for a, b in mapping.items() if a != b)
        for ev in events:
            ev["global_id"] = mapping.get(ev["global_id"], ev["global_id"])

    # ---- write artifacts ----
    if rows:
        with (rep_dir / f"{spec.scenario}_timing.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    if events:
        with (rep_dir / f"{spec.scenario}_track_events.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(events[0].keys())); w.writeheader(); w.writerows(events)
    if gallery.telemetry:
        with (rep_dir / f"{spec.scenario}_gallery_telemetry.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(gallery.telemetry[0].keys()))
            w.writeheader(); w.writerows(gallery.telemetry)

    elapsed = time.perf_counter() - started
    summary = {
        "run_id": run_id, "run_status": "passed" if rows else "failed",
        **asdict(spec), "backend": backend, "threshold_used": threshold,
        "frames": len(rows), "elapsed_seconds": round(elapsed, 2),
        "avg_live_fps": round(float(np.mean([r["live_fps"] for r in rows])), 2) if rows else 0.0,
        "avg_reid_ms": round(float(np.mean([r["reid_ms"] for r in rows])), 2) if rows else 0.0,
        "unique_gids_final": len({e["global_id"] for e in events}),
        "rerank_merged_ids": n_merged,
        "gallery_stats": gallery.stats(),
    }
    (rep_dir / f"{spec.scenario}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def load_mtmc_config(path: Path = _CONFIG) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="MTMC tournament pipeline runner")
    ap.add_argument("--config", default=str(_CONFIG))
    ap.add_argument("--embedder", default="osnet_ain")
    ap.add_argument("--scenario", default="cross_camera", choices=["cross_camera", "single_delay", "both"])
    ap.add_argument("--tracker", default="iou")
    ap.add_argument("--no-tta", action="store_true")
    ap.add_argument("--smoothing", type=int, default=5)
    ap.add_argument("--gallery-policy", default="ema", choices=["ema", "ring", "quality_topk"])
    ap.add_argument("--gallery-k", type=int, default=5)
    ap.add_argument("--gallery-match", default="min", choices=["min", "mean", "top3"])
    ap.add_argument("--max-age", type=float, default=180.0)
    ap.add_argument("--gate", default="none", choices=["none", "bev", "topology"])
    ap.add_argument("--rerank", default="none", choices=["none", "k_reciprocal", "ca_jaccard", "aqe"])
    ap.add_argument("--fusion", default="none",
                    choices=["none", "camera_aware", "quality", "geff", "learned", "rank", "camera_aware_gait"])
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--stage1", action="store_true", help="run every Stage-1 embedder")
    ap.add_argument("--stage3", action="store_true",
                    help="run stage3_embedders x fusion strategies (camera_aware, quality, geff)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip runs whose summary JSON already exists (resume mode)")
    ap.add_argument("--display", action="store_true")
    args = ap.parse_args()

    config = load_mtmc_config(Path(args.config))
    scenarios = ["cross_camera", "single_delay"] if args.scenario == "both" else [args.scenario]

    # (embedder, fusion) combos per mode
    if args.stage1:
        combos = [(e, args.fusion) for e in config["stage1_embedders"]]
    elif args.stage3:
        combos = [(e, f) for e in config["stage3_embedders"]
                  for f in ("camera_aware", "quality", "geff")]
    else:
        combos = [(args.embedder, args.fusion)]
    embedders = [c[0] for c in combos]

    if args.stage1 or args.stage3:
        # One subprocess per run: several model repos define colliding
        # top-level modules (config/model/torchreid) — sequential in-process
        # loading poisons later imports (kpr, bpbreid, clip_reid, pass_reid).
        import subprocess
        results = []
        for emb, fusion in combos:
            for scen in scenarios:
                summary_path = (_ROOT / config["paths"]["reports_dir"]
                                / RunSpec(embedder=emb, scenario=scen, tracker=args.tracker,
                                          gallery_policy=args.gallery_policy, gallery_k=args.gallery_k,
                                          spatial_gate=args.gate, rerank=args.rerank,
                                          fusion=fusion, tag=args.tag).run_id()
                                / f"{scen}_summary.json")
                if args.skip_existing and summary_path.exists():
                    prev = json.loads(summary_path.read_text(encoding="utf-8"))
                    if prev.get("run_status") == "passed":
                        print(f"=== {emb}/{fusion}/{scen} === (skipped, already done)", flush=True)
                        results.append(prev)
                        continue
                cmd = [sys.executable, "-m", "MTMC.pipelines", "--config", args.config,
                       "--embedder", emb,
                       "--scenario", scen, "--tracker", args.tracker,
                       "--gallery-policy", args.gallery_policy,
                       "--gallery-k", str(args.gallery_k),
                       "--gallery-match", args.gallery_match,
                       "--max-age", str(args.max_age), "--gate", args.gate,
                       "--rerank", args.rerank, "--fusion", fusion,
                       "--smoothing", str(args.smoothing)]
                if args.no_tta:
                    cmd.append("--no-tta")
                if args.tag:
                    cmd += ["--tag", args.tag]
                if args.threshold is not None:
                    cmd += ["--threshold", str(args.threshold)]
                print(f"=== {emb}/{fusion}/{scen} (subprocess) ===", flush=True)
                proc = subprocess.run(cmd, cwd=str(_ROOT))
                if summary_path.exists():
                    results.append(json.loads(summary_path.read_text(encoding="utf-8")))
                else:
                    results.append({"run_id": f"{emb}/{fusion}/{scen}", "run_status": "skipped_or_failed",
                                    "exit_code": proc.returncode})
        print(json.dumps([{k: r.get(k) for k in ("run_id", "run_status", "unique_gids_final",
                                                  "avg_live_fps", "reason")} for r in results],
                         indent=2, default=str))
        return 0

    summaries = []
    for emb, fusion in combos:
        for scen in scenarios:
            spec = RunSpec(
                embedder=emb, scenario=scen, tracker=args.tracker,
                tta_flip=not args.no_tta, smoothing_window=args.smoothing,
                gallery_policy=args.gallery_policy, gallery_k=args.gallery_k,
                gallery_match=args.gallery_match, max_age_seconds=args.max_age,
                spatial_gate=args.gate, rerank=args.rerank, fusion=fusion,
                threshold=args.threshold, tag=args.tag,
            )
            summary_path = (_ROOT / config["paths"]["reports_dir"] / spec.run_id()
                            / f"{scen}_summary.json")
            if args.skip_existing and summary_path.exists():
                prev = json.loads(summary_path.read_text(encoding="utf-8"))
                if prev.get("run_status") == "passed":
                    print(f"=== {spec.run_id()} === (skipped, already done)", flush=True)
                    summaries.append(prev)
                    continue
            print(f"=== {spec.run_id()} ===", flush=True)
            summaries.append(run_pipeline(spec, config, display=args.display))

    print(json.dumps(summaries, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
