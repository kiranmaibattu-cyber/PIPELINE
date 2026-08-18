"""Edge load-test application for the MTMC OpenVINO pipeline -- POOLED build.

Same pipeline and same shared re-id gallery as streamapp_int8.py, but the six
models are NOT compiled per stream. They live in four shared device-server
processes (see MTMC/infer_pool.py), so model memory is constant instead of
linear in cameras, and the NPU embed calls are batched across streams.

Web dashboard to add video/RTSP streams and watch live hardware usage, so you
can find how many concurrent streams the Intel NPU+iGPU box handles.

Each stream = a worker thread: decode -> detect (iGPU) -> re-id embed (NPU).
A sampler thread reads CPU/RAM (psutil), NPU (sysfs busy counter), iGPU engines
(intel_gpu_top: Video=decode, Compute=inference). Dashboard polls /api/metrics.

Run on the box:   cd ~/Documents/RE_ID_E && python3 -m MTMC.streamapp
Then browse:      http://<box-ip>:8080

Env: PORT, DET_DEV(GPU), EMB_DEV(NPU), DET_MODEL, EMB_MODEL, PROC_EVERY(1)
"""
from __future__ import annotations
import os, sys, json, time, threading, glob, subprocess, re, itertools, multiprocessing
import atexit
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from MTMC.ov_backends import OVReidEmbedder, OVDetector  # noqa: E402,F401
from MTMC import infer_pool  # noqa: E402

MODELS = ROOT / "models"
PORT = int(os.environ.get("PORT", 8082))    # pooled dashboard (FP16=8080, INT8-per-process=8081)
APP_TAG = os.environ.get("APP_TAG", "pool")  # namespace for shm frames + gpu telemetry
DET_DEV = os.environ.get("DET_DEV", "GPU")
EMB_DEV = os.environ.get("EMB_DEV", "NPU")
DET_MODEL = os.environ.get("DET_MODEL", "yolo11s_int8.xml")   # INT8 detector
DET_CONF = float(os.environ.get("DET_CONF", "0.35"))     # detection floor. At 0.25 the detector emitted boxes
#                                                          # on a solid black region and on the burned-in TIMESTAMP
#                                                          # overlay; each became a global id and then "matched"
#                                                          # something in another camera. Audited on 5 real cameras:
#                                                          # 0.25 -> 15/33 correct cross-cam links, 8 anchored on
#                                                          # non-people; 0.35 -> 18/38 correct, 6 non-people.
GAIT_MOTION_PX = int(os.environ.get("GAIT_MOTION_PX", "22"))  # only run gait for tracks whose center moved > this over
GAIT_MOTION_WIN = 8                                            # the last N frames; seated/standing people skip gait (useless + heavy)
MAX_EMBED_PER_FRAME = int(os.environ.get("MAX_EMBED_PER_FRAME", "8"))  # cap crops embedded per frame (biggest/closest
#                                                        # first); keeps one heavy frame under the 5fps slot on crowded
#                                                        # cams. Overflow people get '?' that frame (rotates as they move). 0=off.
# ---- association rule + repair pass ----
# `min` link accepts on the single closest exemplar, so one lucky anchor decides a
# match and the greedy online assignment never revisits it. topk averages the k
# closest: robust to one outlier, without punishing view-diverse identities the way
# a full average does (ch9/ch10 face each other, so back-view vs front-view
# exemplars are legitimately far apart).
LINK_MODE = os.environ.get("LINK_MODE", "topk")     # min | avg | topk
LINK_TOPK = int(os.environ.get("LINK_TOPK", "2"))
# Repair: the online pass cannot undo a split it already made, so sweep the live
# gallery and merge ids that are one person. Bounded by a CANNOT-LINK set built for
# free from same-frame co-occurrence (two ids in one frame of one camera are
# provably different people).
REPAIR_EVERY_S = float(os.environ.get("REPAIR_EVERY_S", "20"))
REPAIR_THR = float(os.environ.get("REPAIR_THR", "0.0"))   # 0 -> derive from re-id threshold
REPAIR_THR_SCALE = float(os.environ.get("REPAIR_THR_SCALE", "0.85"))  # stricter than a live match
REPAIR_MAX_IDS = int(os.environ.get("REPAIR_MAX_IDS", "400"))
TRACK_MIN_HITS = int(os.environ.get("TRACK_MIN_HITS", "3"))  # a local track must survive this many
#                                                             # processed frames before it may create or
#                                                             # touch a global id. Without it a single-frame
#                                                             # detection blip (half-occluded, motion-blurred,
#                                                             # entering frame) mints an identity from a bad
#                                                             # crop, and the real person then fails to match
#                                                             # it. Mirrors tracker_min_hits in the offline
#                                                             # multicam_pipeline. Immature tracks are still
#                                                             # detected and drawn, just labelled "?".
MIN_EMBED_H = int(os.environ.get("MIN_EMBED_H", "48"))   # skip re-id embed for boxes shorter than this (px);
#                                                          # tiny/distant people stay DETECTED+drawn, just not embedded
#                                                          # -> frees the shared NPU (crowded cams embed far fewer crops)
# FULL INT8. The re-id threshold is RECALIBRATED for INT8 embeddings: 0.145 (vs
# FP16 0.141), found by matching FP16@0.141's same/different decisions on 696 real
# person crops (99.7% pairwise agreement over 242k pairs). INT8 body is near-
# identical to FP16 in distance scale; residual minor id-churn is quantization
# NOISE, not a threshold shift (calibration can't remove noise). NPU ~51% (vs FP16
# 85%). For FP16-exact re-id at the cost of NPU (~66%), set EMB_MODEL=transreid_ssl_fp16.xml.
EMB_MODEL = os.environ.get("EMB_MODEL", "transreid_ssl_int8.xml")    # INT8 body (re-id thr recalibrated to 0.145)
FACE_MODEL = os.environ.get("FACE_MODEL", "adaface_ir101_int8.xml")  # INT8 face
GAIT_MODEL = os.environ.get("GAIT_MODEL", "gaitbase_int8.xml")       # INT8 gait
SEG_MODEL = os.environ.get("SEG_MODEL", "yolov8n_seg.xml")
FACE_DEV = os.environ.get("FACE_DEV", "GPU")   # AdaFace on iGPU (was NPU): frees the NPU for appearance+gait,
#                                                # iGPU has headroom (~37%). gait CANNOT move (iGPU plugin rejects its ops).
GAIT_DEV = os.environ.get("GAIT_DEV", "NPU")
SEG_DEV = os.environ.get("SEG_DEV", "GPU")
PROC_EVERY = int(os.environ.get("PROC_EVERY", 1))  # process every Nth frame
TARGET_FPS = float(os.environ.get("TARGET_FPS", "5"))  # pace each stream to this fps: drop source frames to stay
#                                                        # live + throttle fast streams so the shared NPU serves more
#                                                        # streams at target. 0 = unthrottled. Cannot exceed NPU limit.
FACE_EVERY = int(os.environ.get("FACE_EVERY", 3))  # run face inference every N frames (cache between)
GAIT_EVERY = int(os.environ.get("GAIT_EVERY", 5))  # run gait inference every N frames (cache between)
NCORES = os.cpu_count() or 16

# ---------------- shared inference pool ----------------
# One server process per device role instead of six models inside every stream.
POOL_KINDS = ("det", "embed", "face", "gait")
# Replicas per role. One server per role would SERIALIZE every stream through a
# single process, which is slower than the old per-stream models. Replicas share
# one request queue as competing consumers, so work spreads with no scheduler.
# Cost is bounded: replicas x one model, not streams x six models.
POOL_REPLICAS = {k: max(1, int(os.environ.get(f"POOL_N_{k.upper()}", d)))
                 for k, d in (("det", 4), ("embed", 4), ("face", 4), ("gait", 2))}
POOL_Q: dict = {}          # kind -> request Queue (filled in main)
FRAME_SHM_BYTES = int(os.environ.get("FRAME_SHM_BYTES", str(1920 * 1080 * 3)))
POOL_CFG = {
    "det":   {"xml": str(MODELS / DET_MODEL),  "device": DET_DEV, "conf": DET_CONF},
    "embed": {"xml": str(MODELS / EMB_MODEL),  "device": EMB_DEV},
    "face":  {"xml": str(MODELS / FACE_MODEL), "device": FACE_DEV},
    "gait":  {"xml": str(MODELS / GAIT_MODEL), "device": GAIT_DEV,
              "seg_xml": str(MODELS / SEG_MODEL), "seg_device": SEG_DEV},
}

_ids = itertools.count(1)
WORKERS: dict[int, "WorkerHandle"] = {}
WLOCK = threading.Lock()


def _l2n(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else v


# ---------------- hardware video decode (iGPU media engine via ffmpeg VA-API) ----------------
VAAPI_DEV = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
DECODE_H = int(os.environ.get("DECODE_H", "720"))   # GPU-downscale decode height; 0 = full res (huge CPU cost)
HW_DECODE = os.environ.get("HW_DECODE", "1") == "1"


def _have_ffmpeg_vaapi():
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-hwaccels"],
                             capture_output=True, text=True, timeout=8).stdout
        return "vaapi" in out and os.path.exists(VAAPI_DEV)
    except Exception:
        return False


def _probe_dims(src):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", src],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


XCAM_THR = float(os.environ.get("XCAM_THR", "0.85"))  # cross-camera bar, as a fraction of the
#   calibrated appearance threshold. Swept at 15 streams and scored by crop audit
#   (MTMC/audit_live_crosscam.py, 84 pairs judged by eye):
#       1.00 -> 12 correct / 18 wrong   40% precision
#       0.85 -> 14 correct / 10 wrong   58% precision
#       0.70 -> 12 correct / 10 wrong   55% precision
#   0.85 removed 8 false links and cost no true ones -- correct links went UP,
#   so this is not a recall trade. 0.70 over-tightens: same false count, 2 fewer
#   true. The false links it kills are uniform collisions (navy scrubs vs navy
#   staff vest, two white coats) where the body embedding alone cannot separate
#   people and back views leave no face for the veto.
GALLERY_MAX_AGE_S = float(os.environ.get("GALLERY_MAX_AGE_S", "1800"))
#   Was 480s against a measured clock spread of 1152s at 30 streams, so live
#   identities were being evicted. Now wall-clock based (see _age_stamp), which
#   makes spread irrelevant to expiry; the headroom is for genuine absences.
WALL_CLOCK_AGING = os.environ.get("WALL_CLOCK_AGING", "1") == "1"
# Persistent gid rejoin store (MTMC/persistent_gallery.py). Off unless REJOIN_STORE
# names a directory. Lets a returning person REUSE their old global id instead of
# minting a new one, and carries ids across restarts. Face-based by default;
# appearance rejoin opt-in via REJOIN_APP_THR. Matching itself is untouched --
# rejoin only fires on ids the gallery just minted.
REJOIN_STORE = os.environ.get("REJOIN_STORE", "")
REJOIN_FACE_THR = float(os.environ.get("REJOIN_FACE_THR", "0.45"))
# app/gait rejoin thresholds: blank => use the pipeline's own calibrated values
# (gallery app_threshold / gait_threshold). Body+gait only rejoin when BOTH agree.
REJOIN_APP_THR = os.environ.get("REJOIN_APP_THR", "")
REJOIN_GAIT_THR = os.environ.get("REJOIN_GAIT_THR", "")
REJOIN_SAVE_S = float(os.environ.get("REJOIN_SAVE_S", "60"))
REJOIN_MAX_AGE_S = float(os.environ.get("REJOIN_MAX_AGE_S", "86400"))  # evict ids unseen 24h; 0 => never
VIDEO_CLOCK = os.environ.get("VIDEO_CLOCK", "1") == "1"
DECIMATE = os.environ.get("DECIMATE", "1") == "1"   # drop to TARGET_FPS inside ffmpeg,
#                                                    # before the GPU->host download
CATCHUP_MAX_S = float(os.environ.get("CATCHUP_MAX_S", "2.0"))  # video seconds a stream
#                                                               # may skip per cycle to re-sync


def _probe_duration(src):
    """Clip length, used to wrap a late stream's seek back into the file."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", src],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        d = float(out)
        return d if d > 1.0 else None
    except Exception:
        return None
# One reference instant for every stream. Workers are forked, so they all inherit
# the same value and therefore target the same position in the recording -- a
# camera added later fast-forwards to where the others already are instead of
# starting its own private timeline.
APP_T0 = time.time()


def _probe_fps(src):
    """Source frame rate, needed to convert a frame count into video seconds."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", src],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        num, _, den = out.partition("/")
        fps = float(num) / float(den or 1)
        return fps if 1.0 < fps < 240.0 else None
    except Exception:
        return None


def _clip_start_epoch(src):
    """Recording start time from an NVR filename (..._20260617100004_...).

    All five clips are the same recording window, so this puts every camera on ONE
    absolute timeline. Without it each stream reports its own wall clock since it
    was added, and since the pacing loop discards surplus decoded frames, a fast
    stream races AHEAD in video content -- a person's ch1 -> ch2 -> ch16 path can
    arrive out of order and every time-based gate (topology, max_age, same-camera
    gap) is measured on an axis that means nothing across cameras.
    """
    import re as _re
    from datetime import datetime, timezone
    m = _re.search(r"_(\d{14})_", os.path.basename(str(src)))
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class HWDecoder:
    """Decode frames on the iGPU media engine (VA-API) via ffmpeg; yields BGR ndarrays.
    Loops the source. Falls back is handled by the caller (make_decoder)."""

    def __init__(self, src, w, h, start_s=0.0, src_fps=None, out_fps=None):
        # downscale ON THE GPU during decode so only a small frame is copied to
        # the CPU (full-res NV12->bgr24 swscale for 1440p x N streams is the CPU
        # hog that eats fps; decode itself is cheap on the media engine).
        scaled = bool(DECODE_H and h > DECODE_H)
        # Decimate FIRST, on the GPU side. The fixed-function block still decodes
        # every frame (inter-frame prediction needs it) but only out_fps frames are
        # ever downloaded to host memory, colour-converted NV12->BGR24 by swscale
        # and pushed through the pipe. Those three are the CPU cost of "GPU decode":
        # at 25 fps a 720p BGR frame is 2.76 MB, i.e. 69 MB/s PER STREAM, and the
        # pipeline only ever uses out_fps of them.
        self.out_fps = float(out_fps) if out_fps and out_fps > 0 else None
        pre = f"fps={self.out_fps:g}," if self.out_fps else ""
        if scaled:
            tw = max(2, int(round(w * DECODE_H / h)) // 2 * 2)   # keep aspect, even width
            th = DECODE_H
            vf = f"{pre}scale_vaapi=w={tw}:h={th}:format=nv12,hwdownload,format=nv12"
        else:
            tw, th = w, h
            vf = f"fps={self.out_fps:g}" if self.out_fps else ""
        self.w, self.h, self.fsize = tw, th, tw * th * 3
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-hwaccel", "vaapi"] + \
              (["-hwaccel_output_format", "vaapi"] if scaled else []) + \
              ["-vaapi_device", VAAPI_DEV, "-stream_loop", "-1"] + \
              (["-ss", f"{start_s:.3f}"] if start_s > 0.5 else []) + ["-i", src] + \
              (["-vf", vf] if vf else []) + \
              ["-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self.fsize)
        self.hw = True
        # Frames pulled, INCLUDING pacing drops. Seeded from the seek so video
        # time is right immediately -- a late stream must not decode its way
        # forward through everything it missed.
        rate = self.out_fps or src_fps or 25.0
        self.n = int(start_s * rate) if start_s > 0.5 else 0

    def read(self):
        buf = self.p.stdout.read(self.fsize)
        if not buf or len(buf) < self.fsize:
            return False, None
        self.n += 1
        return True, np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)

    def release(self):
        try:
            self.p.kill()
        except Exception:
            pass


class Cv2Decoder:
    """Software decode fallback (OpenCV/CPU)."""

    def __init__(self, src):
        self.src = src
        self.cap = cv2.VideoCapture(src)
        self.hw = False
        self.n = 0        # frames pulled from the source, INCLUDING pacing drops

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            self.cap.release(); self.cap = cv2.VideoCapture(self.src)  # loop
            ok, frame = self.cap.read()
        if ok:
            self.n += 1
        return ok, frame

    def release(self):
        try:
            self.cap.release()
        except Exception:
            pass


_HW_OK = HW_DECODE and _have_ffmpeg_vaapi()


def make_decoder(src, start_s=0.0, src_fps=None, out_fps=None):
    """iGPU hardware decoder for file/rtsp sources when available, else CPU.

    start_s SEEKS the source so a stream added late lands where the already
    running cameras are. Decoding forward to that point instead means pushing
    every skipped frame through the pipe, which blocks the worker outright.
    """
    if _HW_OK and isinstance(src, str) and not src.isdigit():
        dims = _probe_dims(src)
        if dims:
            try:
                d = HWDecoder(src, dims[0], dims[1], start_s=start_s, src_fps=src_fps,
                              out_fps=out_fps)
                ok, _ = d.read()        # prime: if the vaapi filter chain errored, fall back to CPU
                if ok:
                    return d
                d.release()
            except Exception:
                pass
    return Cv2Decoder(int(src) if str(src).isdigit() else src)


HYST = int(os.environ.get("GID_HYST", 4))


class NamePlate:
    """Puts a NAME on an anonymous re-id gid by matching that gid's stored face
    exemplars against an enrolled gallery (FROZEN_V90_FACE/enrolled_named, built
    on Windows). Read-only: it never touches matching, merging, or the gallery --
    it only reads face_embs that the gallery already keeps and votes a name.

    Same AdaFace ir101 / 512-d space on both sides (Windows FP32 vs box INT8);
    measured top-1 agreement 51/51, mean |dist| drift 0.012, so the enrolled
    index transfers with no re-enrollment. Vote policy mirrors recognition.py
    exactly: per-name MIN cosine distance, global-min name, gate at threshold.

    Disabled (returns {} forever) when ENROLLED_FACES is unset or the index is
    missing/unloadable -- so the default deployment behaves identically."""

    def __init__(self, enrolled_dir: str, threshold: float):
        self.index = None
        self.row_names: dict[int, str] = {}
        self.threshold = float(threshold)
        if not enrolled_dir:
            return
        try:
            import faiss
            d = Path(enrolled_dir)
            self.index = faiss.read_index(str(d / "enrolled_faces.faiss"))
            man = json.loads((d / "enrolled_names.json").read_text(encoding="utf-8"))
            self.row_names = {int(k): v for k, v in man["row_names"].items()}
            self._faiss = faiss
            print(f"[names] enrolled {self.index.ntotal} faces / "
                  f"{len(set(self.row_names.values()))} people from {enrolled_dir} "
                  f"(thr={self.threshold})", flush=True)
        except Exception as e:
            self.index = None
            print(f"[names] disabled: {e}", flush=True)

    @property
    def enabled(self) -> bool:
        return self.index is not None

    def vote(self, face_embs) -> tuple[str, float]:
        """(name, cosine_distance) for one gid's face exemplars. 'Unknown' when
        no face clears the threshold. Faces are already L2-normed by the embedder."""
        if self.index is None or not face_embs:
            return "Unknown", 999.0
        q = np.ascontiguousarray(np.vstack(face_embs).astype(np.float32))
        self._faiss.normalize_L2(q)
        sims, ids = self.index.search(q, min(20, self.index.ntotal))
        best_by_name: dict[str, float] = {}
        for row_sims, row_ids in zip(sims, ids):
            for s, rid in zip(row_sims, row_ids):
                if rid < 0:
                    continue
                name = self.row_names.get(int(rid))
                if name is None:
                    continue
                dist = 1.0 - float(s)
                if name not in best_by_name or dist < best_by_name[name]:
                    best_by_name[name] = dist
        if not best_by_name:
            return "Unknown", 999.0
        name, dist = min(best_by_name.items(), key=lambda kv: kv[1])
        return (name if dist <= self.threshold else "Unknown"), dist


class RealReID:
    """The ACTUAL pipeline re-id: wraps MultiModalGallery (RRF fusion of
    appearance+face+gait, calibrated threshold, camera-aware face weighting).
    Shared across streams (cross-stream re-id); thread-safe."""

    def __init__(self):
        import yaml
        # app-private gallery copy (current version w/ cross-cam guards + exclude_gids);
        # falls back to the pipeline's own gallery. Offline pipeline is never touched.
        try:
            from MTMC.fusion_gallery_app import MultiModalGallery
        except Exception:
            from MTMC.fusion_gallery import MultiModalGallery
        self._MMG = MultiModalGallery
        root = MODELS.parent
        cfg = {}
        for name in ("multicam_5_ov_fix.yaml", "multicam_5_ov.yaml", "multicam_5.yaml"):
            p = root / "MTMC" / "configs" / name
            if p.exists():
                cfg = yaml.safe_load(p.read_text(encoding="utf-8")); break

        def _jload(rel):
            p = root / rel
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

        emb_key = cfg.get("embedder", "transreid_ssl")
        thr = _jload(cfg.get("calibration_file", "MTMC/reports/calibrated_thresholds.json")).get(emb_key, {}).get("threshold", 0.35)
        face_thr = _jload("MTMC/reports/stage2_face_screen.json").get("adaface_ir101", {}).get("threshold", 0.8045)
        gd = _jload("MTMC/reports/stage2_gait_screen.json").get("gaitbase_gait3d", {})
        gait_thr = round(1.0 - (gd.get("mean_same_sim", 0.75) + gd.get("mean_diff_sim", 0.55)) / 2.0, 4) if gd else 0.3496
        strat = cfg.get("fusion", "camera_aware_gait")
        self.cfg = dict(
            app_threshold=float(thr),
            strategy=strat if strat != "none" else "camera_aware_gait",
            frontal_cameras=tuple(cfg.get("frontal_cameras", ["ch2", "ch9", "ch10"])),
            max_age_seconds=GALLERY_MAX_AGE_S,
            wall_clock_aging=WALL_CLOCK_AGING,
            k=int((cfg.get("gallery") or {}).get("k", 10)),
            face_threshold=float(face_thr), gait_threshold=float(gait_thr),
            # Topology is usable now that every camera shares one recording timeline
            # (video clock + video-time pacing). It was OFF because per-stream wall
            # clocks put the cameras up to 16 minutes apart in content, which made
            # every transition window meaningless. Learned per-pair windows come from
            # MTMC/reports/learned_transitions.json.
            topology=os.environ.get("TOPOLOGY", "1") == "1",
            learned_transitions=_jload("MTMC/reports/learned_transitions.json") or None,
            topo_enforce_min=os.environ.get("TOPO_ENFORCE_MIN", "1") == "1",
            # cross-camera precision guards (were defaulted OFF -> body+colour alone
            # merged different people, e.g. two white-coat doctors). Face veto is safe:
            # only fires when BOTH sides have a face and the faces clearly disagree.
            cross_camera_face_veto=bool(cfg.get("cross_camera_face_veto", True)),
            cross_camera_face_veto_threshold=float(cfg.get("cross_camera_face_veto_threshold", 1.10)),  # back to recall-safe
            cross_camera_require_face=bool(cfg.get("cross_camera_require_face", False)),                 # OFF: it blocked valid back-view merges (killed cross-cam recall)
            # With the maturation gate now filtering blips upstream, requiring a
            # second single-camera sighting before an identity may be a cross-camera
            # candidate is partly redundant and can block valid merges. Tunable.
            cross_camera_min_single_camera_seen=int(os.environ.get(
                "XCAM_MIN_SEEN", cfg.get("cross_camera_min_single_camera_seen", 2))),
            # Cross-camera decision threshold, as a FRACTION of the calibrated
            # appearance threshold (distances reach _decision_threshold already
            # divided by self.threshold, so 1.0 == the same bar as same-camera).
            # Below 1.0 demands a closer match to link across cameras and trades
            # recall for precision. Default 1.0 keeps existing behaviour.
            cross_camera_match_threshold=XCAM_THR,
            link_mode=LINK_MODE, link_topk=LINK_TOPK,
            # FAISS candidate search: index the appearance exemplars and score only
            # the top-k gids. Verified to reproduce the linear scan's decisions
            # exactly (1800/1800) at 2.6x the speed. Off below FAISS_MIN_GALLERY,
            # where the linear scan is already cheaper than maintaining the index.
            use_faiss=os.environ.get("USE_FAISS", "1") == "1",
            faiss_min_gallery=int(os.environ.get("FAISS_MIN_GALLERY", "512")),
            faiss_topk=int(os.environ.get("FAISS_TOPK", "256")),
        )
        self.lock = threading.Lock()
        self._build()

    def _build(self):
        import inspect
        # pass only kwargs this MultiModalGallery version actually accepts
        allowed = set(inspect.signature(self._MMG.__init__).parameters)
        cfg = {k: v for k, v in self.cfg.items() if k in allowed}
        self._supports_exclude = "exclude_gids" in set(inspect.signature(self._MMG.match).parameters)
        self.g = self._MMG(**cfg)
        self.matches = 0
        self.total = 0

    def match(self, sm, fe, cam, t, ge, exclude_gids=None):
        with self.lock:
            self.total += 1
            if self._supports_exclude:
                gid, dist = self.g.match(sm, fe, cam, t, ge, exclude_gids=exclude_gids)
            else:
                gid, dist = self.g.match(sm, fe, cam, t, ge)
            if dist <= 1.0:
                self.matches += 1
            return gid, dist

    def force_assign(self, gid, sm, fe, cam, t, ge):
        with self.lock:
            self.g.force_assign(gid, sm, fe, cam, t, ge)

    def repair(self, cannot_link, thr, max_ids):
        """Merge live ids that the online pass split. Returns list of (src, dst).

        Only merges pairs that are NOT provably different (cannot_link) and whose
        exemplar sets agree under the link rule. Merges are applied smallest-into-
        largest and the cannot-link sets are unioned, so a merge can never create a
        pair that was already excluded -- the transitivity trap that corrupted
        gid_remap in the offline work.
        """
        merged = []
        with self.lock:
            gids = sorted(self.g.gallery, key=lambda g: -self.g.gallery[g].seen_count)[:max_ids]
            excl: dict[int, set] = {}
            for pair in cannot_link:
                pr = tuple(pair)
                if len(pr) != 2:
                    continue
                x, y = int(pr[0]), int(pr[1])
                excl.setdefault(x, set()).add(y)
                excl.setdefault(y, set()).add(x)
            group = {g: {g} for g in gids}   # ids already fused into this one
            gone = set()
            for i, a in enumerate(gids):
                if a in gone:
                    continue
                for b in gids[i + 1:]:
                    if b in gone:
                        continue
                    ga, gb = group[a], group[b]
                    # transitivity: after merging, a's group absorbs b's. Refuse if
                    # ANY member of one group is provably different from ANY member
                    # of the other -- checking only the (a, b) pair is what let the
                    # offline gid_remap fuse different people through a chain.
                    if any(excl.get(x, set()) & gb for x in ga):
                        continue
                    ea, eb = self.g.gallery[a], self.g.gallery[b]
                    # Repair reunites FRAGMENTS of one person in one camera. It must
                    # not forge CROSS-camera links: the online matcher gates those
                    # with the face veto and the colour gate, and this path has
                    # neither. A visual audit of cross-camera pairs showed ~14/18
                    # were different people once repair was allowed to link cameras.
                    if not (ea.camera_set & eb.camera_set):
                        continue
                    # if both sides have faces and the faces disagree, they are not
                    # the same person, whatever the body says (same-uniform problem)
                    if ea.face_embs and eb.face_embs:
                        df = self.g._min_dist(np.stack(eb.face_embs)[0], ea.face_embs)
                        if df is not None and df > self.g.face_threshold:
                            continue
                    d = self.g.pair_distance(a, b)
                    if d is None or d > thr:
                        continue
                    if self.g.merge_gid(b, a):
                        gone.add(b)
                        group[a] = ga | gb
                        merged.append((int(b), int(a)))
        return merged

    def touch(self, gid, cam, t):
        """Refresh recency for a hysteresis-held id WITHOUT storing embeddings."""
        with self.lock:
            try:
                return self.g.touch(gid, cam, t)
            except AttributeError:      # older gallery build on the box
                return False

    def set_threshold(self, thr):
        with self.lock:
            self.cfg["app_threshold"] = float(thr)
            self._build()

    def live_gids(self):
        """gids still in the gallery (max_age purges the rest)."""
        with self.lock:
            return {int(g) for g in self.g.gallery}

    def crosscam(self):
        """How many live identities were actually seen in >1 camera. This is the
        only honest answer to 'is cross-camera re-id happening'."""
        with self.lock:
            hist, multi, pairs = {}, 0, {}
            for e in self.g.gallery.values():
                cams = sorted(str(c) for c in e.camera_set)
                hist[len(cams)] = hist.get(len(cams), 0) + 1
                if len(cams) > 1:
                    multi += 1
                    for i in range(len(cams)):
                        for j in range(i + 1, len(cams)):
                            k = f"{cams[i]}|{cams[j]}"
                            pairs[k] = pairs.get(k, 0) + 1
            top = dict(sorted(pairs.items(), key=lambda kv: -kv[1])[:10])
            return {"multi_cam_ids": multi, "cams_per_id": hist, "top_pairs": top}

    def names(self, plate):
        """gid -> {name, dist, n_faces} for every LIVE gid whose face exemplars
        match an enrolled person. Read-only walk of the gallery, mirrors
        crosscam(). Returns {} when naming is disabled."""
        if plate is None or not plate.enabled:
            return {}
        with self.lock:
            snap = [(int(g), list(e.face_embs)) for g, e in self.g.gallery.items() if e.face_embs]
        out = {}
        for gid, fembs in snap:
            name, dist = plate.vote(fembs)
            if name != "Unknown":
                out[gid] = {"name": name, "dist": round(dist, 4), "n_faces": len(fembs)}
        return out

    def stats(self):
        with self.lock:
            return {"persons": self.g.next_global_id - 1, "live": len(self.g.gallery),
                    "matches": self.matches, "queries": self.total,
                    "hit_rate": round(100 * self.matches / self.total, 1) if self.total else 0.0,
                    "thr": round(self.cfg["app_threshold"], 3), "fusion": self.cfg["strategy"]}

    def reset(self):
        with self.lock:
            self._build()


DESIRED_THR = float(os.environ.get("REID_THR", "0.145"))  # INT8-calibrated re-id threshold (FP16 was 0.141)


# ---------------- IRRA text->person search (lazy load, CPU fp32, async index) ----------------
CROP_STORE = {}          # pid (sid*100000+gid) -> BGR crop (one per person)
CROP_LOCK = threading.Lock()
CROP_Q = None            # multiprocessing Queue: workers -> manager (pid, jpeg bytes)
MGR = None               # multiprocessing.Manager (created in main)
REID_REQ_Q = None        # multiprocessing Queue: workers -> central re-id service (cross-cam)
REID_STAT = None         # shared dict: single-gallery stats (persons/live/match_rate)


class IRRAIndex:
    """Text->person search via IRRA (CLIP dual encoder). A background thread
    IRRA-encodes each person's crop (off the per-frame path); a query encodes
    the text and cosine-ranks persons. Loads lazily (1.3 GB, fp32 on CPU)."""

    def __init__(self):
        self.searcher = None
        self.emb = {}        # gid -> (512,) IRRA image embedding
        self.lock = threading.Lock()
        self.status = "idle"
        self.thread = None
        self.last_query = None

    def _load(self):
        if self.searcher is not None:
            return True
        try:
            self.status = "loading model (1.3GB)…"
            from MTMC.text_search.models import IRRASearcher
            s = IRRASearcher()
            try:
                s.model.float()   # box has no CUDA -> run IRRA in fp32
            except Exception:
                pass
            self.searcher = s
            self.status = "ready"
            return True
        except Exception as e:
            self.status = f"load failed: {str(e)[:100]}"
            return False

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        if not self._load():
            return
        while True:
            time.sleep(2.0)
            with CROP_LOCK:
                todo = [(g, c) for g, c in CROP_STORE.items() if g not in self.emb]
            for g, c in todo[:6]:
                try:
                    e = self.searcher.encode_images([c])[0]
                    with self.lock:
                        self.emb[g] = e
                except Exception:
                    pass

    def search(self, query, top_k=12):
        self.start()
        if self.searcher is None:
            return {"status": self.status, "results": []}
        self.last_query = query
        try:
            q = self.searcher.encode_text(query)
        except Exception as e:
            return {"status": f"query failed: {str(e)[:80]}", "results": []}
        with self.lock:
            gids = list(self.emb.keys())
            mat = np.stack([self.emb[g] for g in gids]) if gids else None
        if mat is None:
            return {"status": "no persons indexed yet — add a stream first", "results": []}
        sims = mat @ q
        order = np.argsort(-sims)[:top_k]
        return {"status": "ok", "query": query,
                "results": [{"gid": int(gids[i]), "score": round(float(sims[i]), 3)} for i in order]}

    def stats(self):
        with self.lock:
            return {"status": self.status, "indexed": len(self.emb), "query": self.last_query}


IRRA = IRRAIndex()


def _shm_path(sid):
    return f"/dev/shm/{APP_TAG}_{sid}.jpg"


def _ser(x):
    """np vector -> bytes for cross-process transfer (None-safe, float32)."""
    return None if x is None else np.ascontiguousarray(x, dtype=np.float32).tobytes()


def _deser(b):
    return None if b is None else np.frombuffer(b, dtype=np.float32).copy()


def _color_hist(crop):
    """HSV hue-saturation histogram over the torso ROI (ported from the offline
    pipeline). 16x8 = 128-d, MINMAX-normalized. Used as a cross-camera colour gate."""
    if crop is None or crop.size == 0:
        return None
    h, w = crop.shape[:2]
    roi = crop[int(0.15 * h):int(0.95 * h), int(0.15 * w):int(0.85 * w)]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return hist.astype(np.float32).reshape(-1)


def _hist_distance(a, b):
    if a is None or b is None:
        return 0.0
    return float(cv2.compareHist(a.reshape(16, 8), b.reshape(16, 8), cv2.HISTCMP_BHATTACHARYYA))


# cross-camera colour gate: reject a cross-cam merge if the torso colour differs
# by more than this Bhattacharyya distance (0=identical, 1=disjoint). "cg45" winner.
COLOUR_GATE_THR = float(os.environ.get("COLOUR_GATE_THR", "0.45"))
COLOUR_GATE_MIN = 2      # need >= this many stored hists in the OTHER camera to trust the gate
COLOUR_GATE_MAX = 12     # cap stored hists per (gid, camera)
COLOUR_GATE_PRUNE_EVERY = 200   # sweep dead gids out of the colour history this often
DEV_SMOOTH_N = int(os.environ.get("DEV_SMOOTH_N", "5"))   # device gauges: rolling mean over N 1 s samples


def reid_service(req_q, stop_ev, thr):
    """CENTRAL re-id process: ONE MultiModalGallery shared by every stream, so a
    person seen in ch9 and ch10 gets the SAME global id (cross-camera re-id, same
    as the offline pipeline). Workers send appearance/face/gait embeddings; this
    process runs smoothing + gallery.match + tracklet hysteresis and returns the
    global ids. All MTMC fusion/gallery code runs UNCHANGED here."""
    gallery = RealReID()
    if thr:
        gallery.set_threshold(thr)
    # Optional face-recognition naming layer (read-only; see NamePlate). Off unless
    # ENROLLED_FACES points at an enrolled_named dir. Names published to REID_STAT.
    plate = NamePlate(os.environ.get("ENROLLED_FACES", ""),
                      float(os.environ.get("NAME_THR", "0.45")))
    # 0.45 not 0.55: at 0.55 back-view/masked people got weak faces that squeaked
    # under the bar and mislabelled DIFFERENT people (three men all "Imran"). The
    # live distances split cleanly -- real matches <=0.41, false ones >=0.49 -- so
    # 0.45 sits in the gap, keeping every true name and dropping the false ones.
    # Optional persistent gid rejoin store. Off unless REJOIN_STORE is set. Reuses a
    # returning person's old gid and carries ids across restarts. `seen` is the set
    # of gids already handed out this run, so rejoin fires only on truly fresh ids.
    store = None
    seen = set()
    last_save = [time.time()]
    if REJOIN_STORE:
        try:
            from MTMC.persistent_gallery import PersistentGallery
            # default app/gait rejoin thresholds to the pipeline's calibrated values
            app_thr = float(REJOIN_APP_THR) if REJOIN_APP_THR else float(gallery.cfg.get("app_threshold", 0.35))
            gait_thr = float(REJOIN_GAIT_THR) if REJOIN_GAIT_THR else float(gallery.cfg.get("gait_threshold", 0.35))
            store = PersistentGallery(REJOIN_STORE, face_thr=REJOIN_FACE_THR,
                                      app_thr=app_thr, gait_thr=gait_thr, max_age_s=REJOIN_MAX_AGE_S)
            if store.enabled:
                print(f"[rejoin] thresholds face={REJOIN_FACE_THR} app={app_thr:.3f} "
                      f"gait={gait_thr:.3f} (body+gait must agree)", flush=True)
                # resume the id counter above every persisted id so numbers never collide
                gallery.g.next_global_id = max(gallery.g.next_global_id, store.next_gid)
            else:
                store = None
        except Exception as e:
            print(f"[rejoin] init failed, disabled: {e}", flush=True)
            store = None
    resp = {}   # sid -> response Queue
    st = {}     # sid -> {"sbuf":{}, "tgid":{}, "tpend":{}}  (per-stream smoothing + hysteresis)
    chists = {}  # gid -> {camera: [colour hist, ...]}  (cross-camera colour gate)
    prune = [0]  # remember_colour calls since the last dead-gid sweep
    cc = [0, None]  # [messages since last cross-cam summary, last summary]
    cannot = set()   # frozenset({gid_a, gid_b}) seen in the SAME frame of one camera
    #                  -> provably different people, free negatives for the repair pass
    remap = {}       # merged-away gid -> surviving gid (streams still hold the old id)
    namemap = {}     # gid -> enrolled name (rides the reid response so workers can
    #                  draw the NAME on the bounding box). Refreshed on the cc cadence.
    last_repair = [time.time()]

    def resolve(g):
        seen = 0
        while g in remap and seen < 16:
            g = remap[g]; seen += 1
        return g

    def do_repair():
        thr = REPAIR_THR or (gallery.cfg["app_threshold"] * REPAIR_THR_SCALE)
        merges = gallery.repair(cannot, thr, REPAIR_MAX_IDS)
        for src, dst in merges:
            remap[src] = dst
            chists.pop(src, None)
        if merges:
            # rewrite every stream's held id so the display does not flip back
            for st_s in st.values():
                for k, v in list(st_s["tgid"].items()):
                    nv = resolve(v)
                    if nv != v:
                        st_s["tgid"][k] = nv
            print(f"[reid] repair merged {len(merges)} ids (thr={thr:.4f})", flush=True)
        return len(merges)

    def colour_exclude(cam, hist):
        """gids whose torso colour, seen in a DIFFERENT camera, clearly disagrees
        with this crop -> block them from being matched (prevents cross-cam merge of
        different people). Only gates identities NOT yet seen in this camera."""
        if hist is None or not COLOUR_GATE_THR:
            return None
        bad = set()
        for gid, by_cam in chists.items():
            if cam in by_cam:          # already appears in this camera -> not a cross-cam merge, don't gate
                continue
            other = [h for hs in by_cam.values() for h in hs]
            if len(other) < COLOUR_GATE_MIN:
                continue
            if min(_hist_distance(hist, h) for h in other) > COLOUR_GATE_THR:
                bad.add(int(gid))
        return bad or None

    def remember_colour(gid, cam, hist):
        if gid is None or hist is None:
            return
        hs = chists.setdefault(int(gid), {}).setdefault(cam, [])
        hs.append(hist)
        if len(hs) > COLOUR_GATE_MAX:
            del hs[0]
        # the gallery ages identities out; chists must follow or colour_exclude
        # degrades into an O(every gid ever created) scan and stalls every stream.
        prune[0] += 1
        if prune[0] >= COLOUR_GATE_PRUNE_EVERY:
            prune[0] = 0
            try:
                alive = gallery.live_gids()
            except Exception:
                return
            for dead in [g for g in chists if g not in alive]:
                del chists[dead]

    def smooth(s, key, e):
        b = s["sbuf"].setdefault(key, []); b.append(_l2n(e))
        if len(b) > 5:
            del b[0]
        return _l2n(np.mean(b, axis=0))

    def hyst(s, key, rg, sm, fe, cam, ge, t):
        tgid, tpend = s["tgid"], s["tpend"]
        lk = tgid.get(key)
        if lk is None:
            tgid[key] = rg; return rg
        if rg == lk:
            tpend.pop(key, None); return lk
        cg, cnt = tpend.get(key, (rg, 0)); cnt = cnt + 1 if cg == rg else 1; tpend[key] = (rg, cnt)
        if cnt >= HYST:
            tgid[key] = rg; tpend.pop(key, None); return rg
        # hold the displayed id, but only refresh recency -- writing sm/fe/ge into
        # `lk` here would store the CURRENT person's embedding under the OLD id.
        gallery.touch(lk, cam, t)
        return lk

    _ppid0 = os.getppid()
    def _flush_store():
        if store is not None:
            try:
                store.save(gallery.g.next_global_id)
                print(f"[rejoin] saved store: {store.stats()}", flush=True)
            except Exception as e:
                print(f"[rejoin] final save failed: {e}", flush=True)

    while not stop_ev.is_set():
        if os.getppid() != _ppid0:   # re-parented -> our parent died; do not linger
            _flush_store()
            break
        try:
            msg = req_q.get(timeout=0.5)
        except Exception:
            continue
        ty = msg.get("t")
        if ty == "reg":
            resp[msg["sid"]] = msg["resp"]; st[msg["sid"]] = {"sbuf": {}, "tgid": {}, "tpend": {}}
            continue
        if ty == "unreg":
            resp.pop(msg["sid"], None); st.pop(msg["sid"], None)
            continue
        if ty == "thr":
            try:
                gallery.set_threshold(float(msg["thr"]))
                chists.clear()   # gallery rebuilt -> colour history is stale
            except Exception:
                pass
            continue
        if ty == "m":
            sid, cam, t = msg["sid"], msg["cam"], msg["ts"]
            s = st.get(sid)
            gids, dists = [], []
            used = set()   # gids already handed out in THIS frame of THIS camera
            # One bad item must not kill the service: every stream blocks on this
            # process, so an uncaught exception here stalls the whole box at the
            # workers' 3 s timeout (~0.3 fps) instead of failing one query.
            try:
              if s is not None:
                for it in msg["items"]:
                    key = (sid, it["lid"])
                    sm = smooth(s, key, _deser(it["a"]))
                    fe, ge = _deser(it.get("f")), _deser(it.get("g"))
                    hist = _deser(it.get("c"))
                    ex = colour_exclude(cam, hist)   # block colour-incompatible cross-cam ids
                    # same-frame same-camera guard: one person cannot be two boxes in
                    # one frame, so an id already used this frame is not a candidate.
                    # Without this two people merge into one identity and poison it.
                    ex = (ex | used) if ex else (set(used) or None)
                    rg, dist = gallery.match(sm, fe, cam, t, ge, exclude_gids=ex)
                    gid = resolve(hyst(s, key, resolve(rg), sm, fe, cam, ge, t))
                    if gid in used:
                        # hysteresis wanted an id already taken this frame -> drop the
                        # hold and take the fresh match (rg excluded `used`, so it is free)
                        s["tgid"][key] = rg; s["tpend"].pop(key, None)
                        gid = rg
                    # persistent rejoin: a gid we have never handed out before may be a
                    # returning person. Ask the store; if its face matches a retired id,
                    # remap this fresh id back to the old one (same path as repair).
                    if store is not None and gid is not None and gid not in seen:
                        rj, rd, mod = store.rejoin(fe, sm, ge)
                        if rj is not None and rj != gid and rj not in used:
                            remap[gid] = rj
                            s["tgid"][key] = rj; s["tpend"].pop(key, None)
                            gid = rj
                    if gid is not None:
                        seen.add(gid)
                    used.add(gid)
                    remember_colour(gid, cam, hist)
                    if store is not None and gid is not None:
                        store.observe(gid, fe, sm, ge, t)
                    gids.append(gid); dists.append(float(dist))
            except Exception as e:
                n = len(msg.get("items") or [])
                gids = (gids + [None] * n)[:n]
                dists = (dists + [99.0] * n)[:n]
                print(f"[reid] item batch failed: {e}", flush=True)
            q = resp.get(sid)
            if q is not None:
                try:
                    # resolve remap before naming so a rejoined id carries its name
                    q.put({"seq": msg.get("seq"), "gids": gids, "dists": dists,
                           "names": [namemap.get(resolve(g)) if g is not None else None for g in gids]})
                except Exception:
                    pass
            # every pair co-visible in this frame of this camera is a free negative
            if len(used) > 1:
                ul = sorted(g for g in used if g is not None)
                for i, ga in enumerate(ul):
                    for gb in ul[i + 1:]:
                        cannot.add(frozenset((ga, gb)))
            if REPAIR_EVERY_S and (time.time() - last_repair[0]) >= REPAIR_EVERY_S:
                last_repair[0] = time.time()
                try:
                    do_repair()
                except Exception as e:
                    print(f"[reid] repair failed: {e}", flush=True)
            # persist the rejoin store off the hot path
            if store is not None and REJOIN_SAVE_S and (time.time() - last_save[0]) >= REJOIN_SAVE_S:
                last_save[0] = time.time()
                try:
                    store.save(gallery.g.next_global_id)
                except Exception as e:
                    print(f"[rejoin] save failed: {e}", flush=True)
            try:
                s = gallery.stats()
                cc[0] += 1
                if cc[0] >= 50:      # cross-cam summary walks the gallery -> not every message
                    cc[0] = 0
                    cc[1] = gallery.crosscam()
                    if plate.enabled:
                        nm = gallery.names(plate)
                        cc[1] = dict(cc[1] or {}, names=nm, n_named=len(nm))
                        namemap = {int(g): v["name"] for g, v in nm.items()}
                    if store is not None:
                        cc[1] = dict(cc[1] or {}, **store.stats())
                if cc[1]:
                    s.update(cc[1])
                REID_STAT.update(s)
            except Exception:
                pass
    _flush_store()


def run_stream(sid, source, camera, do_face, do_gait, mdict, crop_q, stop_ev, desired_thr, req_q, resp_q,
               pool_q, pool_resp):
    """PROCESS entry: the heavy per-camera pipeline for one stream (decode, detect,
    track, embed, face, gait — all on NPU/iGPU, no GIL contention). Re-id itself is
    delegated to the CENTRAL reid_service so all streams share ONE gallery
    (cross-camera). Writes metrics to `mdict`, annotated frame to /dev/shm, crops to
    `crop_q`."""
    shm, shm_tmp = _shm_path(sid), _shm_path(sid) + ".tmp"
    _ppid0 = os.getppid()
    try:
        from MTMC.adapters import IoUTracker, crop_boxes
        # No models are compiled here: this process only decodes, tracks and draws.
        # Inference goes to the shared device servers (one model copy for the box).
        ftx = infer_pool.FrameTx(FRAME_SHM_BYTES)
        det = infer_pool.DetClient(sid, pool_q["det"], pool_resp["det"], ftx)
        tracker = IoUTracker()
        emb = infer_pool.EmbedClient(sid, pool_q["embed"], pool_resp["embed"])
        face = gait = None
        if do_face:
            face = infer_pool.FaceClient(sid, pool_q["face"], pool_resp["face"])
        if do_gait:
            gait = infer_pool.GaitClient(sid, pool_q["gait"], pool_resp["gait"], ftx)
        # cross-cam re-id lives in the central service; register this stream's return channel
        req_q.put({"t": "reg", "sid": sid, "resp": resp_q})

        fcache, gcache = {}, {}
        gids_seen, sent = set(), set()
        pos_hist = {}   # local_id -> recent bbox-center list (motion gate for gait)
        hits = {}       # local_id -> processed frames survived (maturation gate)
        last_gid = {}   # local_id -> last assigned gid (label persists when a track is capped out of embedding)
        last_name = {}  # local_id -> enrolled name (drawn on the box; persists like last_gid)
        reid_hits = reid_q = frames = seq = 0
        fps = dpf = tdec = tdet = ttk = tem = tfa = tga = 0.0
        a = 0.3
        fe_every, ga_every = max(1, FACE_EVERY), max(1, GAIT_EVERY)

        src_fps0 = _probe_fps(source) if VIDEO_CLOCK else None
        clip_len = _probe_duration(source) if VIDEO_CLOCK else None
        # where the already-running cameras are now, wrapped into the clip
        seek_s = 0.0
        if src_fps0 and clip_len and clip_len > 1.0:
            seek_s = (time.time() - APP_T0) % clip_len
        want_out = TARGET_FPS if (DECIMATE and TARGET_FPS > 0) else None
        dec = make_decoder(source, start_s=seek_s, src_fps=src_fps0, out_fps=want_out)
        hw = getattr(dec, "hw", False)
        # VIDEO clock: absolute recording time = clip start + frames_pulled / fps.
        # Shared by every camera, so ordering is the real ordering no matter how
        # fast each stream happens to process. Falls back to per-stream wall clock
        # for live sources or filenames without an NVR timestamp.
        # rate at which dec.n advances: the decimated output rate when ffmpeg is
        # doing the dropping, otherwise the true source rate
        src_fps = getattr(dec, "out_fps", None) or src_fps0
        clip_t0 = _clip_start_epoch(source) if VIDEO_CLOCK else None
        use_vclock = bool(src_fps and clip_t0)
        last = 0.0
        wall0 = time.perf_counter()   # real elapsed video time (frames are dropped for pacing)
        while not stop_ev.is_set():
            if os.getppid() != _ppid0:   # manager died (re-parented) -> self-exit
                break
            t0 = time.perf_counter()
            ok, frame = dec.read()
            if not ok:
                break
            for _ in range(PROC_EVERY - 1):
                dec.read()
            td = (time.perf_counter() - t0) * 1000
            t1 = time.perf_counter(); boxes = det.detect(frame); tdt = (time.perf_counter() - t1) * 1000
            t1 = time.perf_counter(); tracks = tracker.update(boxes, frames); ttr = (time.perf_counter() - t1) * 1000
            # motion history (bbox center) for the gait gate — updated every frame
            live_ids = {int(tr.local_id) for tr in tracks}
            pos_hist = {k: v for k, v in pos_hist.items() if k in live_ids}
            hits = {k: v for k, v in hits.items() if k in live_ids}
            for lid in live_ids:
                hits[lid] = hits.get(lid, 0) + 1
            for tr in tracks:
                cx, cy = (tr.bbox[0] + tr.bbox[2]) / 2.0, (tr.bbox[1] + tr.bbox[3]) / 2.0
                h = pos_hist.setdefault(int(tr.local_id), [])
                h.append((cx, cy))
                if len(h) > GAIT_MOTION_WIN:
                    del h[0]

            def _moving(lid):
                h = pos_hist.get(int(lid))
                if not h or len(h) < 3:
                    return True   # new/short track -> allow (capture gait while they walk in)
                xs = [p[0] for p in h]; ys = [p[1] for p in h]
                return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5 > GAIT_MOTION_PX

            crops = crop_boxes(frame, [tr.bbox for tr in tracks])
            # only embed/re-id boxes tall enough to matter; tiny distant people stay
            # detected+drawn but skip the shared NPU (big win on crowded cameras).
            eidx = [i for i, tr in enumerate(tracks)
                    if (tr.bbox[3] - tr.bbox[1]) >= MIN_EMBED_H and i < len(crops)
                    and hits.get(int(tr.local_id), 0) >= TRACK_MIN_HITS]
            if MAX_EMBED_PER_FRAME and len(eidx) > MAX_EMBED_PER_FRAME:
                # over budget: keep the biggest (closest) boxes so one frame fits the fps slot
                eidx = sorted(eidx, key=lambda i: tracks[i].bbox[3] - tracks[i].bbox[1],
                              reverse=True)[:MAX_EMBED_PER_FRAME]
                eidx.sort()
            crops_e = [crops[i] for i in eidx]
            t1 = time.perf_counter(); embs_e = emb.embed(crops_e) if crops_e else None; tmb = (time.perf_counter() - t1) * 1000

            tfc = 0.0
            if face is not None and crops_e and frames % fe_every == 0:
                t1 = time.perf_counter(); fembs = face.embed(crops_e); tfc = (time.perf_counter() - t1) * 1000
                for j, i in enumerate(eidx):
                    if j < len(fembs):
                        fcache[tracks[i].local_id] = fembs[j]
            tgt = 0.0
            if gait is not None and eidx and frames % ga_every == 0:
                # gait only for people actually WALKING; seated/standing keep their
                # cached gait (captured while moving) and skip the heavy NPU sequence.
                gtracks = [tracks[i] for i in eidx if _moving(tracks[i].local_id)]
                if gtracks:
                    t1 = time.perf_counter(); gembs = gait.embed_tracks(frame, gtracks, str(sid)); tgt = (time.perf_counter() - t1) * 1000
                    for tr, g in zip(gtracks, gembs):
                        if g is not None:
                            gcache[tr.local_id] = g

            gids = [None] * len(tracks)
            t_sec = (clip_t0 + getattr(dec, "n", 0) / src_fps) if use_vclock                 else (time.perf_counter() - wall0)
            if embs_e is not None and len(embs_e) == len(eidx) and eidx:
                live = {tr.local_id for tr in tracks}
                fcache = {k: v for k, v in fcache.items() if k in live}
                gcache = {k: v for k, v in gcache.items() if k in live}
                # one round-trip to the central gallery: send raw embeddings, get global ids
                items = [{"lid": tracks[i].local_id, "a": _ser(embs_e[j]),
                          "f": _ser(fcache.get(tracks[i].local_id)), "g": _ser(gcache.get(tracks[i].local_id)),
                          "c": _ser(_color_hist(crops[i]))}
                         for j, i in enumerate(eidx)]
                seq += 1
                req_q.put({"t": "m", "sid": sid, "cam": camera, "ts": t_sec, "seq": seq, "items": items})
                try:
                    r = resp_q.get(timeout=3.0)
                    egids, edists = r.get("gids", []), r.get("dists", [])
                    enames = r.get("names", [])
                except Exception:
                    egids, edists, enames = [None] * len(eidx), [99.0] * len(eidx), []
                for j, i in enumerate(eidx):
                    gid = egids[j] if j < len(egids) else None
                    gids[i] = gid
                    gids_seen.add(gid); reid_q += 1
                    if gid is not None:
                        last_gid[int(tracks[i].local_id)] = gid
                    nm = enames[j] if j < len(enames) else None
                    if nm:
                        last_name[int(tracks[i].local_id)] = nm
                    if j < len(edists) and edists[j] <= 1.0:
                        reid_hits += 1
                    if gid is not None:
                        pid = sid * 100000 + gid
                        if pid not in sent:
                            sent.add(pid)
                            try:
                                ok2, jb = cv2.imencode(".jpg", crops[i])
                                if ok2:
                                    crop_q.put_nowait((pid, jb.tobytes()))
                            except Exception:
                                pass
            # tracks not embedded this frame (capped out / too small) keep their last
            # known id so the label stays stable instead of flickering to '?'
            last_gid = {k: v for k, v in last_gid.items() if k in live_ids}
            last_name = {k: v for k, v in last_name.items() if k in live_ids}
            for i, tr in enumerate(tracks):
                if gids[i] is None:
                    gids[i] = last_gid.get(int(tr.local_id))

            frames += 1
            dt = time.perf_counter() - t0
            # effective (paced) cycle time: never faster than the target slot, so the
            # reported fps reflects the throttle, not the raw capability.
            eff = max(dt, 1.0 / TARGET_FPS) if TARGET_FPS > 0 else dt
            inst = 1.0 / eff if eff > 0 else 0
            fps = inst if fps == 0 else (1 - a) * fps + a * inst
            dpf = (1 - a) * dpf + a * len(boxes)
            tdec = (1 - a) * tdec + a * td; tdet = (1 - a) * tdet + a * tdt; ttk = (1 - a) * ttk + a * ttr
            tem = (1 - a) * tem + a * tmb
            if tfc > 0: tfa = (1 - a) * tfa + a * tfc   # true per-run cost (not amortized over cadence)
            if tgt > 0: tga = (1 - a) * tga + a * tgt

            try:
                PW = 640; h, w = frame.shape[:2]; scl = PW / w
                small = cv2.resize(frame, (PW, int(h * scl)))
                for idx, tr in enumerate(tracks):
                    gid = gids[idx] if idx < len(gids) else None
                    x1, y1, x2, y2 = [int(v * scl) for v in tr.bbox]
                    col = ((gid * 37) % 255, (gid * 91) % 255, (gid * 17) % 255) if gid else (150, 150, 150)
                    cv2.rectangle(small, (x1, y1), (x2, y2), col, 2)
                    # named person -> show the NAME; otherwise the anonymous id
                    nm = last_name.get(int(tr.local_id))
                    lab = nm if nm else (("P%d" % gid) if gid else "?")
                    (tw, th), _ = cv2.getTextSize(lab, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
                    cv2.rectangle(small, (x1, max(0, y1 - th - 7)), (x1 + tw + 6, y1), col, -1)
                    cv2.putText(small, lab, (x1 + 3, max(12, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2, cv2.LINE_AA)
                cv2.putText(small, "#%d  %.0f fps  %d ppl  %d ids" % (sid, fps, len(tracks), len(gids_seen)),
                            (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
                ok2, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 72])
                if ok2:
                    with open(shm_tmp, "wb") as f:
                        f.write(buf.tobytes())
                    os.replace(shm_tmp, shm)
            except Exception:
                pass

            now = time.perf_counter()
            if now - last > 0.33:
                last = now
                mdict.update(dict(
                    id=sid, source=source, running=True, err=None, fps=round(fps, 1), frames=frames,
                    det_per_frame=round(dpf, 1), ms_decode=round(tdec, 1), ms_detect=round(tdet, 1),
                    ms_track=round(ttk, 1), ms_embed=round(tem, 1), ms_face=round(tfa, 1), ms_gait=round(tga, 1),
                    face=face is not None, gait=gait is not None, decode="iGPU" if hw else "CPU",
                    reid_ids=len(gids_seen), reid_hit_rate=round(100 * reid_hits / reid_q, 1) if reid_q else 0.0,
                    # video position on the SHARED recording timeline: with the video
                    # clock on, these track each other across cameras; with wall time
                    # they drift apart as fast streams race ahead through the clip.
                    vtime=round(t_sec, 1), vclock=bool(use_vclock)))

            # PACING. Two different things have to be true at once:
            #   1. every camera must sit at the SAME position in the recording, and
            #   2. no stream may process faster than TARGET_FPS.
            # The old loop only did (2): it burned surplus decoded frames until the
            # wall slot elapsed, so a quiet camera raced through the clip while a
            # crowded one crawled -- 16 minutes apart after 5 minutes of running.
            # Now the decoder is driven to the position real time says it should be
            # at, shared by all streams via APP_T0. A stream that cannot keep up
            # DROPS frames to catch up instead of falling behind.
            if use_vclock:
                slot = 1.0 / TARGET_FPS if TARGET_FPS > 0 else 0.0
                # Bounded catch-up: skip at most CATCHUP_MAX_S of video per cycle.
                # Unbounded, a stream that fell behind spins here decoding all it
                # missed and never processes another frame -- which is how
                # late-added streams froze at position 0 producing no ids.
                burn = int(CATCHUP_MAX_S * src_fps)
                while True:
                    want = int((time.time() - APP_T0) * src_fps)
                    if dec.n < want and burn > 0:
                        if not dec.read()[0]:
                            break
                        burn -= 1
                        continue
                    if slot and (time.perf_counter() - t0) < slot:
                        time.sleep(0.005)
                        continue
                    break
            elif TARGET_FPS > 0:
                slot = 1.0 / TARGET_FPS
                while (time.perf_counter() - t0) < slot:
                    if not dec.read()[0]:
                        break
        try:
            req_q.put({"t": "unreg", "sid": sid})
        except Exception:
            pass
        try:
            dec.release()
        except Exception:
            pass
    except Exception as e:
        try:
            mdict.update(dict(id=sid, source=source, running=False, err=str(e)[:140], fps=0.0))
        except Exception:
            pass
    try:
        if os.path.exists(shm):
            os.remove(shm)
    except Exception:
        pass


class WorkerHandle:
    """Manager-side handle for one stream PROCESS (metrics via shared dict)."""

    def __init__(self, sid, source, camera, do_face, do_gait):
        self.id = sid
        self.source = source
        self.mdict = MGR.dict(dict(
            id=sid, source=source, running=True, err=None, fps=0.0, frames=0, det_per_frame=0.0,
            ms_decode=0.0, ms_detect=0.0, ms_track=0.0, ms_embed=0.0, ms_face=0.0, ms_gait=0.0,
            face=do_face, gait=do_gait, decode="starting…", reid_ids=0, reid_hit_rate=0.0))
        self.stop_ev = MGR.Event()
        self.resp_q = MGR.Queue()   # central re-id service -> this stream (global ids)
        self.pool_resp = {k: MGR.Queue() for k in POOL_KINDS}   # device servers -> this stream
        self.proc = multiprocessing.Process(
            target=run_stream, daemon=True,
            args=(sid, source, camera, do_face, do_gait, self.mdict, CROP_Q, self.stop_ev,
                  DESIRED_THR, REID_REQ_Q, self.resp_q, POOL_Q, self.pool_resp))

    def start(self):
        self.proc.start()

    def stop(self):
        try:
            self.stop_ev.set()
            self.proc.join(timeout=3)
            if self.proc.is_alive():
                self.proc.terminate()
        except Exception:
            pass
        try:
            p = _shm_path(self.id)
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    def snapshot(self):
        try:
            return dict(self.mdict)
        except Exception:
            return {"id": self.id, "source": self.source, "running": False, "err": "lost", "fps": 0.0}


# ---------------- device sampler ----------------
class DeviceSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.metrics = {}
        npu = (glob.glob("/sys/devices/pci*/*/npu_busy_time_us") or
               glob.glob("/sys/devices/pci*/*/*/npu_busy_time_us"))
        self.npu_file = npu[0] if npu else None
        self.gpu_json = f"/tmp/streamapp_gpu_{APP_TAG}.json"
        self._gt = None
        self._hist = []   # last DEV_SMOOTH_N raw samples

    def _stop_gpu(self):
        """intel_gpu_top runs under sudo, so it is NOT killed by our process dying --
        every restart used to leave one behind burning ~4% CPU forever. Kill ours,
        then sweep any stale one still writing to this app's json (previous run that
        was SIGKILLed). The -o path carries APP_TAG, so this never touches the other
        app's sampler."""
        gt, self._gt = self._gt, None
        if gt is not None:
            for fn in (gt.terminate, gt.kill):
                try:
                    fn()
                    gt.wait(timeout=2)
                    break
                except Exception:
                    continue
        try:
            subprocess.run(["sudo", "pkill", "-9", "-f", f"intel_gpu_top.*{self.gpu_json}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass

    def _start_gpu(self):
        self._stop_gpu()          # sweep leftovers from a previous run first
        try:
            self._gt = subprocess.Popen(
                ["sudo", "intel_gpu_top", "-J", "-s", "1000", "-o", self.gpu_json],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            atexit.register(self._stop_gpu)
        except Exception:
            self._gt = None

    def _npu(self):
        try:
            return int(open(self.npu_file).read())
        except Exception:
            return None

    def _gpu_engines(self):
        try:
            t = open(self.gpu_json).read()[-262144:]  # wide tail: one sample is huge with many GPU clients
        except Exception:
            return {}
        # Parse the GLOBAL "engines" block of the last sample, NOT per-client
        # "engine-classes" (busy=0, and quoted). With many clients a small tail
        # lands inside the client blocks and misses the global values -> shows 0.
        gi = t.rfind('"engines"')
        if gi >= 0:
            ci = t.find('"clients"', gi)
            blk = t[gi:ci] if ci > gi else t[gi:gi + 6000]
        else:
            blk = t
        def eng(name):
            v = re.findall('"%s":\\s*{\\s*"busy":\\s*"?([\\d.]+)' % re.escape(name), blk)
            return float(v[-1]) if v else 0.0
        pw = re.findall('"GPU":\\s*"?([\\d.]+)', t)
        return {"gpu_compute": eng("Compute"), "gpu_render": eng("Render/3D"),
                "gpu_video": eng("Video"), "gpu_power_w": float(pw[-1]) if pw else 0.0}

    def run(self):
        import psutil
        self._start_gpu()
        psutil.cpu_percent(None)
        nb0, t0 = self._npu(), time.time()
        while True:
            time.sleep(1.0)
            cpu = psutil.cpu_percent(None)
            vm = psutil.virtual_memory()
            nb1, t1 = self._npu(), time.time()
            npu = 0.0
            if nb0 is not None and nb1 is not None and t1 > t0:
                npu = min(100.0, (nb1 - nb0) / ((t1 - t0) * 1e6) * 100)
            nb0, t0 = nb1, t1
            m = {"cpu_pct": round(cpu, 1), "cpu_cores_busy": round(cpu / 100 * NCORES, 1),
                 "ncores": NCORES,
                 "ram_used_gb": round(vm.used / 1e9, 1), "ram_pct": vm.percent,
                 "npu_pct": round(npu, 1)}
            m.update({k: round(v, 1) for k, v in self._gpu_engines().items()})
            # raw 1 s samples of a bursty workload swing wildly and read as noise.
            # Report a 5 s rolling mean; keep the instantaneous value alongside.
            self._hist.append(m)
            if len(self._hist) > DEV_SMOOTH_N:
                del self._hist[0]
            avg = {}
            for k in m:
                vals = [h[k] for h in self._hist if isinstance(h.get(k), (int, float))]
                avg[k] = round(sum(vals) / len(vals), 1) if vals else m[k]
            avg["ncores"] = NCORES
            avg["window_s"] = len(self._hist)
            avg["instant"] = m
            self.metrics = avg


SAMPLER = DeviceSampler()


# ---------------- HTTP ----------------
def _global():
    with WLOCK:
        ws = [w.snapshot() for w in WORKERS.values()]
    total_fps = round(sum(w.get("fps", 0) for w in ws), 1)
    rs = dict(REID_STAT) if REID_STAT else {}
    reid = {"persons": rs.get("persons", 0),            # unique people across ALL cameras (one shared gallery)
            "live": rs.get("live", 0),                  # ids currently alive in the gallery
            "match_rate": rs.get("hit_rate", 0.0),      # % queries linked to an existing id (coverage, NOT accuracy)
            "queries": rs.get("queries", 0),
            "fusion": "camera_aware_gait (cross-cam, shared gallery)",
            "thr": round(DESIRED_THR, 3) if DESIRED_THR else 0.141,
            # how many live ids were actually seen in more than one camera
            "multi_cam_ids": rs.get("multi_cam_ids", 0),
            "cams_per_id": rs.get("cams_per_id", {}),
            "top_pairs": rs.get("top_pairs", {}),
            # face-recognition naming (empty unless ENROLLED_FACES is set)
            "names": rs.get("names", {}),
            "n_named": rs.get("n_named", 0),
            # persistent gid rejoin store (zero unless REJOIN_STORE is set)
            "store_ids": rs.get("store_ids", 0),
            "rejoins": rs.get("rejoins", 0),
            "rejoins_by_mod": rs.get("rejoins_by_mod", {}),
            "store_exemplars": rs.get("store_exemplars", {}),
            "store_evicted": rs.get("store_evicted", 0),
            "store_max_age_h": rs.get("store_max_age_h", 0)}
    return {"streams": ws, "n_streams": len(ws), "total_fps": total_fps,
            "devices": SAMPLER.metrics, "reid": reid, "irra": IRRA.stats(),
            "config": {"detector": f"{DET_MODEL}@{DET_DEV}", "appearance": f"{EMB_MODEL}@{EMB_DEV}",
                       "face": f"{FACE_MODEL}@{FACE_DEV}+SCRFD@GPU", "gait": f"{GAIT_MODEL}@{GAIT_DEV}+seg@{SEG_DEV}",
                       "decode": (f"iGPU VA-API, GPU-scaled to {DECODE_H}p" if DECODE_H else "iGPU VA-API full-res") if _HW_OK else "CPU (software)",
                       "cadence": f"face 1/{FACE_EVERY} · gait 1/{GAIT_EVERY} frames",
                       "guards": f"colour-gate {COLOUR_GATE_THR} + cross-cam face-veto",
                       "det_conf": DET_CONF, "proc_every": PROC_EVERY,
                       "pacing": f"target {TARGET_FPS:g} fps/stream · max {MAX_EMBED_PER_FRAME} embeds/frame"}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")   # always fresh (dashboard + metrics survive restarts)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (Path(__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html")
        if self.path == "/api/metrics":
            return self._send(200, json.dumps(_global()))
        if self.path == "/api/sample_videos":
            vids = sorted(str(p) for p in (ROOT / "trimmed_clips").glob("*.mp4"))
            return self._send(200, json.dumps(vids))
        m = re.match(r"/api/crop/(\d+)", self.path)
        if m:
            with CROP_LOCK:
                c = CROP_STORE.get(int(m.group(1)))
            if c is None:
                return self._send(404, b"")
            ok, buf = cv2.imencode(".jpg", c)
            return self._send(200, buf.tobytes(), "image/jpeg")
        m = re.match(r"/api/frame/(\d+)", self.path)
        if m:  # single current frame (self-healing: dashboard re-polls this, never a stuck stream)
            sid = int(m.group(1))
            try:
                with open(_shm_path(sid), "rb") as f:
                    jb = f.read()
            except Exception:
                jb = None
            if not jb:
                return self._send(404, "{}")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(jb)
            except Exception:
                pass
            return
        m = re.match(r"/api/mjpeg/(\d+)", self.path)
        if m:
            sid = int(m.group(1)); shm = _shm_path(sid)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    with WLOCK:
                        alive = sid in WORKERS
                    if not alive:
                        break
                    jb = None
                    try:
                        with open(shm, "rb") as f:
                            jb = f.read()
                    except Exception:
                        jb = None
                    if jb:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jb)).encode())
                        self.wfile.write(jb)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        return self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or "{}")
        if self.path == "/api/streams":
            src = str(data.get("source", "")).strip()
            df, dg = bool(data.get("face", True)), bool(data.get("gait", True))
            cam = str(data.get("camera", "ch10")).strip() or "ch10"
            if not src:
                return self._send(400, '{"error":"no source"}')
            sid = next(_ids)
            w = WorkerHandle(sid, src, cam, df, dg)
            with WLOCK:
                WORKERS[sid] = w
            w.start()
            return self._send(200, json.dumps({"id": sid}))
        if self.path == "/api/search":  # IRRA text -> person search
            return self._send(200, json.dumps(IRRA.search(str(data.get("query", "")).strip())))
        if self.path == "/api/reid_thr":  # re-id distance threshold (shared gallery -> rebuilds/clears ids)
            global DESIRED_THR
            DESIRED_THR = float(data.get("thr", 0.141))
            try:
                REID_REQ_Q.put({"t": "thr", "thr": DESIRED_THR})
            except Exception:
                pass
            return self._send(200, json.dumps({"thr": DESIRED_THR, "note": "resets the shared gallery"}))
        if self.path == "/api/add_n":  # add N copies of a source for load testing
            src = str(data.get("source", "")).strip()
            k = int(data.get("n", 1))
            df, dg = bool(data.get("face", True)), bool(data.get("gait", True))
            cam = str(data.get("camera", "ch10")).strip() or "ch10"
            ids = []
            for _ in range(k):
                sid = next(_ids)
                w = WorkerHandle(sid, src, cam, df, dg)
                with WLOCK:
                    WORKERS[sid] = w
                w.start(); ids.append(sid); time.sleep(0.4)
            return self._send(200, json.dumps({"ids": ids}))
        return self._send(404, "{}")

    def do_DELETE(self):
        m = re.match(r"/api/streams/(\d+)", self.path)
        if m:
            wid = int(m.group(1))
            with WLOCK:
                w = WORKERS.pop(wid, None)
            if w:
                w.stop()
            return self._send(200, "{}")
        if self.path == "/api/streams":  # clear all
            with WLOCK:
                workers = list(WORKERS.values())
                WORKERS.clear()
            for w in workers:
                w.stop()
            with CROP_LOCK:
                CROP_STORE.clear()
            with IRRA.lock:
                IRRA.emb.clear()
            try:
                REID_REQ_Q.put({"t": "thr", "thr": DESIRED_THR})  # rebuild -> empty shared gallery
            except Exception:
                pass
            return self._send(200, "{}")
        return self._send(404, "{}")


def _crop_drain():
    """Manager thread: pull new-person crops from worker processes into CROP_STORE
    (the IRRA search index source)."""
    while True:
        try:
            pid, jb = CROP_Q.get(timeout=1.0)
        except Exception:
            continue
        try:
            img = cv2.imdecode(np.frombuffer(jb, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                with CROP_LOCK:
                    CROP_STORE[pid] = img
        except Exception:
            pass


def main():
    global MGR, CROP_Q, REID_REQ_Q, REID_STAT
    try:
        multiprocessing.set_start_method("fork")
    except RuntimeError:
        pass
    MGR = multiprocessing.Manager()
    CROP_Q = MGR.Queue()
    REID_REQ_Q = MGR.Queue()
    REID_STAT = MGR.dict(persons=0, live=0, matches=0, queries=0, hit_rate=0.0)
    reid_stop = MGR.Event()
    multiprocessing.Process(target=reid_service, args=(REID_REQ_Q, reid_stop, DESIRED_THR),
                            daemon=True).start()
    for kind in POOL_KINDS:
        POOL_Q[kind] = MGR.Queue()
        for _ in range(POOL_REPLICAS[kind]):
            multiprocessing.Process(target=infer_pool.infer_server,
                                    args=(kind, POOL_CFG[kind], POOL_Q[kind], reid_stop),
                                    daemon=True).start()
    SAMPLER.start()
    threading.Thread(target=_crop_drain, daemon=True).start()
    print(f"[POOL] shared device servers: det={DET_MODEL}@{DET_DEV} embed={EMB_MODEL}@{EMB_DEV} "
          f"face={FACE_MODEL}@{FACE_DEV} gait={GAIT_MODEL}@{GAIT_DEV}+seg@{SEG_DEV}", flush=True)
    print(f"[POOL] replicas: {POOL_REPLICAS} -- constant model memory, streams decode/track only", flush=True)
    print(f"Pooled stream dashboard on http://0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
