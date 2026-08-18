"""Edge MTMC pipeline -- TWO-TIER build (local tracking + async global re-id).

Forked from streamapp_pool.py. Same models, same gallery, same fusion, same
thresholds -- the ONLY change is WHEN global matching runs.

  streamapp_pool (:8082): every embedded frame round-trips to ONE central
      gallery and BLOCKS for the global id. That single-threaded gallery is the
      scaling wall -- 20 cams x 5 fps = 100 blocking match calls/sec through one
      Python core, so streams queue and fps falls while the devices sit idle.

  this build (:8083): the TRACKER keeps a person's box within a camera (no
      gallery needed frame to frame). The global gallery is queried only when a
      track is NEW / matured / due for re-sync -- on a track SUMMARY (best
      appearance+face+gait prototype), NOT every frame. The query is async: the
      worker fires the summary and never blocks; global ids arrive a frame or two
      later and are applied to the box. Same match logic, ~1/150th the calls.

Re-id quality is meant to be unchanged (same embeddings/fusion/thresholds) or
better (whole-track prototype beats a single frame). Validate with the crop
audit against :8082 before trusting -- the decision ORDER differs.

Local id shows as "T<n>" until the first global reply (a fraction of a second).

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
from collections import deque
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Batched iGPU inference: fuse detect/face across streams into one dispatch. Set
# BEFORE importing infer_pool so its module-level flags (and the spawned device
# servers that inherit this env) see it. :8082 never sets these -> unchanged.
os.environ.setdefault("POOL_DET_BATCH", "1")
os.environ.setdefault("POOL_FACE_BATCH", "1")
# async detection infer queue: overlaps a frame's CPU letterbox with the prior
# frame's GPU infer. Small free win in the pipeline (~11% lower det latency, ~5%
# more fps @18 streams; identical model/outputs). Set 0 to disable.
os.environ.setdefault("DET_ASYNC", "1")
from MTMC.ov_backends import OVReidEmbedder, OVDetector  # noqa: E402,F401
from MTMC import infer_pool  # noqa: E402

MODELS = ROOT / "models"
PORT = int(os.environ.get("PORT", 8083))    # two-tier dashboard (pool=8082, INT8=8081, FP16=8080)
APP_TAG = os.environ.get("APP_TAG", "2tier")  # namespace for shm frames + gpu telemetry (distinct from :8082)
DET_DEV = os.environ.get("DET_DEV", "GPU")
# split detection replicas across devices to BALANCE the load: iGPU carries det +
# SCRFD + seg (conv-heavy), NPU carries embed + gait and is idle. Putting some det
# replicas on the NPU offloads the iGPU; competing consumers on one queue auto-
# distribute by device speed. e.g. "GPU,GPU,NPU,NPU". Empty -> all on DET_DEV.
# (NPU needs static batch -> set POOL_DET_BATCH=0 when any replica is on NPU.)
DET_DEVICES = [d.strip() for d in os.environ.get("DET_DEVICES", "").split(",") if d.strip()]
EMB_DEV = os.environ.get("EMB_DEV", "NPU")
DET_MODEL = os.environ.get("DET_MODEL", "yolo11s_int8.xml")   # INT8 detector
DET_CONF = float(os.environ.get("DET_CONF", "0.35"))     # detection floor. At 0.25 the detector emitted boxes
#                                                          # on a solid black region and on the burned-in TIMESTAMP
#                                                          # overlay; each became a global id and then "matched"
#                                                          # something in another camera. Audited on 5 real cameras:
#                                                          # 0.25 -> 15/33 correct cross-cam links, 8 anchored on
#                                                          # non-people; 0.35 -> 18/38 correct, 6 non-people.
DET_IOU = float(os.environ.get("DET_IOU", "0.50"))       # detector NMS IoU. Lower removes duplicate boxes; too low can
#                                                          # suppress nearby/overlapping people in the office.
GAIT_MOTION_PX = int(os.environ.get("GAIT_MOTION_PX", "22"))  # only run gait for tracks whose center moved > this over
GAIT_MOTION_WIN = 8                                            # the last N frames; seated/standing people skip gait (useless + heavy)
MAX_EMBED_PER_FRAME = int(os.environ.get("MAX_EMBED_PER_FRAME", "8"))  # cap crops embedded per frame (biggest/closest
#                                                        # first); keeps one heavy frame under the target-fps slot on crowded
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
TRACK_MAX_AGE = int(os.environ.get('TRACK_MAX_AGE', '100'))   # tracker lost-track buffer in frames (~8s @5fps); higher = a track survives a longer detection gap (sit/occlusion) instead of dying and re-minting a new id
TRACK_IOU = float(os.environ.get('TRACK_IOU', '0.25'))       # IoU match gate; lower tolerates more box movement between frames
TRACKER = os.environ.get('TRACKER', 'iou')                    # iou (default, unchanged) | sort (Kalman motion, survives crossings/occlusion) | ocsort | bytetrack...
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
TARGET_FPS = float(os.environ.get("TARGET_FPS", "15"))  # pace each stream to this fps: drop source frames to stay
#                                                        # live + throttle fast streams so the shared NPU serves more
#                                                        # streams at target. 0 = unthrottled. Cannot exceed NPU limit.
# The annotated stream is a real 1080p surface.  Keep these dimensions in the
# observation metadata too, so the API canvas, zones and engine-burned boxes all
# apply the exact same source -> display transform.
OUTPUT_W = max(1, int(os.environ.get("OUTPUT_W", "1920")))
OUTPUT_H = max(1, int(os.environ.get("OUTPUT_H", "1080")))
OUTPUT_JPEG_QUALITY = min(100, max(1, int(os.environ.get("OUTPUT_JPEG_QUALITY", "80"))))
# Burn boxes/labels into the served frame. Set 0 to serve a CLEAN frame and let the
# dashboard draw the overlay from /api/live instead: vector strokes and real text stay
# crisp at any tile size, where a 1080p bitmap annotation scaled into a 340px card does
# not. Detection/tracking/re-id are untouched either way -- this is render-only.
DRAW_OVERLAY = bool(int(os.environ.get("DRAW_OVERLAY", "1")))
DRAW_HEADER = bool(int(os.environ.get("DRAW_HEADER", "0")))
FACE_EVERY = int(os.environ.get("FACE_EVERY", 3))  # run face inference every N frames (cache between)
GAIT_EVERY = int(os.environ.get("GAIT_EVERY", 2))  # accumulate a gait silhouette every N
#   frames. Denser = the min_len=20 buffer fills sooner (min_len*N frames) and the
#   sequence is more consecutive (better gait). Was 5 (sequence too sparse) + sync-gated.
# per-detection event log for the HONEST metric (MTMC/honest_metric.py). Off by
# default; set to a dir and each stream writes ev_<sid>.csv with camera,frame,
# track_id,global_id,bbox -- provable merge/fragmentation scoring, no ground truth.
EVENT_LOG_DIR = os.environ.get("EVENT_LOG_DIR", "")
# OFF by default. When set, each embedded detection is dumped (bbox + app/face/gait
# emb) as JSONL for PLATF replay (the re-id-plugin honest-metric gate). Write-only,
# additive: unset => this code never runs and the re-id path is byte-identical.
OBS_DUMP_DIR = os.environ.get("OBS_DUMP_DIR", "")
# cap each per-stream dump: it carries full embeddings per detection and grows to GBs.
# When a file passes the cap it is truncated + restarted (the platform only TAILS new
# appends, so old lines are already consumed). 0 disables the cap. Default 256 MB/stream.
OBS_DUMP_MAX_BYTES = int(os.environ.get("OBS_DUMP_MAX_MB", "256")) * 1024 * 1024
# live same-frame guard: at match time exclude gids held by co-visible tracks in the
# same camera (one person != two boxes/frame). Default on; set 0 to A/B against the
# 23.8% provable-merge baseline the honest metric measured.
SAME_FRAME_GUARD = bool(int(os.environ.get("SAME_FRAME_GUARD", "1")))
# The final same-camera uniqueness gate used to be gated on body_ok, so with ~93% of
# crops failing that gate a track could keep a gid a co-visible track already held --
# committed unchallenged, frame after frame. That is how one gid absorbs a whole
# camera, and a gid that is "present" all the time then blocks every later merge
# through the mapper's co-visibility rule. Strict applies the gate regardless of crop
# quality: a weak crop may REFUSE the contradiction but never picks the replacement.
# Set 0 to restore the old body_ok-gated behaviour.
GID_UNIQUE_STRICT = bool(int(os.environ.get("GID_UNIQUE_STRICT", "1")))
# Seconds a gid stays un-matchable for a track the sink guard just took it from, so the
# loser cannot immediately re-acquire the id it was released from. 0 disables.
GID_DENY_S = float(os.environ.get("GID_DENY_S", "10"))
# multi-frame cross-cam confirmation: a CROSS-CAMERA link (matched gid lives in
# another camera) is not committed on one match -- the track holds its own-camera id
# and the link is committed only after CONFIRM_K consistent matches. Topology can't
# help the ch9<->ch10 overlap pair (concurrent-exclusion is disabled for overlapping
# cameras), so repeated-agreement is the only guard against 1-lucky-match false merges.
CROSS_CONFIRM = bool(int(os.environ.get("CROSS_CONFIRM", "1")))
CONFIRM_K = int(os.environ.get("CONFIRM_K", "3"))
# flicker fixes (honest_metric.flicker showed 77% T->P async-latency + 22% tracker
# re-acquire). LOCAL_REASSOC: a track the tracker re-acquired as a NEW local id,
# spatially continuing a just-lost track, inherits that track's gid immediately
# (no async wait). HIDE_PROVISIONAL: draw an unassigned box with no "T<n>" text so
# the eye sees a box then a label, not "T5"->"P12".
LOCAL_REASSOC = bool(int(os.environ.get("LOCAL_REASSOC", "1")))
FACE_UPGRADE = os.environ.get('FACE_UPGRADE', '1') == '1'   # one-shot cross-cam re-match when a faceless-matched track first gets a valid face
REASSOC_GAP = int(os.environ.get("REASSOC_GAP", "45"))   # frames a lost track stays inheritable
REASSOC_APP_GATE = float(os.environ.get('REASSOC_APP_GATE', '0.3'))  # position-rejoin also requires appearance within this cosine distance when both prototypes exist
REASSOC_REQUIRE_APP = bool(int(os.environ.get("REASSOC_REQUIRE_APP", "0")))  # when ON, position rejoin must have body appearance on BOTH sides
REASSOC_IOU = float(os.environ.get('REASSOC_IOU', '0.3'))  # min IoU for position-rejoin; low is safe because the appearance gate confirms identity
REASSOC_CENTER_FRAC = float(os.environ.get('REASSOC_CENTER_FRAC', '1.5'))  # rejoin also matches by CENTRE distance within ~this fraction of box height (IoU fails on small shifted boxes)
HIDE_PROVISIONAL = bool(int(os.environ.get("HIDE_PROVISIONAL", "1")))
# LOCAL_REASSOC inherits a gid by BOX OVERLAP (position), not appearance -- so a DIFFERENT
# person who steps into the spot a person just left inherits their id (two people, one id).
# Verify: at the re-synced track's next embedding, if its appearance is WAY off the inherited
# gid (a swap), break the sticky bond + re-match. Loose scale so a same-person sparse-embed
# blip (mild divergence) still stays sticky -- only a clear different-person gap breaks it.
REASSOC_VERIFY = bool(int(os.environ.get("REASSOC_VERIFY", "0")))
# absolute appearance distance above which the inherited bond is a DIFFERENT person. 0.5 is
# a clear-different-person gap; same-person sparse-embed blips stay well under it, so this
# only breaks real swaps (no extra churn). Lower = stricter (may over-split), higher = looser.
REASSOC_VERIFY_DIST = float(os.environ.get("REASSOC_VERIFY_DIST", "0.5"))
APP_REINFORCE_MIN_Q = float(os.environ.get("APP_REINFORCE_MIN_Q", "0.0"))  # app exemplars below this do not enrich live/persistent galleries
APP_CACHE_MIN_Q = float(os.environ.get("APP_CACHE_MIN_Q", str(APP_REINFORCE_MIN_Q)))  # app exemplars below this cannot drive local re-association
BODY_ID_MIN_Q = float(os.environ.get("BODY_ID_MIN_Q", str(APP_REINFORCE_MIN_Q)))  # body crop must clear this before it can create/merge identities
BODY_ID_MIN_H = int(os.environ.get("BODY_ID_MIN_H", "140"))  # reject random heads/bottoms even when sharp
BODY_ID_MIN_ASPECT = float(os.environ.get("BODY_ID_MIN_ASPECT", "0.18"))  # width/height lower bound for upright body
BODY_ID_MAX_ASPECT = float(os.environ.get("BODY_ID_MAX_ASPECT", "0.62"))  # width/height upper bound; seated/desk crops are often too wide
BODY_ID_EDGE_MARGIN = int(os.environ.get("BODY_ID_EDGE_MARGIN", "6"))  # frame-edge boxes are partial; don't seed identity
# ---- SECOND, LOOSER TIER -------------------------------------------------------------
# BODY_ID_* is a SEED gate: is this crop clean enough to write into appearance memory and
# form cross-camera links. It was also being used to decide whether a track may hold an id
# at all, and those are different questions. Measured on this deployment: 89% of tracks
# that never got an id never once produced a crop taller than BODY_ID_MIN_H, against a
# median person height of 75-100px -- so most people stayed grey their whole time on
# screen, and each re-approach minted a NEW id, which is itself a source of fragmentation.
# BODY_MATCH_* is the looser MATCH gate. A crop that clears it may keep/continue an
# identity and feed LOCAL_REASSOC, but may NOT write to the gallery and may NOT form a
# cross-camera link -- so gallery purity and cross-camera precision are unchanged.
# 58% of failures were the aspect band, not height, so it is widened too. Set
# BODY_MATCH_MIN_H=0 to collapse this back to the single-gate behaviour.
BODY_MATCH_MIN_H = int(os.environ.get("BODY_MATCH_MIN_H", "85"))
BODY_MATCH_MIN_Q = float(os.environ.get("BODY_MATCH_MIN_Q", "0.30"))
BODY_MATCH_MIN_ASPECT = float(os.environ.get("BODY_MATCH_MIN_ASPECT", "0.15"))
BODY_MATCH_MAX_ASPECT = float(os.environ.get("BODY_MATCH_MAX_ASPECT", "0.85"))
NONBODY_REINFORCE_APP_DIST = float(os.environ.get("NONBODY_REINFORCE_APP_DIST", str(REASSOC_VERIFY_DIST)))
NONBODY_REINFORCE_FACE_FRAC = float(os.environ.get("NONBODY_REINFORCE_FACE_FRAC", "1.0"))
NONBODY_REINFORCE_GAIT_FRAC = float(os.environ.get("NONBODY_REINFORCE_GAIT_FRAC", "1.0"))
DISPLAY_BBOX_SMOOTH = float(os.environ.get("DISPLAY_BBOX_SMOOTH", "0.0"))  # 0 off; e.g. 0.65 smooths overlay only, never crops/re-id
DISPLAY_BBOX_RESET_IOU = float(os.environ.get("DISPLAY_BBOX_RESET_IOU", "0.15"))


def _bb_iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, x2 - x1), max(0.0, y2 - y1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0
NCORES = os.cpu_count() or 16

# ---------------- shared inference pool ----------------
# One server process per device role instead of six models inside every stream.
POOL_KINDS = ("det", "embed", "face", "gait")
# Replicas per role. One server per role would SERIALIZE every stream through a
# single process, which is slower than the old per-stream models. Replicas share
# one request queue as competing consumers, so work spreads with no scheduler.
# Cost is bounded: replicas x one model, not streams x six models.
# Gait is intentionally a single stateful server: its per-track silhouette buffer
# lives inside the server process. Competing gait consumers split one walking
# sequence between independent buffers and destroy the temporal continuity GaitBase
# needs. Detector/embed/face calls are stateless and remain safely replicated.
POOL_REPLICAS = {k: max(1, int(os.environ.get(f"POOL_N_{k.upper()}", d)))
                 for k, d in (("det", 4), ("embed", 4), ("face", 4), ("gait", 1))}
POOL_Q: dict = {}          # kind -> request Queue (filled in main)
FRAME_SHM_BYTES = int(os.environ.get("FRAME_SHM_BYTES", str(1920 * 1080 * 3)))
POOL_CFG = {
    "det":   {"xml": str(MODELS / DET_MODEL),  "device": DET_DEV, "conf": DET_CONF, "iou": DET_IOU},
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
    attempts = int(os.environ.get("RTSP_PROBE_RETRIES", "3")) if _is_rtsp(src) else 1
    timeout_s = float(os.environ.get("RTSP_PROBE_TIMEOUT_S", "20" if _is_rtsp(src) else "8"))
    for attempt in range(attempts):
        try:
            cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0"]
            if _is_rtsp(src):
                cmd.extend(["-rtsp_transport", os.environ.get("RTSP_TRANSPORT", "tcp")])
            cmd.extend(["-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", src])
            out = subprocess.run(cmd,
                                 capture_output=True, text=True, timeout=timeout_s).stdout.strip()
            w, h = out.split("x")[:2]
            return int(w), int(h)
        except Exception:
            if attempt + 1 < attempts:
                time.sleep(float(os.environ.get("RTSP_PROBE_RETRY_S", "1.0")))
    return None


def _is_rtsp(src):
    return isinstance(src, str) and src.startswith("rtsp://")


XCAM_THR = float(os.environ.get("XCAM_THR", "0.85"))  # cross-camera bar, as a fraction of the
XCAM_NOMODAL_THR = float(os.environ.get('XCAM_NOMODAL_THR', '0.6'))  # cross-cam bar when NO corroborating modality (face/gait) is present -- stricter than XCAM_THR so appearance-alone can't merge look-alike uniforms (fraction of calibrated appearance threshold)
XCAM_QUERY_MIN_Q = float(os.environ.get("XCAM_QUERY_MIN_Q", "0.45"))
XCAM_EXEMPLAR_MIN_Q = float(os.environ.get("XCAM_EXEMPLAR_MIN_Q", "0.45"))
OFF_CHAIN_CAMS = tuple(c.strip() for c in os.environ.get('OFF_CHAIN_CAMS', 'ch16').split(',') if c.strip())  # cameras NOT in the physical chain -> never cross-link (physical constraint, independent of learned windows)
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
# --- two-tier scheduling: how often a local track queries the global gallery ---
LOCAL_MIN_HITS = int(os.environ.get("LOCAL_MIN_HITS", "3"))     # frames a track must survive
#   before its FIRST global sync (skip blips; ensures a decent prototype).
SYNC_EVERY_S = float(os.environ.get("SYNC_EVERY_S", "2.0"))     # re-sync a live track this often
#   to refine its global id as better face/gait prototypes arrive (video time).
VIDEO_CLOCK = os.environ.get("VIDEO_CLOCK", "1") == "1"
DECIMATE = os.environ.get("DECIMATE", "1") == "1"   # drop to TARGET_FPS inside ffmpeg,
#                                                    # before the GPU->host download
CATCHUP_MAX_S = float(os.environ.get("CATCHUP_MAX_S", "2.0"))  # video seconds a stream
#                                                               # may skip per cycle to re-sync


def _probe_duration(src):
    """Clip length, used to wrap a late stream's seek back into the file."""
    try:
        cmd = ["ffprobe", "-v", "error"]
        if isinstance(src, str) and src.startswith("rtsp://"):
            cmd.extend(["-rtsp_transport", os.environ.get("RTSP_TRANSPORT", "tcp")])
        cmd.extend(["-show_entries", "format=duration", "-of", "csv=p=0", src])
        out = subprocess.run(cmd,
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
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0"]
        if isinstance(src, str) and src.startswith("rtsp://"):
            cmd.extend(["-rtsp_transport", os.environ.get("RTSP_TRANSPORT", "tcp")])
        cmd.extend(["-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", src])
        out = subprocess.run(cmd,
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
        rtsp_args = (["-rtsp_transport", os.environ.get("RTSP_TRANSPORT", "tcp")]
                     if isinstance(src, str) and src.startswith("rtsp://") else [])
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-hwaccel", "vaapi"] + \
              (["-hwaccel_output_format", "vaapi"] if scaled else []) + \
              ["-vaapi_device", VAAPI_DEV, "-stream_loop", "-1"] + \
              (["-ss", f"{start_s:.3f}"] if start_s > 0.5 else []) + rtsp_args + ["-i", src] + \
              (["-vf", vf] if vf else []) + \
              ["-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        # Keep ffmpeg's stderr instead of discarding it: when the VA-API path fails
        # (unsupported H.264 profile, missing render node, bad filter chain) the reason
        # is ONLY in here, and make_decoder's fallback to CPU is otherwise silent.
        # A drain THREAD is required -- an undrained PIPE fills its 64K buffer and
        # blocks ffmpeg mid-decode, and cameras do emit sporadic errors ("corrupted
        # macroblock") for as long as they run. The deque bounds what is retained.
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  bufsize=self.fsize)
        self.err_lines = deque(maxlen=40)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.hw = True
        # Frames pulled, INCLUDING pacing drops. Seeded from the seek so video
        # time is right immediately -- a late stream must not decode its way
        # forward through everything it missed.
        rate = self.out_fps or src_fps or 25.0
        self.n = int(start_s * rate) if start_s > 0.5 else 0

    def _drain_stderr(self):
        """Consume ffmpeg's stderr forever, keeping only the last few lines."""
        try:
            for line in iter(self.p.stderr.readline, b""):
                line = line.decode("utf-8", "replace").strip()
                if line:
                    self.err_lines.append(line)
        except Exception:
            pass

    def why_failed(self, limit: int = 3) -> str:
        """The last ffmpeg errors, for logging a fallback to CPU. Waits briefly: on a
        failed start the drain thread may not have collected the message yet."""
        for _ in range(20):
            if self.err_lines:
                break
            time.sleep(0.05)
        return " | ".join(list(self.err_lines)[-limit:]) or "no ffmpeg stderr output"

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
        for pipe in (self.p.stdout, self.p.stderr):   # let the drain thread end on EOF
            try:
                pipe.close()
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


def _dec_tag(src):
    """Short label for decoder logs, with any rtsp://user:pass@ credentials stripped."""
    # strip credentials BEFORE truncating: truncation could otherwise cut inside
    # "user:pass@" and leave the password in the log.
    s = re.sub(r"://[^/@]*@", "://", str(src))
    return s if len(s) <= 80 else s[:77] + "..."


def make_decoder(src, start_s=0.0, src_fps=None, out_fps=None):
    """iGPU hardware decoder for file/rtsp sources when available, else CPU.

    start_s SEEKS the source so a stream added late lands where the already
    running cameras are. Decoding forward to that point instead means pushing
    every skipped frame through the pipe, which blocks the worker outright.
    """
    tag = _dec_tag(src)
    why = "source is a device index" if not (isinstance(src, str) and not src.isdigit()) else ""
    strict_hw = _is_rtsp(src) and os.environ.get("STRICT_HW_DECODE", "1") == "1"
    while _HW_OK and isinstance(src, str) and not src.isdigit():
        dims = _probe_dims(src)
        if not dims:
            why = "ffprobe could not read the stream dimensions (source down or timed out?)"
        else:
            try:
                d = HWDecoder(src, dims[0], dims[1], start_s=start_s, src_fps=src_fps,
                              out_fps=out_fps)
                ok, _ = d.read()        # prime: if the vaapi filter chain errored, fall back to CPU
                if ok:
                    print(f"[dec] {tag} decoding on iGPU (VA-API), {dims[0]}x{dims[1]} source",
                          flush=True)
                    return d
                why = f"VA-API decode failed: {d.why_failed()}"
                d.release()
            except Exception as exc:
                why = f"VA-API decoder raised {type(exc).__name__}: {exc}"
        if not strict_hw:
            break
        print(f"[dec] {tag} VA-API startup retry -- {why}", flush=True)
        time.sleep(float(os.environ.get("HW_DECODE_RETRY_S", "1.0")))
    if not _HW_OK:
        why = ("HW_DECODE=0" if not HW_DECODE else
               f"no ffmpeg VA-API support or {VAAPI_DEV} is missing")
        if strict_hw:
            raise RuntimeError(f"strict RTSP hardware decode requested but unavailable: {why}")
    # SILENT fallback here used to be invisible: /api/metrics just started saying
    # decode="CPU". Software decode costs a full-res swscale per frame, so say why.
    print(f"[dec] {tag} FALLING BACK TO CPU (software decode) -- {why}", flush=True)
    return Cv2Decoder(int(src) if str(src).isdigit() else src)


HYST = int(os.environ.get("GID_HYST", 4))
NAME_CLEAR_MISSES = max(1, int(os.environ.get("NAME_CLEAR_MISSES", "2")))
NAME_HOLD_S = max(0.0, float(os.environ.get("NAME_HOLD_S", "8")))
NAME_APP_CLEAR_DIST = float(os.environ.get("NAME_APP_CLEAR_DIST", "0.45"))
# Recognition display and canonical name anchoring use the same enrolled-gallery
# acceptance band. Safety comes from the hard guards below: no same-camera co-visible
# merge, and cross-camera remaps are confirmed/re-written instead of one-shot body
# matches. Live Kiran evidence on cam6/cam7 sits around 0.43-0.44, so the old 0.35
# canonical bar displayed the name but refused to persist the ID.
NAME_CANON_MERGE_THR = float(os.environ.get("NAME_CANON_MERGE_THR", "0.45"))
# After an enrolled face confirms a name, body/gait may preserve/recover that named
# person's canonical gid when later frames have no usable face. Thresholds are in
# fractions of each modality's calibrated threshold. Body-only must be strong; a
# weaker body match needs gait agreement too.
NAME_ANCHOR_APP_FRAC = float(os.environ.get("NAME_ANCHOR_APP_FRAC", "2.0"))
NAME_ANCHOR_GAIT_FRAC = float(os.environ.get("NAME_ANCHOR_GAIT_FRAC", "1.1"))
NAME_ANCHOR_LOG = os.environ.get("NAME_ANCHOR_LOG", "")
FACE_NAME_MIN_DET = float(os.environ.get("FACE_NAME_MIN_DET", os.environ.get("FACE_MIN_DET", "0.65")))
FACE_NAME_MIN_PX = float(os.environ.get("FACE_NAME_MIN_PX", os.environ.get("FACE_MIN_PX", "40")))
FACE_NAME_MIN_Q = float(os.environ.get("FACE_NAME_MIN_Q", "0.25"))


def _face_meta_ok(meta: dict | None) -> bool:
    """Reject weak face chips before they can label/name/reinforce a track."""
    if not meta:
        return False
    try:
        det = float(meta.get("det_score", meta.get("det", 0.0)) or 0.0)
        px = float(meta.get("face_w", meta.get("w", 0.0)) or 0.0)
        q = float(meta.get("face_q", meta.get("q", 0.0)) or 0.0)
    except Exception:
        return False
    return det >= FACE_NAME_MIN_DET and px >= FACE_NAME_MIN_PX and q >= FACE_NAME_MIN_Q


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
        self.adapter = None
        self.row_names: dict[int, str] = {}
        self.threshold = float(threshold)
        if not enrolled_dir:
            return
        try:
            # The platform enrollment workflow writes index.json + vectors.npy.
            # Use its adapter so recognition and enrollment share the exact same
            # decision policy (including margin) and gallery hot reload.
            d = Path(enrolled_dir)
            if (d / "index.json").exists() and (d / "vectors.npy").exists():
                from PLATF.plugins.enroll_gallery import EnrollmentGalleryAdapter
                self.adapter = EnrollmentGalleryAdapter.load(d)
                if self.adapter is None:
                    raise RuntimeError("platform face gallery is empty or invalid")
                status = self.adapter.status()
                print(f"[names] enrolled {status['vectors']} faces / "
                      f"{status['person_count']} people from {enrolled_dir} "
                      f"(platform gallery, hot reload)", flush=True)
                return

            import faiss
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
        return self.adapter is not None or self.index is not None

    def vote(self, face_embs) -> tuple[str, float]:
        """(name, cosine_distance) for one gid's face exemplars. 'Unknown' when
        no face clears the threshold. Faces are already L2-normed by the embedder."""
        if not self.enabled or not face_embs:
            return "Unknown", 999.0
        if self.adapter is not None:
            best = None
            for emb in face_embs:
                matches = self.adapter.search(emb, k=1)
                if matches and (best is None or matches[0][1] < best[1]):
                    best = matches[0]
            if best is None:
                return "Unknown", 999.0
            name, dist = best
            return (name if float(dist) <= self.threshold else "Unknown"), float(dist)
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
            # The config requested quality_topk, but the live constructor previously
            # omitted policy and silently used FIFO. Body queries now carry a measured
            # crop quality, so retaining the best K temporal exemplars is meaningful.
            policy=os.environ.get(
                "GALLERY_POLICY", (cfg.get("gallery") or {}).get("policy", "quality_topk")),
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
            cross_camera_no_modal_threshold=XCAM_NOMODAL_THR,
            # Cross-camera BODY links may only be created from good query crops
            # and good stored exemplars. Weak crops still update same-camera
            # continuity; they just cannot pollute the cross-camera gallery.
            cross_camera_query_min_quality=float(os.environ.get(
                "XCAM_QUERY_MIN_Q", cfg.get("cross_camera_query_min_quality", XCAM_QUERY_MIN_Q))),
            cross_camera_exemplar_min_quality=float(os.environ.get(
                "XCAM_EXEMPLAR_MIN_Q", cfg.get("cross_camera_exemplar_min_quality", XCAM_EXEMPLAR_MIN_Q))),
            off_chain_cameras=OFF_CHAIN_CAMS,
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
        match_params = set(inspect.signature(self._MMG.match).parameters)
        force_params = set(inspect.signature(self._MMG.force_assign).parameters)
        self._supports_exclude = "exclude_gids" in match_params
        self._supports_face_quality = "face_quality" in match_params
        self._supports_force_face_quality = "face_quality" in force_params
        self.g = self._MMG(**cfg)
        try:
            s = self.g.stats()
            print(
                "[reid] gallery "
                f"policy={s.get('policy')} "
                f"xcam={s.get('cross_camera_match_threshold')} "
                f"xcam_no_modal={s.get('cross_camera_no_modal_threshold')} "
                f"xcam_query_min_q={s.get('cross_camera_query_min_quality')} "
                f"xcam_exemplar_min_q={s.get('cross_camera_exemplar_min_quality')}",
                flush=True,
            )
        except Exception:
            pass
        self.matches = 0
        self.total = 0

    def match(self, sm, fe, cam, t, ge, exclude_gids=None, quality=1.0,
              face_quality=1.0, gait_quality=1.0):
        with self.lock:
            self.total += 1
            kw = {"quality": quality}
            if self._supports_face_quality:
                kw.update(face_quality=face_quality, gait_quality=gait_quality)
            if self._supports_exclude:
                gid, dist = self.g.match(sm, fe, cam, t, ge, exclude_gids=exclude_gids,
                                         **kw)
            else:
                gid, dist = self.g.match(sm, fe, cam, t, ge, **kw)
            if dist <= 1.0:
                self.matches += 1
            return gid, dist

    def force_assign(self, gid, sm, fe, cam, t, ge, face_quality=1.0, gait_quality=1.0):
        with self.lock:
            if self._supports_force_face_quality:
                self.g.force_assign(gid, sm, fe, cam, t, ge,
                                    face_quality=face_quality, gait_quality=gait_quality)
            else:
                self.g.force_assign(gid, sm, fe, cam, t, ge)

    def appearance_dist(self, gid, sm):
        """Min appearance distance between sm and this gid's exemplars (999 if unknown).
        Used to verify a position-inherited (LOCAL_REASSOC) sticky bond is still the same
        person -- a large distance means the track swapped onto someone else."""
        with self.lock:
            e = self.g.gallery.get(int(gid)) if hasattr(self.g, "gallery") else None
            if e is None:
                return 999.0
            try:
                d = self.g._link_dist(sm, e.app_embs, e.app_qualities, 0.0)
            except Exception:
                return 999.0
            return float(d) if d is not None else 999.0

    def gait_dist(self, gid, ge):
        """Min gait distance between ge and this gid's gait exemplars (None if either
        side has no valid gait)."""
        if ge is None or float(np.linalg.norm(np.asarray(ge, np.float32))) <= 1e-6:
            return None
        with self.lock:
            e = self.g.gallery.get(int(gid)) if hasattr(self.g, "gallery") else None
            if e is None or not getattr(e, "gait_embs", None):
                return None
            try:
                d = self.g._min_dist(ge, e.gait_embs)
            except Exception:
                return None
            return float(d) if d is not None else None

    def face_dist(self, gid, fe):
        """Min face distance between fe and this gid's face exemplars.

        Used only as a consistency check before a BODY-failed frame is allowed to
        enrich an existing identity.  A valid face can be strong evidence, but if a
        tracker swapped people and the body crop is too partial to verify, blindly
        writing that face into the old gid contaminates future Re-ID/name/search.
        """
        if fe is None or float(np.linalg.norm(np.asarray(fe, np.float32))) <= 1e-6:
            return None
        with self.lock:
            e = self.g.gallery.get(int(gid)) if hasattr(self.g, "gallery") else None
            if e is None or not getattr(e, "face_embs", None):
                return None
            try:
                d = self.g._min_dist(fe, e.face_embs)
            except Exception:
                return None
            return float(d) if d is not None else None

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

    def is_local(self, gid):
        """True for a still-unpromoted `mint_local` id (no appearance evidence yet)."""
        if gid is None:
            return False
        with self.lock:
            try:
                return bool(self.g.is_local(int(gid)))
            except AttributeError:
                return False

    def mint_local(self, cam, t):
        """A gid with no appearance evidence, for a match-grade-but-not-seed-grade crop.
        None if the gallery build on this box predates it -- the caller then falls back
        to leaving the track unidentified, i.e. the previous behaviour."""
        with self.lock:
            try:
                return self.g.mint_local(cam, t)
            except AttributeError:
                return None

    def merge(self, source_gid, target_gid):
        """Fold source_gid into target_gid in the live engine gallery."""
        with self.lock:
            try:
                return bool(self.g.merge_gid(int(source_gid), int(target_gid)))
            except Exception:
                return False

    def reinforce(self, gid, app_emb, face_emb, cam, t, gait_emb=None, quality=1.0,
                  face_quality=1.0, gait_quality=1.0):
        """Add a fresh exemplar to an EXISTING gid without matching/minting. The
        two-tier app calls this on a track RE-sync so the track keeps its id (no
        churn) while the gallery entry is enriched for cross-camera matches of
        other tracks. No-op if the gid was aged out. Uses only existing gallery
        internals -- the shared fusion_gallery_app is not modified."""
        g = self.g
        with self.lock:
            e = g.gallery.get(int(gid))
            if e is None:
                return
            try:
                if app_emb is not None and float(quality) >= APP_REINFORCE_MIN_Q:
                    g._store_embedding(e.app_embs, e.app_qualities, app_emb, quality,
                                       cameras=e.app_cameras, camera=cam)
                if face_emb is not None and float(np.linalg.norm(face_emb)) > 1e-6:
                    g._store_embedding(e.face_embs, e.face_qualities, face_emb,
                                       float(face_quality), policy="quality_topk")
                if gait_emb is not None and float(np.linalg.norm(gait_emb)) > 1e-6:
                    g._store_embedding(e.gait_embs, e.gait_qualities, gait_emb,
                                       float(gait_quality), policy="ring")
                e.last_seen_time = g._age_stamp(t)
                e.seen_count += 1
                e.camera_set.add(cam)
                e.camera_last_seen[cam] = t
            except Exception:
                pass

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
                    "thr": round(self.cfg["app_threshold"], 3), "fusion": self.cfg["strategy"],
                    "gallery_policy": self.cfg.get("policy", "ring")}

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
# in-process observation bus: when the PLATF app owns the engine it sets this to an mp
# Queue and workers PUSH each observation dict here (same dict as the disk dump, incl the
# authoritative gid) instead of writing the multi-GB JSONL. Default None -> standalone
# :8083 never touches it (byte-identical). Fork start-method => workers inherit this global.
OBS_Q = None
# live box telemetry: shared {camera -> {frame, wh, boxes:[{bbox, track}]}} written once
# per processed frame straight from the tracker. DELIBERATELY NOT on OBS_Q: that queue is
# bounded and drops on full, so putting high-rate boxes there could evict a real
# observation and cost re-id its embedding evidence. A last-write-wins dict cannot drop an
# observation, cannot apply backpressure, and carries no embeddings -- boxes are geometry
# only. Identity still comes solely from the observation stream. Default None -> standalone
# :8083 never touches it.
BOX_STATE = None
ALERT_TRACKS = None       # shared {camera:track/gid -> video-clock expiry}; PLATF alerts
REID_STAT = None         # shared dict: single-gallery stats (persons/live/match_rate)
REID_STOP = None         # shared Event stopping reid_service + infer servers (set in setup_engine)


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


def _body_crop_quality(crop, bbox, frame_shape, return_parts=False):
    """Cheap, deterministic [0,1] quality for an appearance exemplar.

    This is deliberately computed before gallery retention, not used to suppress
    inference or matching.  A low-quality query can therefore still recover an ID;
    it simply cannot evict a sharper, larger, better-exposed temporal exemplar.
    """
    if crop is None or crop.size == 0:
        return (0.0, {}) if return_parts else 0.0
    fh, fw = frame_shape[:2]
    h, w = crop.shape[:2]
    if h < 2 or w < 2 or fh < 2 or fw < 2:
        return (0.0, {}) if return_parts else 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # Saturating terms make the score comparable across 720p/1080p sources.
    size = min(1.0, (h / float(fh)) / 0.35)
    # Normalize scale before measuring edges: raw Laplacian variance made tiny,
    # compression-noisy crops look sharper merely because they had fewer pixels.
    sharp_gray = cv2.resize(gray, (128, 256), interpolation=cv2.INTER_AREA)
    lap_var = float(cv2.Laplacian(sharp_gray, cv2.CV_32F).var())
    sharp = min(1.0, lap_var / 2500.0)
    mean = float(gray.mean())
    exposure = max(0.0, 1.0 - abs(mean - 127.5) / 127.5)
    aspect = w / float(h)
    aspect_score = max(0.0, 1.0 - abs(aspect - 0.42) / 0.42)
    x1, y1, x2, y2 = (float(v) for v in bbox)
    border = 0.80 if (x1 <= 2 or y1 <= 2 or x2 >= fw - 2 or y2 >= fh - 2) else 1.0
    q = border * (0.40 * size + 0.30 * sharp + 0.15 * exposure + 0.15 * aspect_score)
    q = float(np.clip(q, 0.0, 1.0))
    if return_parts:
        return q, {"size": round(size, 4), "sharp": round(sharp, 4),
                   "exposure": round(exposure, 4), "aspect": round(aspect_score, 4),
                   "border": round(border, 4), "lap_var": round(lap_var, 2),
                   "height_px": int(h), "width_px": int(w)}
    return q


def _body_identity_ok(crop, bbox, frame_shape, quality=None) -> bool:
    """True only for body crops safe enough to seed/reinforce Re-ID identity.

    This is stricter than `_body_crop_quality`: live display may draw partial
    detections, and face/gait may still be useful, but body appearance from a
    head, random lower half, desk-occluded seated person, or over-wide crowd crop
    must not become gallery evidence. Otherwise one bad crop contaminates the
    temporal identity buffer and later makes correct crops match the wrong gid.
    """
    if crop is None or crop.size == 0:
        return False
    h, w = crop.shape[:2]
    if h < BODY_ID_MIN_H:
        return False
    aspect = w / float(max(1, h))
    if aspect < BODY_ID_MIN_ASPECT or aspect > BODY_ID_MAX_ASPECT:
        return False
    x1, y1, x2, y2 = (float(v) for v in bbox)
    fh, fw = frame_shape[:2]
    m = float(BODY_ID_EDGE_MARGIN)
    if x1 <= m or y1 <= m or x2 >= fw - m or y2 >= fh - m:
        return False
    if quality is None:
        quality = _body_crop_quality(crop, bbox, frame_shape)
    return float(quality) >= BODY_ID_MIN_Q


def _body_match_ok(crop, bbox, frame_shape, quality=None) -> bool:
    """True for crops good enough to CONTINUE an identity, not to seed one.

    Deliberately weaker than `_body_identity_ok`: this decides whether a track may hold
    an id and feed LOCAL_REASSOC, never whether a crop may enter the gallery or form a
    cross-camera link. A person walking the far half of the room is perfectly
    recognisable AS THE SAME TRACK long before their crop is clean enough to be
    appearance evidence, and refusing them an id was making them a new person on every
    approach. The frame-edge test is dropped on purpose -- a partially-cropped person is
    still that person for continuity, they just must not become gallery evidence.
    """
    if crop is None or crop.size == 0:
        return False
    if BODY_MATCH_MIN_H <= 0:
        return False                      # tier disabled -> seed gate is the only gate
    h, w = crop.shape[:2]
    if h < BODY_MATCH_MIN_H:
        return False
    aspect = w / float(max(1, h))
    if aspect < BODY_MATCH_MIN_ASPECT or aspect > BODY_MATCH_MAX_ASPECT:
        return False
    if quality is None:
        quality = _body_crop_quality(crop, bbox, frame_shape)
    return float(quality) >= BODY_MATCH_MIN_Q


def _bbox_body_geometry_ok(bbox, frame_shape=None) -> bool:
    """Geometry-only version used before crop/embedding is available."""
    x1, y1, x2, y2 = (float(v) for v in bbox)
    h = max(1.0, y2 - y1)
    w = max(1.0, x2 - x1)
    if h < BODY_ID_MIN_H:
        return False
    aspect = w / h
    if not (BODY_ID_MIN_ASPECT <= aspect <= BODY_ID_MAX_ASPECT):
        return False
    if frame_shape is not None:
        fh, fw = frame_shape[:2]
        m = float(BODY_ID_EDGE_MARGIN)
        if x1 <= m or y1 <= m or x2 >= fw - m or y2 >= fh - m:
            return False
    return True


def _rect_intersects(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _place_label_box(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    text_w: int,
    text_h: int,
    pad: int,
    frame_w: int,
    frame_h: int,
    occupied: list,
):
    """Return a frame-bounded label background rect and text origin."""
    label_w = min(frame_w, int(text_w + 2 * pad))
    label_h = min(frame_h, int(text_h + 2 * pad))
    max_x = max(0, frame_w - label_w)
    max_y = max(0, frame_h - label_h)

    def clamp_rect(px, py):
        lx = max(0, min(max_x, int(px)))
        ly = max(0, min(max_y, int(py)))
        return (lx, ly, lx + label_w, ly + label_h)

    step = max(label_h + 2, 1)
    x_step = max(label_w // 2, 1)
    preferred_x = max(0, min(max_x, x1))
    preferred_y = max(0, min(max_y, y1 - label_h))
    raw = [
        (x1, y1 - label_h), (x1, y2), (x1, y1), (x1, y2 - label_h),
        (x2 - label_w, y1 - label_h), ((x1 + x2 - label_w) // 2, y1 - label_h),
    ]
    for dy in range(-3 * step, 10 * step, step):
        for dx in range(-6 * x_step, 7 * x_step, x_step):
            raw.append((preferred_x + dx, preferred_y + dy))
    raw.sort(key=lambda p: abs(p[0] - preferred_x) + abs(p[1] - preferred_y))
    candidates = [clamp_rect(px, py) for px, py in raw]

    seen = set()
    fallback = candidates[0] if candidates else (0, 0, label_w, label_h)
    for rect in candidates:
        if rect in seen:
            continue
        seen.add(rect)
        if not any(_rect_intersects(rect, used) for used in occupied):
            occupied.append(rect)
            return rect, (rect[0] + pad, rect[3] - pad)
    occupied.append(fallback)
    return fallback, (fallback[0] + pad, fallback[3] - pad)


def _hist_distance(a, b):
    if a is None or b is None:
        return 0.0
    return float(cv2.compareHist(a.reshape(16, 8), b.reshape(16, 8), cv2.HISTCMP_BHATTACHARYYA))


# cross-camera colour gate: reject a cross-cam merge if the torso colour differs
# by more than this Bhattacharyya distance (0=identical, 1=disjoint). "cg45" winner.
COLOUR_GATE_THR = float(os.environ.get("COLOUR_GATE_THR", "0.45"))
COLOUR_GATE_MIN = int(os.environ.get("COLOUR_GATE_MIN", "2"))   # stored hists in the OTHER
#   camera before the gate is trusted. =2 leaves a hole: a cross-cam link forms at
#   first-sync before 2 samples accumulate, so a gross colour-mismatch slips through.
#   Set 1 to gate on the first sample.
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
    # Prefer the platform gallery used by live recognition/enrollment. Keep the
    # legacy FAISS gallery fallback for older deployments.
    plate = NamePlate(os.environ.get("FACE_GALLERY",
                                    os.environ.get("ENROLLED_FACES", "")),
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
    namemap = {}     # gid-level audit metric only; never used to label a live track
    track_names = {} # (sid,lid) -> current-face-supported name state
    track_gid = {}   # (sid, local_id) -> global id: the service's memory of each
    #                  track's assignment, so co-visible tracks become free negatives
    #                  and re-syncs stay stable.
    gid_cam = {}     # gid -> set(cameras) it has been COMMITTED in (cross-cam detect)
    active_lids = {} # sid -> latest co-visible local-id set reported by that worker
    pending = {}     # (sid,local_id) -> {"cand": gid, "hits": n}: cross-cam link held
    faced = set()    # (sid,local_id) already given a one-shot face-upgrade re-match
    #                  for CONFIRM_K consistent matches before it is committed
    gid_since = {}   # (sid,local_id) -> stream time this track ACQUIRED its current gid.
    #                  The sink guard arbitrates with it: when two co-visible tracks claim
    #                  one gid, the older claim is the likelier true owner. Without this
    #                  the survivor was decided by processing order, and a 4400-frame
    #                  owner was observed surrendering its id to a track seconds old.
    gid_denied = {}  # ((sid,local_id), gid) -> stream time the guard took that gid away.
    #                  Re-matching is blocked for GID_DENY_S so a released track cannot
    #                  walk straight back into the same contradiction (observed: a track
    #                  cycling 520 -> 521 -> none -> 520 inside 43 frames).
    name_gid = {}     # enrolled name -> canonical live gid, from STRONG fresh face matches only
    name_anchor = {}  # enrolled name -> clean body/gait prototypes captured ONLY on
    #                  fresh face-confirmed frames. This lets a named person recover
    #                  their id/name with no face later, without querying a contaminated
    #                  whole-gid gallery.
    last_repair = [time.time()]

    def resolve(g):
        seen = 0
        while g in remap and seen < 16:
            g = remap[g]; seen += 1
        return g

    def do_repair():
        # Refresh hard negatives from the latest live set before clustering. This
        # closes the timing hole where first-sync messages did not yet know every
        # co-visible gid, allowing the later repair pass to merge active people.
        for asid, lids in active_lids.items():
            ag = {resolve(track_gid[(asid, lid)]) for lid in lids
                  if (asid, lid) in track_gid}
            for ga in ag:
                for gb in ag:
                    if ga != gb:
                        cannot.add(frozenset((ga, gb)))
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

    def _fold_chists(source, target):
        src = chists.pop(int(source), None)
        if not src:
            return
        dst = chists.setdefault(int(target), {})
        for cam0, vals in src.items():
            cur = dst.setdefault(cam0, [])
            cur.extend(vals)
            del cur[:-COLOUR_GATE_MAX]

    def _rewrite_held_gids():
        for st_s in st.values():
            for k, v in list(st_s["tgid"].items()):
                nv = resolve(v)
                if nv != v:
                    st_s["tgid"][k] = nv
        for k, v in list(track_gid.items()):
            nv = resolve(v)
            if nv != v:
                track_gid[k] = nv
        for nm, g in list(name_gid.items()):
            name_gid[nm] = resolve(g)

    def _co_visible_in_same_stream(key, other_gid):
        sid0, lid0 = key
        for olid in active_lids.get(sid0, set()):
            if int(olid) == int(lid0):
                continue
            og = track_gid.get((sid0, int(olid)))
            if og is not None and resolve(og) == int(other_gid):
                return True
        return False

    def _canonicalize_named_gid(name, dist, gid, key, cam):
        """Use a STRONG enrolled-face match as an identity anchor.

        This is intentionally stricter than the green display label:
        - only fresh face evidence calls this;
        - weak-but-accepted labels do not merge ids;
        - same-camera co-visible ids are never folded together.
        """
        if not name or name == "Unknown" or gid is None:
            return gid
        if float(dist) > NAME_CANON_MERGE_THR:
            return gid
        gid = resolve(int(gid))
        live = gallery.live_gids()
        canon = name_gid.get(str(name))
        canon = resolve(canon) if canon is not None else None
        if canon not in live:
            name_gid[str(name)] = gid
            return gid
        if canon == gid:
            return gid
        # Preserve the enrolled name's established canonical gid. Numeric min-id
        # merging makes the displayed ID drift when a later fragment happens to have
        # a smaller P-number; for a named person the first face-confirmed anchor is
        # the stable identity until a hard conflict rejects it.
        target, source = int(canon), int(gid)
        if frozenset((int(source), int(target))) in cannot:
            return gid
        if _co_visible_in_same_stream(key, source) or _co_visible_in_same_stream(key, target):
            cannot.add(frozenset((int(source), int(target))))
            return gid
        if gallery.merge(source, target):
            remap[int(source)] = int(target)
            _fold_chists(source, target)
            gid_cam.setdefault(int(target), set()).update(gid_cam.pop(int(source), set()))
            gid_cam.setdefault(int(target), set()).add(cam)
            name_gid[str(name)] = int(target)
            _rewrite_held_gids()
            print(f"[names] canonicalized {name}: P{source}->P{target} "
                  f"(dist={float(dist):.3f})", flush=True)
            return resolve(gid)
        return gid

    def _anchor_add(name, sm, ge):
        if not name or name == "Unknown" or sm is None:
            return
        try:
            a = np.asarray(sm, np.float32).reshape(-1)
            if float(np.linalg.norm(a)) <= 1e-6:
                return
            n = str(name)
            d = name_anchor.setdefault(n, {"app": [], "gait": []})
            d["app"].append(a.copy())
            del d["app"][:-10]
            if ge is not None:
                g = np.asarray(ge, np.float32).reshape(-1)
                if float(np.linalg.norm(g)) > 1e-6:
                    d["gait"].append(g.copy())
                    del d["gait"][:-5]
        except Exception:
            return

    def _anchor_dist(rows, q):
        if not rows or q is None:
            return None
        try:
            qq = np.asarray(q, np.float32).reshape(-1)
            qn = float(np.linalg.norm(qq))
            if qn <= 1e-6:
                return None
            best = None
            for r in rows:
                rr = np.asarray(r, np.float32).reshape(-1)
                rn = float(np.linalg.norm(rr))
                if rn <= 1e-6 or rr.shape != qq.shape:
                    continue
                d = 1.0 - float(np.dot(rr, qq) / (rn * qn + 1e-9))
                best = d if best is None or d < best else best
            return best
        except Exception:
            return None

    def _merge_to_named_anchor(name, gid, target_gid, key, cam, reason, app_d=None, gait_d=None):
        """Fold gid into the already face-confirmed canonical gid for `name`.

        This is stricter than generic Re-ID: it only runs for names that face has
        already anchored in `name_gid`, and it refuses co-visible impossibilities.
        """
        if not name or gid is None or target_gid is None:
            return gid
        gid = resolve(int(gid))
        target_gid = resolve(int(target_gid))
        live = gallery.live_gids()
        if target_gid not in live:
            name_gid[str(name)] = gid
            return gid
        if gid == target_gid:
            return gid
        if frozenset((int(gid), int(target_gid))) in cannot:
            return gid
        if _co_visible_in_same_stream(key, gid) or _co_visible_in_same_stream(key, target_gid):
            cannot.add(frozenset((int(gid), int(target_gid))))
            return gid
        if gallery.merge(gid, target_gid):
            remap[int(gid)] = int(target_gid)
            _fold_chists(gid, target_gid)
            gid_cam.setdefault(int(target_gid), set()).update(gid_cam.pop(int(gid), set()))
            gid_cam.setdefault(int(target_gid), set()).add(cam)
            name_gid[str(name)] = int(target_gid)
            _rewrite_held_gids()
            msg = (f"[names] anchor {name}: P{gid}->P{target_gid} via {reason} "
                   f"(app={app_d if app_d is not None else 'na'} "
                   f"gait={gait_d if gait_d is not None else 'na'})")
            print(msg, flush=True)
            if NAME_ANCHOR_LOG:
                try:
                    with open(NAME_ANCHOR_LOG, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"t": round(time.time(), 3), "name": str(name),
                                            "source": int(gid), "target": int(target_gid),
                                            "camera": str(cam), "reason": str(reason),
                                            "app_d": app_d, "gait_d": gait_d}) + "\n")
                except Exception:
                    pass
            return resolve(gid)
        return gid

    def _match_named_anchor(sm, ge, key, cam, gid):
        """Best face-confirmed name whose body/gait evidence agrees with this track.

        Body alone is not enough in this office footage: similar clothing and
        uniform-like views caused false Kiran/chaitra propagation. A different gid
        may inherit a name only when body AND gait both agree. Critically, the
        comparison uses clean name anchors captured on fresh face-confirmed frames,
        not the whole gid gallery, because gids can contain contaminated crops.
        Same-gid name restoration is handled separately below.
        """
        # Either frac at 0 disables body/gait name propagation outright. Relying on the
        # thresholds falling to 0 would leave a hairline case open (a distance of exactly
        # 0.0 still satisfies `<= 0`), and this must be unambiguously off: it is what put
        # one person's name on another's track.
        if sm is None or not name_gid:
            return None
        if NAME_ANCHOR_APP_FRAC <= 0 or NAME_ANCHOR_GAIT_FRAC <= 0:
            return None
        app_thr = float(gallery.cfg.get("app_threshold", 0.145)) * NAME_ANCHOR_APP_FRAC
        gait_thr = float(gallery.cfg.get("gait_threshold", 0.3496)) * NAME_ANCHOR_GAIT_FRAC
        best = None
        for name, canon0 in list(name_gid.items()):
            canon = resolve(canon0)
            if canon is None or canon == gid:
                continue
            if _co_visible_in_same_stream(key, canon):
                continue
            clean = name_anchor.get(str(name)) or {}
            app_d = _anchor_dist(clean.get("app"), sm)
            gait_d = _anchor_dist(clean.get("gait"), ge)
            if app_d is None:
                continue
            body_ok = app_d <= app_thr
            gait_ok = gait_d is not None and gait_d <= gait_thr
            if not (body_ok and gait_ok):
                continue
            score = app_d / max(app_thr, 1e-9)
            score += gait_d / max(gait_thr, 1e-9)
            reason = "app+gait"
            cand = (score, str(name), int(canon), round(float(app_d), 4),
                    round(float(gait_d), 4) if gait_d is not None else None, reason)
            if best is None or cand < best:
                best = cand
        if best is None:
            return None
        _, name, canon, app_d, gait_d, reason = best
        return name, canon, app_d, gait_d, reason

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
            resp.pop(msg["sid"], None); st.pop(msg["sid"], None); active_lids.pop(msg["sid"], None)
            continue
        if ty == "thr":
            try:
                gallery.set_threshold(float(msg["thr"]))
                chists.clear()   # gallery rebuilt -> colour history is stale
            except Exception:
                pass
            continue
        if ty == "claim":
            # the worker locally re-associated a re-acquired track to an existing gid
            # (IoU inheritance). Record it so this track's next sync REINFORCES that gid
            # instead of re-matching to a different one (which would churn the label).
            g = resolve(msg["gid"])
            if g is not None:
                track_gid[(msg["sid"], msg["lid"])] = g
                seen.add(g)
            continue
        if ty == "unclaim":
            # A worker detected the hard impossibility "one gid on two live boxes"
            # after an asynchronous repair/remap. Forget the rejected track so its
            # immediately re-sent summary is matched again with the co-visible gid
            # excluded instead of reinforcing the bad merged identity.
            key = (msg["sid"], msg["lid"])
            track_gid.pop(key, None)
            pending.pop(key, None)
            faced.discard(key)
            ss = st.get(msg["sid"])
            if ss is not None:
                ss["tgid"].pop(msg["lid"], None)
                ss["tpend"].pop(msg["lid"], None)
            continue
        if ty == "sum":
            # TWO-TIER: one matured/re-syncing track's best prototype -> global id.
            # Called ~once per track per SYNC_EVERY_S, not per frame. Same match
            # logic as :8082; the worker never blocks on the reply.
            sid, cam, t, lid = msg["sid"], msg["cam"], msg["ts"], msg["lid"]
            key = (sid, lid)
            active_lids[sid] = {int(lid), *(int(x) for x in (msg.get("covis") or []))}
            gid = None
            try:
                sm = _deser(msg.get("a"))
                quality = float(msg.get("q", 1.0))
                body_ok = bool(msg.get("body_ok", True))
                # Seed-grade implies match-grade; older workers that don't send the field
                # fall back to the seed gate, i.e. the previous single-tier behaviour.
                match_ok = bool(msg.get("match_ok", body_ok)) or body_ok
                face_quality = float(msg.get("fq", 1.0))
                gait_quality = float(msg.get("gq", 1.0))
                fe, ge = _deser(msg.get("f")), _deser(msg.get("g"))
                face_now = _deser(msg.get("fn"))
                face_checked = bool(msg.get("face_checked"))
                hist = _deser(msg.get("c"))
                store_app = sm if (body_ok and float(quality) >= APP_REINFORCE_MIN_Q) else None
                if sm is not None:
                    prev = resolve(track_gid[key]) if key in track_gid else None
                    if prev is not None and REASSOC_VERIFY and body_ok and sm is not None:
                        # verify the position-inherited (LOCAL_REASSOC) bond is still the
                        # SAME person. A large appearance gap => the track swapped onto a
                        # different person who stepped into the same spot -> break the bond
                        # so this sync RE-MATCHES (mints a correct id) instead of gluing a
                        # different person onto the inherited gid.
                        if gallery.appearance_dist(prev, sm) > REASSOC_VERIFY_DIST:
                            track_gid.pop(key, None)
                            prev = None
                            faced.discard(key)
                    if prev is not None and GID_UNIQUE_STRICT:
                        # SINK GUARD. A gid already held by a co-visible track in this
                        # camera cannot also be this track: one person is not two boxes
                        # in one frame. The only check for this used to sit AFTER the
                        # gallery had already been reinforced, and was itself gated on
                        # body_ok -- so with ~93% of crops failing that gate, one gid
                        # could absorb a whole camera unchallenged, and a gid that is
                        # "present" all the time then blocks every later merge through
                        # the mapper's co-visibility rule. Break the bond BEFORE
                        # reinforcing, exactly as REASSOC_VERIFY does above, so this
                        # sync re-matches through the first-sync path -- which already
                        # excludes the co-visible gids (SAME_FRAME_GUARD) and already
                        # refuses to seed from a crop that fails body_ok.
                        for _olid in (msg.get("covis") or []):
                            if int(_olid) == int(lid):
                                continue
                            _okey = (sid, int(_olid))
                            _og = track_gid.get(_okey)
                            if _og is not None and resolve(_og) == prev:
                                # ARBITRATE, do not just yield. Whoever holds the OLDER
                                # claim on this gid is the likelier true owner; the newer
                                # claim is the intruder and is the one released. Yielding
                                # unconditionally let processing order decide, which was
                                # observed evicting a 4400-frame owner in favour of a
                                # track seconds old, and evicting a body_ok crop (q=0.80)
                                # in favour of one that failed the gate.
                                _mine = gid_since.get(key, t)
                                _theirs = gid_since.get(_okey, t)
                                _i_lose = _mine > _theirs
                                if os.environ.get('SINK_GUARD_LOG'):
                                    try:
                                        with open(os.environ['SINK_GUARD_LOG'], 'a') as _sf:
                                            _sf.write(json.dumps({
                                                'wall': round(time.time(), 2), 'cam': str(cam),
                                                'lid': int(lid), 'held_by_lid': int(_olid),
                                                'gid': int(prev), 'body_ok': bool(body_ok),
                                                'q': round(float(quality), 3),
                                                'held_s': round(float(t - _mine), 2),
                                                'other_held_s': round(float(t - _theirs), 2),
                                                'evicted': 'self' if _i_lose else 'other'}) + '\n')
                                    except Exception:
                                        pass
                                if _i_lose:
                                    track_gid.pop(key, None)
                                    gid_since.pop(key, None)
                                    gid_denied[(key, prev)] = t
                                    prev = None
                                    faced.discard(key)
                                    break
                                # My claim is older: keep it and evict the intruder, then
                                # keep scanning in case more than one track grabbed it.
                                track_gid.pop(_okey, None)
                                gid_since.pop(_okey, None)
                                gid_denied[(_okey, prev)] = t
                                faced.discard(_okey)
                    if prev is not None:
                        # RE-SYNC: a track keeps its id for life. Do NOT re-match (that
                        # churns ids when a sparse embedding misses its own gid at the
                        # tight threshold). Just REINFORCE the gallery with the fresh
                        # exemplar so cross-camera matches of OTHER tracks improve.
                        gid = prev
                        # PROMOTION. The "keep your id for life" rule above assumes the id
                        # was earned from real evidence. A mint_local id was not -- it is
                        # empty, so it can never be matched INTO, and a person who leaves
                        # and returns would collect a brand new one every time. That trades
                        # a fast id for worse fragmentation, which is not the trade wanted.
                        # So the FIRST seed-grade crop for a local id re-matches once: if
                        # it lands on a real identity, the local id is remapped onto it and
                        # history re-points through canonical_gid, recovering the person's
                        # earlier sightings. If it lands nowhere, reinforce below promotes
                        # the local id in place and it becomes an ordinary identity.
                        if body_ok and gallery.is_local(prev):
                            ex_p = set(colour_exclude(cam, hist) or [])
                            for _olid in (msg.get("covis") or []):
                                _og = track_gid.get((sid, _olid))
                                if _og is not None:
                                    ex_p.add(resolve(_og))
                            ex_p.add(prev)          # don't match the empty id to itself
                            m_p, _dp = gallery.match(sm, fe, cam, t, ge,
                                                     exclude_gids=ex_p or None, quality=quality,
                                                     face_quality=face_quality,
                                                     gait_quality=gait_quality)
                            m_p = resolve(m_p)
                            # Only adopt an id that ALREADY has evidence. A match that
                            # itself minted a fresh empty gid is no better than what we
                            # hold, and adopting it would just churn the id for nothing.
                            if (m_p is not None and m_p != prev
                                    and not gallery.is_local(m_p)
                                    and m_p not in ex_p):
                                _local_was = int(prev)
                                remap[prev] = m_p
                                gid = prev = m_p
                                track_gid[key] = m_p
                                gid_since.setdefault(key, t)
                                if os.environ.get('SINK_GUARD_LOG'):
                                    try:
                                        with open(os.environ['SINK_GUARD_LOG'], 'a') as _sf:
                                            _sf.write(json.dumps({
                                                'wall': round(time.time(), 2), 'cam': str(cam),
                                                'lid': int(lid), 'event': 'promote',
                                                'local_gid': _local_was, 'into_gid': int(m_p),
                                                'dist': round(float(_dp), 4)}) + '\n')
                                    except Exception:
                                        pass
                        # ...unless this track is HOLDING a cross-cam candidate: re-match
                        # to check it still points at the same gid; commit only after
                        # CONFIRM_K agreements (repeated-agreement guard vs 1-lucky-match).
                        if body_ok and CROSS_CONFIRM and key in pending:
                            m2, _ = gallery.match(sm, fe, cam, t, ge, quality=quality,
                                                  face_quality=face_quality,
                                                  gait_quality=gait_quality,
                                                  exclude_gids=set(colour_exclude(cam, hist) or []) or None)
                            m2 = resolve(m2)
                            if m2 is not None and m2 == resolve(pending[key]["cand"]):
                                pending[key]["hits"] += 1
                                if pending[key]["hits"] >= CONFIRM_K:
                                    remap[prev] = m2; gid = m2   # COMMIT the cross-cam link
                                    pending.pop(key, None)
                            else:
                                pending[key]["hits"] -= 1
                                if pending[key]["hits"] <= 0:
                                    pending.pop(key, None)        # candidate never confirmed
                        _fok = fe is not None and float(np.linalg.norm(np.asarray(fe, np.float32))) > 1e-6
                        if body_ok and FACE_UPGRADE and _fok and key not in faced:
                            faced.add(key)
                            fu, _ = gallery.match(sm, fe, cam, t, ge, quality=quality,
                                                  face_quality=face_quality,
                                                  gait_quality=gait_quality)
                            fu = resolve(fu)
                            if fu is not None and fu != gid:
                                cams_fu = gid_cam.get(fu)
                                if cams_fu and cam not in cams_fu:
                                    if CROSS_CONFIRM:
                                        # A one-shot face upgrade is too weak for live
                                        # cross-camera remap on small CCTV faces. Hold
                                        # the candidate and let the normal pending path
                                        # commit only after CONFIRM_K repeated matches.
                                        pending[key] = {"cand": fu, "hits": 1}
                                    else:
                                        remap[gid] = fu
                                        gid = fu
                        if body_ok:
                            gallery.reinforce(gid, sm, fe, cam, t, ge, quality=quality,
                                              face_quality=face_quality,
                                              gait_quality=gait_quality)
                        else:
                            # Keep the live id warm, and allow valid face/gait evidence
                            # to attach only when it agrees with existing evidence.  A
                            # BODY-failed frame cannot prove the tracker is still on the
                            # same person; if it blindly writes a new face/gait vector, a
                            # tracker swap pollutes the gid and every later Re-ID/search
                            # decision sees mixed exemplars.
                            gallery.touch(gid, cam, t)
                            app_agrees = False
                            if match_ok and sm is not None:
                                try:
                                    app_agrees = gallery.appearance_dist(gid, sm) <= NONBODY_REINFORCE_APP_DIST
                                except Exception:
                                    app_agrees = False
                            safe_fe = None
                            if fe is not None and float(np.linalg.norm(np.asarray(fe, np.float32))) > 1e-6:
                                fd = gallery.face_dist(gid, fe)
                                face_agrees = (fd is not None and
                                               fd <= float(gallery.cfg.get("face_threshold", 0.45)) *
                                               NONBODY_REINFORCE_FACE_FRAC)
                                if app_agrees or face_agrees:
                                    safe_fe = fe
                            safe_ge = None
                            if ge is not None and float(np.linalg.norm(np.asarray(ge, np.float32))) > 1e-6:
                                gd = gallery.gait_dist(gid, ge)
                                gait_agrees = (gd is not None and
                                               gd <= float(gallery.cfg.get("gait_threshold", 0.3496)) *
                                               NONBODY_REINFORCE_GAIT_FRAC)
                                if app_agrees or gait_agrees:
                                    safe_ge = ge
                            if safe_fe is not None or safe_ge is not None:
                                gallery.reinforce(gid, None, safe_fe, cam, t, safe_ge,
                                                  quality=quality,
                                                  face_quality=face_quality,
                                                  gait_quality=gait_quality)
                    else:
                        if not body_ok:
                            # A new fragment with only a partial/over-wide/random body crop
                            # must not seed a fresh appearance identity or cross-camera link.
                            # It will be tried again on the next good body evidence frame.
                            gid = None
                            if match_ok:
                                # ...but it may hold a LOCAL-ONLY id: minted empty, so it
                                # carries no appearance evidence, cannot be matched into,
                                # and cannot form a cross-camera link. The crop is still
                                # kept out of the gallery -- the only thing that changes is
                                # that the person stops being anonymous while they are far
                                # from the camera. On its own this id is a DEAD END: nothing
                                # can match into an empty entry, so a person who leaves and
                                # returns would collect a new one each time. The PROMOTION
                                # step in the re-sync path is what folds it back into their
                                # real identity once a seed-grade crop arrives; without that
                                # step this tier trades fast ids for worse fragmentation.
                                ex_local = set()
                                for olid in (msg.get("covis") or []):
                                    og = track_gid.get((sid, olid))
                                    if og is not None:
                                        ex_local.add(resolve(og))
                                gid = gallery.mint_local(cam, t)
                                if gid is not None and gid in ex_local:
                                    gid = None      # never duplicate a co-visible id
                        else:
                            # FIRST sync: match into the shared gallery (cross-camera link)
                            ex = set(colour_exclude(cam, hist) or [])
                            # LIVE same-frame guard: a track co-visible in THIS camera is a
                            # DIFFERENT person (one person can't be two boxes in one frame),
                            # so its gid must not be returned for this track. Two-tier had
                            # dropped this (only fed the periodic repair); the honest metric
                            # showed ~24% of ids provably merge co-visible people -> restore
                            # it as a HARD exclude at match time.
                            if SAME_FRAME_GUARD:
                                for olid in (msg.get("covis") or []):
                                    og = track_gid.get((sid, olid))
                                    if og is not None:
                                        ex.add(resolve(og))
                            # COOLDOWN: a gid the guard just took off this track stays
                            # excluded for GID_DENY_S. SAME_FRAME_GUARD only excludes ids
                            # CURRENTLY held, so once the rival was released too the
                            # contested gid became matchable again and the track walked
                            # back into the same contradiction.
                            if gid_denied:
                                for (_dkey, _dgid), _dt in list(gid_denied.items()):
                                    if t - _dt > GID_DENY_S:
                                        gid_denied.pop((_dkey, _dgid), None)
                                    elif _dkey == key:
                                        ex.add(resolve(_dgid))
                            gid, _dist = gallery.match(sm, fe, cam, t, ge,
                                                       exclude_gids=ex or None, quality=quality,
                                                       face_quality=face_quality,
                                                       gait_quality=gait_quality)
                            gid = resolve(gid)
                            # persistent rejoin: a freshly minted gid may be a returning person
                            if store is not None and gid is not None and gid not in seen:
                                rj, rd, mod = store.rejoin(fe, store_app, ge)
                                if rj is not None and rj != gid:
                                    remap[gid] = rj; gid = rj
                            # MULTI-FRAME CROSS-CAM CONFIRM: if the match links to a gid that
                            # lives in ANOTHER camera, don't commit on this single match --
                            # take an OWN-camera id instead and hold the link pending until
                            # CONFIRM_K agreements (see re-sync path above).
                            if CROSS_CONFIRM and gid is not None:
                                cams_of = gid_cam.get(gid)
                                if cams_of and cam not in cams_of:
                                    own, _ = gallery.match(sm, fe, cam, t, ge, quality=quality,
                                                           face_quality=face_quality,
                                                           gait_quality=gait_quality,
                                                           exclude_gids=(ex | {gid}) or None)
                                    own = resolve(own)
                                    if own is not None and own != gid:
                                        pending[key] = {"cand": gid, "hits": 1}
                                        gid = own
                    if gid is not None:
                        # Final same-camera uniqueness gate. A repair can remap two
                        # previously distinct active gids after their first-sync guard,
                        # so re-check against every co-visible assignment immediately
                        # before committing this reply. Excluding all of them forces a
                        # different match (or a fresh gid) for this physical person.
                        active_ex = set()
                        for olid in (msg.get("covis") or []):
                            og = track_gid.get((sid, olid))
                            if og is not None:
                                active_ex.add(resolve(og))
                        if gid in active_ex and (GID_UNIQUE_STRICT or body_ok):
                            track_gid.pop(key, None)
                            if body_ok:
                                gid, _dist = gallery.match(
                                    sm, fe, cam, t, ge,
                                    exclude_gids=(active_ex | {gid}),
                                    quality=quality,
                                    face_quality=face_quality,
                                    gait_quality=gait_quality)
                                gid = resolve(gid)
                            else:
                                # A crop too weak to seed identity may still REFUSE a
                                # contradiction, but it must not choose the replacement:
                                # re-matching on evidence the gates already distrust is
                                # how the wrong id gets picked in the first place. Leave
                                # the track unassigned; the first-sync path re-acquires
                                # it (with the same co-visible excludes) as soon as a
                                # good crop arrives.
                                gid = None
                    # The uniqueness gate above can legitimately end with no id, so the
                    # commit is re-guarded rather than folded into the branch above.
                    if gid is not None:
                        seen.add(gid)
                        # Stamp the acquisition time only when the assignment actually
                        # CHANGES, so a track that keeps its id keeps its seniority and
                        # the sink guard can tell a settled owner from a fresh claimant.
                        if track_gid.get(key) != gid:
                            gid_since[key] = t
                        track_gid[key] = gid
                        gid_cam.setdefault(gid, set()).add(cam)
                        remember_colour(gid, cam, hist)
                        if store is not None:
                            # Persistent rejoin must never store a cached/stale face.
                            # A tracker swap can keep the same local id while pixels
                            # change person; writing `fe` here carried the old face
                            # into the new gid and later resurrected false names.
                            store.observe(gid, face_now, store_app, ge, t)
                        # co-visible tracks in THIS camera are different people ->
                        # free negatives for the repair pass (replaces the per-frame
                        # same-frame guard, which two-tier no longer has).
                        for olid in msg.get("covis") or []:
                            og = track_gid.get((sid, olid))
                            if og is not None and og != gid:
                                cannot.add(frozenset((gid, og)))
            except Exception as e:
                print(f"[reid] summary failed: {e}", flush=True)
            # Recognition is LOCAL-TRACK metadata. Never inherit a name from the
            # global gid: a false merge would otherwise put one person's name on
            # every track accumulated under that gid. Only fresh face evidence from
            # this sync may establish/change/contradict a name.
            name_clear = False
            if plate.enabled and face_checked:
                ns = track_names.setdefault(key, {"name": None, "misses": 0,
                                                   "last_match": 0.0, "dist": 999.0,
                                                   "app": None})
                # A tracker can swap from one nearby person to another while keeping
                # the same local id. During that gap there may be no usable face, so
                # face-miss hysteresis alone retains the previous person's name. Body
                # appearance is not allowed to assign a name, but it can safely revoke
                # one when it strongly contradicts the body seen at the face match.
                if ns.get("name") and ns.get("app") is not None and body_ok and sm is not None:
                    a0 = np.asarray(ns["app"], np.float32).reshape(-1)
                    a1 = np.asarray(sm, np.float32).reshape(-1)
                    denom = float(np.linalg.norm(a0) * np.linalg.norm(a1))
                    app_dist = 1.0 - (float(a0 @ a1) / denom if denom > 1e-12 else 0.0)
                    if app_dist > NAME_APP_CLEAR_DIST:
                        ns.update(name=None, misses=0, app=None)
                        name_clear = True
                valid_now = (face_now is not None and
                             float(np.linalg.norm(np.asarray(face_now, np.float32))) > 1e-6)
                matched_fresh_name = False
                if valid_now:
                    fresh_name, fresh_dist = plate.vote([face_now])
                    if fresh_name != "Unknown":
                        matched_fresh_name = True
                        # A confident different identity is direct contradictory
                        # evidence, so switch immediately rather than retaining stale.
                        ns.update(name=fresh_name, misses=0, last_match=time.time(),
                                  dist=float(fresh_dist),
                                  app=(np.asarray(sm, np.float32).copy()
                                       if body_ok and sm is not None else None))
                        if body_ok:
                            _anchor_add(fresh_name, sm, ge)
                        gid = _canonicalize_named_gid(fresh_name, fresh_dist, gid, key, cam)
                        if gid is not None:
                            track_gid[key] = gid
                    else:
                        # Unknown/low-quality face is absence of evidence, not evidence
                        # of a different enrolled person. Keep the last confirmed name
                        # while tracking/body appearance still agrees; the appearance
                        # contradiction guard above or a fresh different face match will
                        # clear/switch it.
                        ns["misses"] += 1
                if not matched_fresh_name and gid is not None and body_ok:
                    anchor = _match_named_anchor(sm, ge, key, cam, gid)
                    if anchor is not None:
                        aname, agid, app_d, gait_d, reason = anchor
                        gid = _merge_to_named_anchor(aname, gid, agid, key, cam,
                                                     reason, app_d=app_d, gait_d=gait_d)
                        if gid is not None:
                            track_gid[key] = gid
                            ns.update(name=aname, misses=0, last_match=time.time(),
                                      app=(np.asarray(sm, np.float32).copy()
                                           if body_ok and sm is not None else ns.get("app")))
                    else:
                        rgid = resolve(gid)
                        for aname, agid in list(name_gid.items()):
                            if resolve(agid) == rgid:
                                ns.update(name=str(aname), misses=0, last_match=time.time(),
                                          app=(np.asarray(sm, np.float32).copy()
                                               if body_ok and sm is not None else ns.get("app")))
                                break
            q = resp.get(sid)
            if q is not None:
                try:
                    ns = track_names.get(key) or {}
                    q.put({"lid": lid, "gid": gid,
                           "name": ns.get("name"), "name_clear": name_clear})
                except Exception:
                    pass
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
        from MTMC.adapters import make_tracker, crop_boxes
        # No models are compiled here: this process only decodes, tracks and draws.
        # Inference goes to the shared device servers (one model copy for the box).
        ftx = infer_pool.FrameTx(FRAME_SHM_BYTES)
        det = infer_pool.DetClient(sid, pool_q["det"], pool_resp["det"], ftx)
        tracker = make_tracker(TRACKER, max_age_frames=TRACK_MAX_AGE, iou_threshold=TRACK_IOU)
        ev_w = ev_f = None
        evface_w = evface_f = None
        obs_f = None
        obs_crop_seen: dict = {}   # (stable_id, camera) -> last frame a crop was saved
        face_logged = set()
        if OBS_DUMP_DIR:
            try:
                os.makedirs(os.path.join(OBS_DUMP_DIR, "crops"), exist_ok=True)
                # only open the JSONL for standalone disk capture; when the PLATF app owns
                # the engine (OBS_Q set) obs go in-process and OBS_DUMP_DIR is crops-only.
                if OBS_Q is None:
                    obs_f = open(os.path.join(OBS_DUMP_DIR, f"obs_{sid}.jsonl"), "w",
                                 encoding="utf-8")
            except Exception:
                obs_f = None
        if EVENT_LOG_DIR:
            import csv as _csv
            os.makedirs(EVENT_LOG_DIR, exist_ok=True)
            ev_f = open(os.path.join(EVENT_LOG_DIR, f"ev_{sid}.csv"), "w", newline="")
            ev_w = _csv.writer(ev_f)
            ev_w.writerow(["camera", "frame", "track_id", "global_id", "label", "x1", "y1", "x2", "y2"])
            # per-(gid,camera) face embeddings for the CROSS-CAM face oracle in
            # honest_metric.py: face is an INDEPENDENT modality (not the appearance
            # emb that does the linking), so cross-cam face agreement is a non-circular
            # check of whether a multi-cam merge is real. Throttled ~1/gid/30 frames.
            evface_f = open(os.path.join(EVENT_LOG_DIR, f"face_{sid}.csv"), "w", newline="")
            evface_w = _csv.writer(evface_f)
            evface_w.writerow(["camera", "gid", "face"])
        emb = infer_pool.EmbedClient(sid, pool_q["embed"], pool_resp["embed"])
        face = gait = None
        if do_face:
            face = infer_pool.FaceClient(sid, pool_q["face"], pool_resp["face"])
        if do_gait:
            gait = infer_pool.GaitClient(sid, pool_q["gait"], pool_resp["gait"], ftx)
        # cross-cam re-id lives in the central service; register this stream's return channel
        req_q.put({"t": "reg", "sid": sid, "resp": resp_q})

        fcache, fqcache, gcache = {}, {}, {}
        fmcache = {}          # lid -> face detection quality (det/w/h/sharp/q)
        appbuf = {}           # lid -> top-K body evidence over this local track
        facebuf = {}          # lid -> top-K face evidence over this local track
        gids_seen, sent = set(), set()
        pos_hist = {}   # local_id -> recent bbox-center list (motion gate for gait)
        hits = {}       # local_id -> processed frames survived (maturation gate)
        last_gid = {}   # local_id -> last assigned gid (label persists when a track is capped out of embedding)
        last_name = {}  # local_id -> enrolled name supported by this track's own face
        last_name_at = {}  # local_id -> monotonic time of the last positive face match
        # --- two-tier local state (replaces the per-frame global round-trip) ---
        local2global = {}   # local_id -> global id  (the tracker keeps the box; this
        #                     map is refreshed asynchronously by the global service)
        best = {}           # local_id -> {"app":emb,"face":(q,emb),"gait":emb} best prototype
        last_sync = {}      # local_id -> video time of last global sync
        lost_mem = {}       # local_id -> (bbox, gid, frame_lost): re-association memory
        prev_state = {}     # local_id -> (bbox, gid) from the previous frame
        draw_bbox = {}      # local_id -> smoothed bbox for DISPLAY ONLY; crops/re-id use raw tracker bbox
        stable_id = {}      # local_id -> STABLE person identity (survives tracker breaks);
        #                     the gait silhouette buffer is keyed by this so it does not
        #                     reset when the tracker re-acquires a walker as a new track
        reid_hits = reid_q = frames = seq = 0
        fps = dpf = tdec = tdet = ttk = tem = tfa = tga = 0.0
        # flow profiling: per-stage queue-WAIT vs COMPUTE (EMA, ms) + call counts, so
        # the delay a stage adds can be split into "sat in the queue" vs "device work".
        dw = dcp = ew = ecp = fw = fcp = gw = gcp = 0.0
        n_det = n_emb = n_fa = n_ga = 0
        d_shm = d_pre = d_inf = d_post = 0.0   # inside detect(): shm read / pre / infer / post (ms)
        n_sum = n_facev = n_gaitv = 0   # summaries sent, and how many carried a REAL face/gait vec
        a = 0.3
        fe_every, ga_every = max(1, FACE_EVERY), max(1, GAIT_EVERY)

        def _buf_mean(buf, lid):
            rows = buf.get(int(lid)) or []
            if not rows:
                return None
            mat = np.stack([e for _q, e in rows]).astype(np.float32)
            v = mat.mean(axis=0)
            n = float(np.linalg.norm(v))
            return (v / n).astype(np.float32) if n > 1e-6 else None

        def _buf_put(buf, lid, emb, q, cap=5):
            if emb is None:
                return None
            arr = np.asarray(emb, np.float32).reshape(-1)
            if float(np.linalg.norm(arr)) <= 1e-6:
                return None
            rows = buf.setdefault(int(lid), [])
            rows.append((float(q), arr.copy()))
            rows.sort(key=lambda x: x[0], reverse=True)
            del rows[int(cap):]
            return _buf_mean(buf, lid)

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
            det_scores = list(getattr(det, "last_confidences", []) or [])
            dw = (1 - a) * dw + a * det.last_wait; dcp = (1 - a) * dcp + a * det.last_compute; n_det += 1
            _ds = det.last_sub
            if _ds:
                d_shm = (1 - a) * d_shm + a * _ds.get("shm", 0); d_pre = (1 - a) * d_pre + a * _ds.get("pre", 0)
                d_inf = (1 - a) * d_inf + a * _ds.get("infer", 0); d_post = (1 - a) * d_post + a * _ds.get("post", 0)
            t1 = time.perf_counter()
            try:
                tracks = tracker.update(boxes, frames, scores=det_scores)
            except TypeError:
                try:
                    tracks = tracker.update(boxes, frames, frame=frame)
                except TypeError:
                    tracks = tracker.update(boxes, frames)
            ttr = (time.perf_counter() - t1) * 1000
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

            # Live box telemetry. The observation stream below only fires when a track is
            # SYNC_EVERY_S-due, so using it for the overlay showed one box per camera while
            # a dozen people were tracked. The tracker already produced every box on this
            # frame; publish them so the overlay draws at frame rate. Write-only, no
            # embeddings, and it never feeds re-id -- PLATF joins gids on `track`.
            if BOX_STATE is not None:
                try:
                    BOX_STATE[str(camera)] = {
                        "frame": int(frames),
                        "wh": [int(frame.shape[1]), int(frame.shape[0])],
                        "boxes": [{"bbox": [round(float(c), 1) for c in tr.bbox],
                                   "track": int(tr.local_id)}
                                  for tr in tracks
                                  if hits.get(int(tr.local_id), 0) >= TRACK_MIN_HITS],
                    }
                except Exception:
                    pass

            def _moving(lid):
                h = pos_hist.get(int(lid))
                if not h or len(h) < 3:
                    return True   # new/short track -> allow (capture gait while they walk in)
                xs = [p[0] for p in h]; ys = [p[1] for p in h]
                return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5 > GAIT_MOTION_PX

            crops = crop_boxes(frame, [tr.bbox for tr in tracks])
            t_sec = (clip_t0 + getattr(dec, "n", 0) / src_fps) if use_vclock                 else (time.perf_counter() - wall0)

            def _sync_due(lid):
                return (lid not in last_sync) or (t_sec - last_sync.get(lid, -1e9) >= SYNC_EVERY_S)

            # TWO-TIER throughput win: embed ONLY tracks due for a global sync.
            # A track keeps its global id between syncs, and the tracker holds its
            # box, so a stable-id track costs ZERO NPU (no embed/face/gait) until it
            # is due again (~every SYNC_EVERY_S). Also skip boxes too short to matter.
            eidx = [i for i, tr in enumerate(tracks)
                    if (tr.bbox[3] - tr.bbox[1]) >= MIN_EMBED_H and i < len(crops)
                    and hits.get(int(tr.local_id), 0) >= TRACK_MIN_HITS
                    and _sync_due(int(tr.local_id))]
            if MAX_EMBED_PER_FRAME and len(eidx) > MAX_EMBED_PER_FRAME:
                # over budget: keep the biggest (closest) boxes so one frame fits the fps slot
                eidx = sorted(eidx, key=lambda i: tracks[i].bbox[3] - tracks[i].bbox[1],
                              reverse=True)[:MAX_EMBED_PER_FRAME]
                eidx.sort()
            crops_e = [crops[i] for i in eidx]
            t1 = time.perf_counter(); embs_e = emb.embed(crops_e) if crops_e else None; tmb = (time.perf_counter() - t1) * 1000
            if crops_e:
                ew = (1 - a) * ew + a * emb.last_wait; ecp = (1 - a) * ecp + a * emb.last_compute; n_emb += 1

            tfc = 0.0
            fembs = None
            fmeta = None
            # face for every DUE track (eidx is already sync-gated, so no extra frame
            # cadence -- we want the face on the summary frame, not a random 1/3 frame).
            if face is not None and crops_e:
                t1 = time.perf_counter()
                if hasattr(face, "embed_with_meta"):
                    fembs, fmeta = face.embed_with_meta(crops_e)
                else:
                    fembs, fmeta = face.embed(crops_e), None
                tfc = (time.perf_counter() - t1) * 1000
                fw = (1 - a) * fw + a * face.last_wait; fcp = (1 - a) * fcp + a * face.last_compute; n_fa += 1
                for j, i in enumerate(eidx):
                    if j < len(fembs):
                        _f = fembs[j]
                        _fq = 0.0
                        _fm = {}
                        if fmeta is not None and j < len(fmeta):
                            try:
                                _fm = fmeta[j] or {}
                                _fq = float(_fm.get("face_q", 0.0))
                            except Exception:
                                _fq = 0.0
                                _fm = {}
                        if (_f is not None and
                                float(np.linalg.norm(np.asarray(_f, np.float32))) > 1e-6 and
                                _face_meta_ok(_fm)):
                            _face_for_id = _buf_put(facebuf, tracks[i].local_id, _f, _fq, cap=5)
                            fcache[tracks[i].local_id] = _face_for_id if _face_for_id is not None else _f
                            fqcache[tracks[i].local_id] = _fq
                            # Publish HOW GOOD the detection was, not just the vector.
                            # AdaFace returns a valid-looking 512-d vector for any chip,
                            # including one aligned from hair or a neck, and those score
                            # 0.57-0.67 against enrolled faces -- as high as genuine
                            # frontal matches. Downstream cannot tell them apart without
                            # these numbers, which were being computed and discarded.
                            fmcache[tracks[i].local_id] = {
                                "det": float(_fm.get("det_score", 0.0)),
                                "w": float(_fm.get("face_w", 0.0)),
                                "h": float(_fm.get("face_h", 0.0)),
                                "sharp": float(_fm.get("sharp", 0.0)),
                                "q": _fq}
                _cl = os.environ.get('FACE_CAP_LOG')
                if _cl:
                    try:
                        with open(_cl, 'a') as _cf:
                            for j, i in enumerate(eidx):
                                if j < len(fembs):
                                    _ff = fembs[j]
                                    _valid = _ff is not None and float(np.linalg.norm(np.asarray(_ff, np.float32))) > 1e-6
                                    _lid = int(tracks[i].local_id)
                                    _fq_log = None
                                    if fmeta is not None and j < len(fmeta):
                                        _fq_log = (fmeta[j] or {}).get("face_q")
                                    _cf.write(json.dumps({'f': int(frames), 'cam': str(camera), 'lid': _lid, 'cap_face': int(_valid), 'face_q': _fq_log, 'retained': int(_lid in fcache), 'hits': int(hits.get(_lid, 0))}) + '\n')
                    except Exception:
                        pass
            tgt = 0.0
            if gait is not None and frames % ga_every == 0:
                # GAIT needs a DENSE, near-consecutive silhouette sequence (min_len=20).
                # The sync-gating (eidx, ~1 sample/2s) starved it -> gait was 0%. So
                # accumulate for ALL moving MATURE tracks every ga_every frames, NOT
                # only sync-due ones; the buffer then fills in ~min_len*ga_every frames
                # and the embedding is cached for use at sync time. Seated/standing
                # people are skipped by _moving (useless + heavy).
                gcand = [tr for tr in tracks
                         if hits.get(int(tr.local_id), 0) >= TRACK_MIN_HITS
                         and (tr.bbox[3] - tr.bbox[1]) >= MIN_EMBED_H
                         and _moving(int(tr.local_id))]
                if MAX_EMBED_PER_FRAME and len(gcand) > MAX_EMBED_PER_FRAME:
                    gcand = sorted(gcand, key=lambda t: t.bbox[3] - t.bbox[1],
                                   reverse=True)[:MAX_EMBED_PER_FRAME]
                if gcand:
                    # feed the gait server the STABLE identity as the buffer key (survives
                    # tracker breaks) but map results back to the live local_id for gcache.
                    from types import SimpleNamespace
                    gstab = [SimpleNamespace(local_id=stable_id.get(int(tr.local_id), int(tr.local_id)),
                                             bbox=tr.bbox) for tr in gcand]
                    # Pass the processed-frame index so OnlineGaitEmbedder can honour
                    # its inference cadence after the temporal buffer matures. Omitting
                    # it made every eligible call re-run GaitBase.
                    t1 = time.perf_counter(); gembs = gait.embed_tracks(
                        frame, gstab, str(sid), frame_idx=frames)
                    tgt = (time.perf_counter() - t1) * 1000
                    gw = (1 - a) * gw + a * gait.last_wait; gcp = (1 - a) * gcp + a * gait.last_compute; n_ga += 1
                    for tr, g in zip(gcand, gembs):
                        if g is not None:
                            gcache[int(tr.local_id)] = g

            gids = [None] * len(tracks)

            # ---- TWO-TIER re-id (no per-frame blocking round-trip) ----
            # 1) drain async global replies -> refresh the local->global map + names
            while True:
                try:
                    r = resp_q.get_nowait()
                except Exception:
                    break
                rl = r.get("lid")
                if rl is None:
                    continue
                rl = int(rl)
                g = r.get("gid")
                if g is not None:
                    local2global[rl] = g; last_gid[rl] = g; gids_seen.add(g)
                nm = r.get("name")
                if nm:
                    last_name[rl] = nm
                    last_name_at[rl] = time.monotonic()
                elif r.get("name_clear"):
                    last_name.pop(rl, None)
                    last_name_at.pop(rl, None)

            # 2) the embedded tracks (eidx) ARE exactly the ones due for a sync this
            #    frame -> fire one summary each (fire-and-forget) and mark them synced.
            #    covis is every co-visible track in this camera, so the service can add
            #    the same-camera "different people" negatives.
            if embs_e is not None and len(embs_e) == len(eidx) and eidx:
                covis_all = [int(tr.local_id) for tr in tracks]
                for j, i in enumerate(eidx):
                    lid = int(tracks[i].local_id)
                    body_q, body_q_parts = _body_crop_quality(
                        crops[i], tracks[i].bbox, frame.shape, return_parts=True)
                    body_ok = _body_identity_ok(
                        crops[i], tracks[i].bbox, frame.shape, quality=body_q)
                    # A seed-grade crop is always match-grade; computing it separately
                    # only matters for the crops that fail the seed gate.
                    match_ok = body_ok or _body_match_ok(
                        crops[i], tracks[i].bbox, frame.shape, quality=body_q)
                    _ql = os.environ.get("BODY_QUALITY_LOG")
                    if _ql:
                        try:
                            with open(_ql, "a", encoding="utf-8") as _qf:
                                _qf.write(json.dumps({"camera": str(camera), "frame": int(frames),
                                                     "lid": lid, "quality": round(body_q, 4),
                                                     "body_ok": int(body_ok),
                                                     "match_ok": int(match_ok),
                                                     **body_q_parts}) + "\n")
                        except Exception:
                            pass
                    # Retain the latest valid body evidence for this local track.
                    # LOCAL_REASSOC uses it to reject a different person entering a
                    # recently lost box; this state was declared but never populated,
                    # silently reducing that guard to position-only matching.
                    _app_now = embs_e[j]
                    _app_for_id = _app_now
                    # MATCH gate, not the seed gate. This buffer only ever feeds
                    # LOCAL_REASSOC's "is the person re-entering this box the same
                    # person" test, which is still gated at REASSOC_APP_GATE (0.25) and
                    # re-verified at REASSOC_VERIFY_DIST (0.30). Gating it on the seed
                    # crop meant a track with no clean crop had no appearance vector, so
                    # REASSOC_REQUIRE_APP skipped it and it could not rejoin either --
                    # one failed gate silenced the track four different ways.
                    if (match_ok and _app_now is not None and
                            float(np.linalg.norm(np.asarray(_app_now, np.float32))) > 1e-6):
                        _app_best = _buf_put(appbuf, lid, _app_now, body_q, cap=5)
                        _app_for_id = _app_best if _app_best is not None else _app_now
                        best.setdefault(lid, {})["app"] = np.asarray(_app_for_id, np.float32).copy()
                    reid_q += 1
                    g = local2global.get(lid)
                    if g is not None:
                        pid = sid * 100000 + g
                        if pid not in sent:
                            sent.add(pid)
                            try:
                                ok2, jb = cv2.imencode(".jpg", crops[i])
                                if ok2:
                                    crop_q.put_nowait((pid, jb.tobytes()))
                            except Exception:
                                pass
                    _fv, _gv = fcache.get(lid), gcache.get(lid)
                    _fqv = float(fqcache.get(lid, 1.0))
                    # Unlike `_fv` (the Re-ID prototype cache), this is strictly
                    # the face result from THIS sync frame. A zero row explicitly
                    # means the detector checked but found no usable face.
                    _face_now = None
                    _face_now_q = 0.0
                    if face is not None and fembs is not None and j < len(fembs):
                        _candidate = fembs[j]
                        _meta_now = fmeta[j] if fmeta is not None and j < len(fmeta) else {}
                        if (_candidate is not None and
                                float(np.linalg.norm(np.asarray(_candidate, np.float32))) > 1e-6 and
                                _face_meta_ok(_meta_now)):
                            _face_now = _candidate
                            _face_now_q = _fqv
                            if _meta_now:
                                try:
                                    _face_now_q = float((_meta_now or {}).get("face_q", _face_now_q))
                                except Exception:
                                    pass
                    n_sum += 1
                    if _fv is not None and float(np.linalg.norm(np.asarray(_fv, np.float32))) > 1e-6:
                        n_facev += 1
                    if _gv is not None and float(np.linalg.norm(np.asarray(_gv, np.float32))) > 1e-6:
                        n_gaitv += 1
                    try:
                        # Send only a face detected on THIS sync.  `_fv` is a cached
                        # per-track face prototype; if the tracker swaps people without
                        # changing local_id, that stale face can contaminate the new gid
                        # and make a stranger inherit an enrolled name.  Temporal
                        # persistence belongs in the identity/name state, not in the
                        # raw face evidence stream.
                        req_q.put({"t": "sum", "sid": sid, "cam": camera, "ts": t_sec, "lid": lid,
                                   "a": _ser(_app_for_id), "q": body_q, "f": _ser(_face_now),
                                   "body_ok": bool(body_ok),
                                   "match_ok": bool(match_ok),
                                   "fq": _fqv,
                                   "fn": _ser(_face_now), "face_checked": face is not None,
                                   "fnq": _face_now_q,
                                   "g": _ser(_gv), "c": _ser(_color_hist(crops[i])),
                                   "covis": [c for c in covis_all if c != lid]})
                        last_sync[lid] = t_sec
                    except Exception:
                        pass
                    # emit one observation to whichever sink is active: the in-process
                    # OBS_Q (PLATF app owns the engine) OR the disk JSONL (standalone
                    # capture). Both write-only; neither touches the sum/re-id above.
                    # Unset both (standalone :8083) => this whole block is skipped.
                    if OBS_Q is not None or obs_f is not None:
                        try:
                            def _lst(v):
                                return (np.asarray(v, np.float32).tolist()
                                        if v is not None else None)
                            bx = [float(c) for c in tracks[i].bbox]
                            sidv = int(stable_id.get(int(lid), int(lid)))
                            # save the person crop the UI/crop-audit shows (throttled to
                            # ~1 per (stable id, camera) per second). Guarded by
                            # OBS_DUMP_DIR so the app can get crops without the giant JSONL.
                            crop_rel = None
                            if OBS_DUMP_DIR:
                                ckey = (sidv, str(camera))
                                if (ckey not in obs_crop_seen
                                        or (frames - obs_crop_seen[ckey]) >= max(1, int(TARGET_FPS))):
                                    ok3, cjb = cv2.imencode(".jpg", crops[i],
                                                            [cv2.IMWRITE_JPEG_QUALITY, 80])
                                    if ok3:
                                        crop_rel = f"crops/{camera}_{sidv}_{int(frames)}.jpg"
                                        with open(os.path.join(OBS_DUMP_DIR, crop_rel), "wb") as _cf:
                                            _cf.write(cjb.tobytes())
                                        obs_crop_seen[ckey] = frames
                            obs = {
                                "camera": str(camera), "local_id": int(lid),
                                # break-stable id from the tracker's own reassoc: lets
                                # the platform bind on a per-frame-continuous key while
                                # still reporting the raw track id for the metric join.
                                "stable_id": sidv,
                                # the backbone's OWN authoritative global id (local2global
                                # == g above). The platform INGESTS this -> single identity
                                # engine. None until the async two-tier reply resolves it.
                                "gid": (int(g) if g is not None else None),
                                "frame": int(frames), "t": float(t_sec), "bbox": bx,
                                "frame_wh": [int(frame.shape[1]), int(frame.shape[0])],
                                "display_wh": [OUTPUT_W, OUTPUT_H],
                                "quality": body_q, "app_emb": _lst(embs_e[j]),
                                "body_ok": bool(body_ok),
                                "face_emb": _lst(_face_now), "gait_emb": _lst(_gv),
                                # torso colour histogram -- the offline pipeline's cross-cam
                                # precision key (reject a merge if colour disagrees). The
                                # platform's GBSL mapper uses it as a cross-cam gate.
                                "color": _lst(_color_hist(crops[i])),
                                # How good the face DETECTION was that produced face_emb.
                                # Without it a downstream consumer cannot distinguish a
                                # real frontal face from a chip aligned out of hair, and
                                # both yield a valid unit vector.
                                "face_meta": fmcache.get(lid),
                                "crop": crop_rel}
                            if OBS_Q is not None:
                                # in-process bus: drop if full (never block the worker)
                                try:
                                    OBS_Q.put_nowait(obs)
                                except Exception:
                                    pass
                            elif obs_f is not None:
                                obs_f.write(json.dumps(obs) + "\n")
                                # rotate: keep the disk dump bounded (embeddings -> GBs).
                                # Platform tails only new appends, so truncating in place
                                # drops already-consumed history safely.
                                if OBS_DUMP_MAX_BYTES and obs_f.tell() > OBS_DUMP_MAX_BYTES:
                                    obs_f.seek(0)
                                    obs_f.truncate()
                                    obs_f.flush()
                        except Exception:
                            pass

            # 3) prune per-track state to live tracks; display id comes from the map,
            #    showing the local track id ("T<n>") until the first global reply
            fcache = {k: v for k, v in fcache.items() if k in live_ids}
            fqcache = {k: v for k, v in fqcache.items() if k in live_ids}
            fmcache = {k: v for k, v in fmcache.items() if k in live_ids}
            gcache = {k: v for k, v in gcache.items() if k in live_ids}
            appbuf = {k: v for k, v in appbuf.items() if k in live_ids}
            facebuf = {k: v for k, v in facebuf.items() if k in live_ids}
            last_sync = {k: v for k, v in last_sync.items() if k in live_ids}
            local2global = {k: v for k, v in local2global.items() if k in live_ids}
            last_gid = {k: v for k, v in last_gid.items() if k in live_ids}
            last_name = {k: v for k, v in last_name.items() if k in live_ids}
            last_name_at = {k: v for k, v in last_name_at.items() if k in live_ids}
            draw_bbox = {k: v for k, v in draw_bbox.items() if k in live_ids}
            if NAME_HOLD_S:
                now_mono = time.monotonic()
                expired = [k for k, at in last_name_at.items()
                           if now_mono - at > NAME_HOLD_S]
                for k in expired:
                    last_name.pop(k, None)
                    last_name_at.pop(k, None)
            stable_id = {k: v for k, v in stable_id.items() if k in live_ids}
            # HARD display invariant: within one camera/frame, one global id belongs
            # to at most one live track. Async gallery repair replies can otherwise
            # collapse two already-visible people after the service's match-time
            # exclusion. Keep the longest-lived track as owner; immediately unclaim
            # and re-sync every conflicting track so this is both hidden now and
            # corrected at the identity source on the next processed frame.
            by_gid = {}
            for _lid, _gid in list(local2global.items()):
                if _gid is None:
                    continue
                _owner = by_gid.get(_gid)
                if _owner is None:
                    by_gid[_gid] = _lid
                    continue
                _reject = _lid
                if hits.get(_lid, 0) > hits.get(_owner, 0):
                    _reject = _owner
                    by_gid[_gid] = _lid
                local2global.pop(_reject, None)
                last_gid.pop(_reject, None)
                last_name.pop(_reject, None)
                last_name_at.pop(_reject, None)
                last_sync.pop(_reject, None)
                try:
                    req_q.put({"t": "unclaim", "sid": sid, "lid": int(_reject)})
                except Exception:
                    pass
            # WITHIN-CAMERA RE-ASSOCIATION: an unassigned track that spatially
            # continues a just-lost assigned track inherits its gid immediately -- no
            # async round-trip, no T->P flash (kills the 22% tracker-break flicker).
            if LOCAL_REASSOC:
                cur = {int(tr.local_id): tr.bbox for tr in tracks}
                for lid, bb in cur.items():
                    if lid in local2global:
                        continue
                    if not _bbox_body_geometry_ok(bb, frame.shape):
                        continue
                    # match a re-appearing track to a lost one by IoU OR CENTER-PROXIMITY.
                    # IoU alone fails on this footage: person boxes are small (~40px) and a
                    # re-detection after an occlusion is shifted enough that the boxes touch
                    # but don't overlap (IoU 0), so the rejoin never fired -> churn. Accept a
                    # candidate whose centre is within REASSOC_CENTER_FRAC of the box height.
                    bestscore, bestg, best_src = REASSOC_IOU, None, None
                    selected_appdist = None
                    napp = best.get(lid, {}).get('app')
                    if REASSOC_REQUIRE_APP and napp is None:
                        continue
                    _bcx, _bcy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
                    _bh = max(1.0, float(bb[3] - bb[1]))
                    for llid, lval in lost_mem.items():
                        lbox, lgid, lf = lval[0], lval[1], lval[2]
                        lapp = lval[3] if len(lval) > 3 else None
                        if frames - lf > REASSOC_GAP or lgid is None:
                            continue
                        if REASSOC_REQUIRE_APP and lapp is None:
                            continue
                        v = _bb_iou(bb, lbox)
                        lcx, lcy = (lbox[0] + lbox[2]) / 2.0, (lbox[1] + lbox[3]) / 2.0
                        cd = ((_bcx - lcx) ** 2 + (_bcy - lcy) ** 2) ** 0.5
                        # centre-proximity score in [0,1] (1 = same centre); use the better of
                        # IoU and centre-score so small shifted boxes still match.
                        cscore = max(0.0, 1.0 - cd / (REASSOC_CENTER_FRAC * _bh))
                        score = max(v, cscore)
                        if score <= bestscore:
                            continue
                        candidate_appdist = None
                        if napp is not None and lapp is not None:
                            _na = np.asarray(napp, np.float32); _la = np.asarray(lapp, np.float32)
                            _dn = (np.linalg.norm(_na) * np.linalg.norm(_la)) + 1e-9
                            candidate_appdist = 1.0 - float(np.dot(_na, _la) / _dn)
                            if candidate_appdist > REASSOC_APP_GATE:
                                continue
                        bestscore, bestg, best_src = score, lgid, llid
                        selected_appdist = candidate_appdist
                    if os.environ.get('REASSOC_LOG'):
                        _dbi, _dbg, _dba = 0.0, None, None
                        for _ll, _lv in lost_mem.items():
                            if frames - _lv[2] > REASSOC_GAP or _lv[1] is None:
                                continue
                            _vv = _bb_iou(bb, _lv[0])
                            if _vv > _dbi:
                                _dbi, _dbg = _vv, _lv[1]
                                _la0 = _lv[3] if len(_lv) > 3 else None
                                if napp is not None and _la0 is not None:
                                    _na0 = np.asarray(napp, np.float32); _la1 = np.asarray(_la0, np.float32)
                                    _dba = round(1.0 - float(np.dot(_na0, _la1) / ((np.linalg.norm(_na0) * np.linalg.norm(_la1)) + 1e-9)), 3)
                                else:
                                    _dba = None
                        # nearest lost box by CENTER distance (regardless of IoU) -- to see if
                        # the re-appearing box is near a lost one (rejoin should work) or isolated
                        _ncx, _ncy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
                        _ncd, _nlb = 1e9, None
                        for _ll, _lv in lost_mem.items():
                            if frames - _lv[2] > REASSOC_GAP or _lv[1] is None:
                                continue
                            _lb = _lv[0]; _lcx, _lcy = (_lb[0] + _lb[2]) / 2.0, (_lb[1] + _lb[3]) / 2.0
                            _d = ((_ncx - _lcx) ** 2 + (_ncy - _lcy) ** 2) ** 0.5
                            if _d < _ncd:
                                _ncd, _nlb = _d, _lb
                        try:
                            with open(os.environ['REASSOC_LOG'], 'a') as _rf:
                                _rf.write(json.dumps({'fr': int(frames), 'cam': str(camera), 'lid': int(lid),
                                    'nbox': [round(float(x)) for x in bb], 'best_iou': round(_dbi, 2),
                                    'cand_gid': _dbg, 'appdist': _dba, 'inherited': bestg, 'n_lost': len(lost_mem),
                                    'selected_appdist': (round(selected_appdist, 3)
                                                         if selected_appdist is not None else None),
                                    'nearest_lost_box': ([round(float(x)) for x in _nlb] if _nlb is not None else None),
                                    'nearest_center_dist': round(_ncd) if _nlb is not None else None}) + '\n')
                        except Exception:
                            pass
                    if bestg is not None:
                        local2global[lid] = bestg; last_gid[lid] = bestg; gids_seen.add(bestg)
                        # gait buffer follows the PERSON: the re-acquired track inherits the
                        # lost track's stable identity so its silhouette buffer continues.
                        if best_src is not None:
                            stable_id[lid] = stable_id.get(best_src, best_src)
                        lost_mem = {k: v for k, v in lost_mem.items() if v[1] != bestg}
                        # tell the service so the next sync reinforces (not re-matches)
                        try:
                            req_q.put({"t": "claim", "sid": sid, "lid": lid, "gid": int(bestg)})
                        except Exception:
                            pass
                live_now = set(cur)
                for plid, (pbox, pg) in prev_state.items():
                    if plid not in live_now and pg is not None:
                        lost_mem[plid] = (pbox, pg, frames, best.get(plid, {}).get('app'))
                lost_mem = {k: v for k, v in lost_mem.items() if frames - v[2] <= REASSOC_GAP}
                # Retain appearance only for live tracks and the bounded lost-track
                # window. Pruning it before constructing lost_mem discarded exactly
                # the evidence the reassociation appearance gate needed.
                _keep_best = live_now | set(lost_mem)
                best = {k: v for k, v in best.items() if k in _keep_best}
                prev_state = {int(tr.local_id): (tr.bbox, local2global.get(int(tr.local_id))) for tr in tracks}
            else:
                best = {k: v for k, v in best.items() if k in live_ids}

            for i, tr in enumerate(tracks):
                gids[i] = local2global.get(int(tr.local_id))

            if os.environ.get('TRACK_LIFE_LOG'):
                try:
                    with open(os.environ['TRACK_LIFE_LOG'], 'a') as _tlf:
                        for _ti, _tr in enumerate(tracks):
                            _tl = int(_tr.local_id)
                            _tlf.write(json.dumps({'fr': int(frames), 'cam': str(camera), 'lid': _tl, 'gid': (int(gids[_ti]) if gids[_ti] is not None else None), 'hits': int(hits.get(_tl, 0)), 'foot': [round(float(_tr.bbox[0] + _tr.bbox[2]) / 2.0), round(float(_tr.bbox[3]))]}) + '\n')
                except Exception:
                    pass

            if ev_w is not None:
                for i, tr in enumerate(tracks):
                    lid = int(tr.local_id)
                    g = gids[i]
                    # the DRAWN label (what the eye sees): name > P<gid> > T<track>.
                    # Log EVERY track incl. the T-state so flicker sees the T->P
                    # transition; gid=-1 marks unassigned so merge/frag/noise skip it.
                    nm = last_name.get(lid)
                    if nm:
                        lab = nm
                    elif g is not None:
                        lab = "P%d" % int(g)
                    elif last_gid.get(lid) is not None:
                        lab = "P%d" % int(last_gid[lid])   # briefly lost gid -> hold it
                    else:
                        lab = "T"                          # truly unassigned (logical)
                    b = tr.bbox
                    ev_w.writerow([camera, frames, lid, int(g) if g is not None else -1, lab,
                                   round(float(b[0]), 1), round(float(b[1]), 1),
                                   round(float(b[2]), 1), round(float(b[3]), 1)])
                    if evface_w is not None and g is not None:
                        fv = fcache.get(lid)
                        fkey = (int(g), frames // 30)
                        if fv is not None and fkey not in face_logged:
                            face_logged.add(fkey)
                            evface_w.writerow([camera, int(g),
                                               ";".join(f"{x:.4f}" for x in np.asarray(fv, np.float32).ravel())])
                if frames % 100 == 0:
                    ev_f.flush()   # survive SIGTERM teardown
                    if evface_f is not None:
                        evface_f.flush()

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
                h, w = frame.shape[:2]
                sx, sy = OUTPUT_W / float(w), OUTPUT_H / float(h)
                small = cv2.resize(frame, (OUTPUT_W, OUTPUT_H), interpolation=cv2.INTER_LINEAR)
                ui_scale = float(os.environ.get(
                    "OVERLAY_UI_SCALE",
                    str(max(1.0, min(2.0, OUTPUT_W / 960.0, OUTPUT_H / 540.0)))))
                font_scale = float(os.environ.get("OVERLAY_FONT_SCALE", "0.48")) * ui_scale
                text_thickness = max(1, int(round(1.2 * ui_scale)))
                crowded = len(tracks) >= int(os.environ.get("COMPACT_LABEL_MIN_PEOPLE", "8"))
                label_font_scale = font_scale * (0.72 if crowded else 1.0)
                label_text_thickness = max(1, int(round(text_thickness * (0.75 if crowded else 1.0))))
                box_thickness = max(2, int(round(1.2 * ui_scale)))
                alert_thickness = max(box_thickness + 2, int(round(4 * ui_scale)))
                pad = max(3, int(round(3 * ui_scale)))
                header_h = int(round(32 * ui_scale))
                header_w = min(OUTPUT_W, int(round(250 * ui_scale)))
                label_occupied = [(0, 0, header_w, header_h)]
                for idx, tr in enumerate(tracks if DRAW_OVERLAY else ()):
                    lid = int(tr.local_id)
                    gid = gids[idx] if idx < len(gids) else None
                    if gid is None and last_gid.get(lid) is not None:
                        gid = last_gid[lid]           # hold last id through a brief gap
                    braw = np.asarray(tr.bbox, np.float32)
                    bdraw = braw
                    if DISPLAY_BBOX_SMOOTH > 0:
                        prevb = draw_bbox.get(lid)
                        if prevb is not None and _bb_iou(prevb, braw) >= DISPLAY_BBOX_RESET_IOU:
                            a_s = max(0.0, min(0.95, DISPLAY_BBOX_SMOOTH))
                            bdraw = (a_s * np.asarray(prevb, np.float32)
                                     + (1.0 - a_s) * braw).astype(np.float32)
                        else:
                            bdraw = braw.copy()
                        draw_bbox[lid] = bdraw.copy()
                    x1 = int(round(float(bdraw[0]) * sx))
                    y1 = int(round(float(bdraw[1]) * sy))
                    x2 = int(round(float(bdraw[2]) * sx))
                    y2 = int(round(float(bdraw[3]) * sy))
                    col = ((gid * 37) % 255, (gid * 91) % 255, (gid * 17) % 255) if gid else (150, 150, 150)
                    alerted = False
                    if ALERT_TRACKS is not None:
                        stable = int(stable_id.get(lid, lid))
                        try:
                            until = max(float(ALERT_TRACKS.get(f"{camera}:track:{stable}", -1e30)),
                                        float(ALERT_TRACKS.get(f"{camera}:gid:{gid}", -1e30))
                                        if gid is not None else -1e30)
                            alerted = t_sec <= until
                        except Exception:
                            alerted = False
                    draw_col = (0, 0, 255) if alerted else col
                    cv2.rectangle(small, (x1, y1), (x2, y2), draw_col,
                                  alert_thickness if alerted else box_thickness)
                    # A recognized name augments (never replaces) the global id. Both
                    # are burned into this exact frame using this exact track box.
                    # reply is still in flight the box is drawn UNLABELED (a label
                    # appearing beats "T5"->"P12" flashing) unless HIDE_PROVISIONAL off.
                    nm = last_name.get(lid)
                    if gid and nm:
                        lab = "P%d %s" % (gid, nm)
                    elif nm:
                        lab = nm
                    elif gid:
                        lab = "P%d" % gid
                    else:
                        lab = "" if HIDE_PROVISIONAL else "T%d" % lid
                    if lab:
                        (tw, th), _ = cv2.getTextSize(
                            lab, cv2.FONT_HERSHEY_SIMPLEX, label_font_scale,
                            label_text_thickness)
                        bg, org = _place_label_box(x1, y1, x2, y2, tw, th, pad,
                                                   OUTPUT_W, OUTPUT_H, label_occupied)
                        cv2.rectangle(small, (bg[0], bg[1]), (bg[2], bg[3]), draw_col, -1)
                        cv2.putText(small, lab, org,
                                    cv2.FONT_HERSHEY_SIMPLEX, label_font_scale, (0, 0, 0),
                                    label_text_thickness, cv2.LINE_AA)
                if DRAW_OVERLAY and DRAW_HEADER:
                    cv2.putText(small, "#%d  %.0f fps  %d ppl  %d ids" % (sid, fps, len(tracks), len(gids_seen)),
                                (int(6 * ui_scale), int(22 * ui_scale)),
                                cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255),
                                text_thickness, cv2.LINE_AA)
                ok2, buf = cv2.imencode(
                    ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, OUTPUT_JPEG_QUALITY])
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
                    # flow split (ms EMA): queue-WAIT vs device-COMPUTE per stage, and how
                    # often each stage ran relative to detection (=every backbone frame).
                    flow=dict(det_w=round(dw, 1), det_c=round(dcp, 1),
                              emb_w=round(ew, 1), emb_c=round(ecp, 1),
                              face_w=round(fw, 1), face_c=round(fcp, 1),
                              gait_w=round(gw, 1), gait_c=round(gcp, 1),
                              r_emb=round(n_emb / max(1, n_det), 2), r_face=round(n_fa / max(1, n_det), 2),
                              r_gait=round(n_ga / max(1, n_det), 2),
                              # inside detect(): shm-read / preprocess(CPU) / infer(GPU) / postproc(CPU)
                              det_shm=round(d_shm, 1), det_pre=round(d_pre, 1),
                              det_inf=round(d_inf, 1), det_post=round(d_post, 1),
                              # what fraction of re-id summaries actually carried a REAL
                              # face / gait vector (the rest fuse on appearance alone)
                              face_valid_pct=round(100 * n_facev / max(1, n_sum), 1),
                              gait_valid_pct=round(100 * n_gaitv / max(1, n_sum), 1),
                              summaries=n_sum),
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
        if ev_f is not None:
            try: ev_f.flush(); ev_f.close()
            except Exception: pass
        if evface_f is not None:
            try: evface_f.flush(); evface_f.close()
            except Exception: pass
        if obs_f is not None:
            try: obs_f.flush(); obs_f.close()
            except Exception: pass
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
        video = eng("Video")
        video_enhance = eng("VideoEnhance")
        return {"gpu_compute": eng("Compute"), "gpu_render": eng("Render/3D"),
                "gpu_video": video, "gpu_video_enhance": video_enhance,
                "igpu_decode": video + video_enhance,
                "gpu_power_w": float(pw[-1]) if pw else 0.0}

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
            # cross-camera body gallery gates
            "xcam_threshold": rs.get("cross_camera_match_threshold", XCAM_THR),
            "xcam_no_modal_threshold": rs.get("cross_camera_no_modal_threshold", XCAM_NOMODAL_THR),
            "xcam_query_min_q": rs.get("cross_camera_query_min_quality", XCAM_QUERY_MIN_Q),
            "xcam_exemplar_min_q": rs.get("cross_camera_exemplar_min_quality", XCAM_EXEMPLAR_MIN_Q),
            "xcam_quality_rejections": rs.get("cross_camera_quality_rejections", 0),
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
                       "tracker": TRACKER, "track_iou": TRACK_IOU,
                       "det_conf": DET_CONF, "det_iou": DET_IOU,
                       "byte_low": float(os.environ.get("BYTE_LOW_THRESH", "0.10")),
                       "byte_high": float(os.environ.get("BYTE_HIGH_THRESH", "0.45")),
                       "byte_new": float(os.environ.get("BYTE_NEW_THRESH", "0.55")),
                       "body_id_min_q": BODY_ID_MIN_Q,
                       "body_id_edge_margin": BODY_ID_EDGE_MARGIN,
                       "proc_every": PROC_EVERY,
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


def setup_engine():
    """Bring up the SHARED engine: mp Manager + queues + the central reid_service (one
    gallery) + infer_pool device servers + device sampler + crop drain. Used by BOTH the
    standalone :8083 app (main) and the PLATF single-app (PLATF/app.py), so the tested
    re-id runs byte-identically in either. Call once per process tree before spawning
    stream workers (WorkerHandle)."""
    global MGR, CROP_Q, REID_REQ_Q, REID_STAT, REID_STOP, ALERT_TRACKS
    try:
        multiprocessing.set_start_method("fork")
    except RuntimeError:
        pass
    MGR = multiprocessing.Manager()
    CROP_Q = MGR.Queue()
    REID_REQ_Q = MGR.Queue()
    REID_STAT = MGR.dict(persons=0, live=0, matches=0, queries=0, hit_rate=0.0)
    ALERT_TRACKS = MGR.dict()
    REID_STOP = MGR.Event()
    multiprocessing.Process(target=reid_service, args=(REID_REQ_Q, REID_STOP, DESIRED_THR),
                            daemon=True).start()
    for kind in POOL_KINDS:
        POOL_Q[kind] = MGR.Queue()
        for ri in range(POOL_REPLICAS[kind]):
            cfg = POOL_CFG[kind]
            if kind == "det" and DET_DEVICES:   # split det replicas across devices
                cfg = {**cfg, "device": DET_DEVICES[ri % len(DET_DEVICES)]}
            multiprocessing.Process(target=infer_pool.infer_server,
                                    args=(kind, cfg, POOL_Q[kind], REID_STOP),
                                    daemon=True).start()
    if DET_DEVICES:
        print(f"[POOL] det replicas split across devices: {DET_DEVICES}", flush=True)
    SAMPLER.start()
    threading.Thread(target=_crop_drain, daemon=True).start()
    print(f"[POOL] shared device servers: det={DET_MODEL}@{DET_DEV} embed={EMB_MODEL}@{EMB_DEV} "
          f"face={FACE_MODEL}@{FACE_DEV} gait={GAIT_MODEL}@{GAIT_DEV}+seg@{SEG_DEV}", flush=True)
    print(f"[POOL] replicas: {POOL_REPLICAS} -- constant model memory, streams decode/track only", flush=True)


def main():
    setup_engine()
    print(f"Pooled stream dashboard on http://0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
