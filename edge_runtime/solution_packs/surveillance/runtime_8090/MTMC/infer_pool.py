"""Shared inference servers for the live MTMC apps.

Problem this solves: in streamapp.py / streamapp_int8.py every stream is its own
process and every process compiles its OWN copy of all six models (~1.0 GB each,
measured). Model memory therefore scales linearly with cameras -- 15 streams cost
~15 GB of duplicated weights and the box hits its RAM wall long before it runs out
of compute.

Here the models live in a small pool of DEVICE SERVER processes instead:

    det   server  -> yolo11s            (iGPU)
    embed server  -> transreid          (NPU)
    face  server  -> scrfd + adaface    (iGPU)
    gait  server  -> yolov8n-seg (iGPU) + gaitbase (NPU)

One copy of each model, regardless of how many cameras run. Stream workers keep
only decode + track + draw, so a worker costs ~0.2 GB instead of ~1.0 GB.

The client objects below are DROP-IN replacements for the in-process model objects
(`detect`, `embed`, `embed_tracks` keep their signatures), so the per-stream
pipeline code is unchanged.

Transport: full frames go through /dev/shm via multiprocessing.shared_memory (a
720p BGR frame is 2.7 MB -- pickling that per frame per stream would cost more than
the inference). Crops are small and go through the queue directly. Each worker
blocks on its own response queue, so a frame buffer is never rewritten while a
server is reading it.

The embed server BATCHES: while a request is being served, further requests from
other streams queue up, and the server concatenates them into one inference call.
The NPU is dispatch-bound (~90% of a per-crop call is overhead), so batching across
cameras is where the throughput comes from.
"""
from __future__ import annotations

import os
import time
from multiprocessing import shared_memory

import numpy as np

BATCH_MAX = int(os.environ.get("POOL_BATCH_MAX", "16"))     # crops fused into one embed call
BATCH_WAIT_MS = float(os.environ.get("POOL_BATCH_WAIT_MS", "3"))  # linger for more work
REQ_TIMEOUT = float(os.environ.get("POOL_REQ_TIMEOUT", "20"))

# Which server kinds fuse cross-stream requests into one device dispatch. embed
# always does (NPU is dispatch-bound). det/face are on the iGPU and only batch
# when explicitly enabled -- keeps :8082 (env unset) byte-for-byte unchanged while
# :8083 opts in. DET fuses N FRAMES into one detect_batch(); FACE fuses crop lists.
DET_BATCH = bool(int(os.environ.get("POOL_DET_BATCH", "0")))
FACE_BATCH = bool(int(os.environ.get("POOL_FACE_BATCH", "0")))
BATCHABLE = {"embed"} | ({"det"} if DET_BATCH else set()) | ({"face"} if FACE_BATCH else set())
DET_BATCH_MAX = int(os.environ.get("POOL_DET_BATCH_MAX", "8"))   # frames per detect dispatch

# Flow profiling: split each pool call into QUEUE-WAIT (time the request sat in the
# shared queue before a replica grabbed it -- the "stage-to-stage delay") vs COMPUTE
# (device work). Cross-process wall clock (time.time) so client-send and server-
# dequeue stamps are comparable. Off by default -> zero fields, :8082 unchanged.
PROFILE_FLOW = bool(int(os.environ.get("PROFILE_FLOW", "0")))


# ---------------------------------------------------------------- shared frames
class FrameTx:
    """Writer side: one reusable /dev/shm buffer per stream."""

    def __init__(self, nbytes: int) -> None:
        self.nbytes = int(nbytes)
        self.shm = shared_memory.SharedMemory(create=True, size=self.nbytes)
        self.name = self.shm.name

    def put(self, frame: np.ndarray):
        arr = np.ndarray(frame.shape, frame.dtype, buffer=self.shm.buf)
        np.copyto(arr, frame)                      # single copy, no pickling
        return (self.name, frame.shape, frame.dtype.str)

    def close(self) -> None:
        try:
            self.shm.close()
            self.shm.unlink()
        except Exception:
            pass

    def reset(self) -> None:
        self.close()
        self.shm = shared_memory.SharedMemory(create=True, size=self.nbytes)
        self.name = self.shm.name


class FrameRx:
    """Reader side: attach each buffer once, then reuse the mapping."""

    def __init__(self) -> None:
        self._cache: dict[str, shared_memory.SharedMemory] = {}

    def get(self, ref) -> np.ndarray:
        name, shape, dtype = ref
        s = self._cache.get(name)
        if s is None:
            s = shared_memory.SharedMemory(name=name)
            self._cache[name] = s
        return np.ndarray(tuple(shape), np.dtype(dtype), buffer=s.buf)

    def close(self) -> None:
        for s in self._cache.values():
            try:
                s.close()
            except Exception:
                pass
        self._cache.clear()


class _Track:
    """Minimal stand-in for the tracker's track objects (gait uses .bbox/.local_id)."""

    __slots__ = ("local_id", "bbox")

    def __init__(self, local_id, bbox):
        self.local_id = local_id
        self.bbox = np.asarray(bbox, dtype=np.float32)


# ---------------------------------------------------------------- server loop
def _reply(msg, payload, err=None, tw=None, tc=None, sub=None):
    """Reply on the queue the caller shipped with the request. Carrying the queue
    per-request (instead of a sid -> queue registry) is what lets several REPLICAS
    of a server consume the same request queue: any replica can answer any stream.
    tw/tc (when profiling) = queue-wait / compute seconds for this call; sub = inside-
    detect breakdown {shm,pre,infer,post} ms."""
    q = msg.get("resp")
    if q is None:
        return
    out = {"rid": msg.get("rid"), "r": payload, "err": err}
    if tw is not None:
        out["_tw"], out["_tc"] = tw, tc
    if sub is not None:
        out["_sub"] = sub
    try:
        q.put(out)
    except Exception:
        pass


def infer_server(kind: str, cfg: dict, req_q, stop_ev):
    """One process per device role. `cfg` carries model paths/devices so this file
    stays free of app-level configuration."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    rx = FrameRx()
    model = None

    def build():
        if kind == "det":
            from MTMC.ov_backends import OVDetector
            return OVDetector(cfg["xml"], cfg["conf"], cfg.get("iou", 0.5), {0}, device=cfg["device"],
                              dyn_batch=DET_BATCH)
        if kind == "embed":
            from MTMC.ov_backends import OVReidEmbedder
            return OVReidEmbedder(cfg["xml"], device=cfg["device"])
        if kind == "face":
            from MTMC.face_embedder import AdaFaceEmbedder
            return AdaFaceEmbedder(backend="openvino", ov_xml=cfg["xml"], ov_device=cfg["device"])
        if kind == "gait":
            from MTMC.gait_embedder import OnlineGaitEmbedder
            return OnlineGaitEmbedder(backend="openvino", ov_xml=cfg["xml"], ov_device=cfg["device"],
                                      ov_seg_xml=cfg["seg_xml"], ov_seg_device=cfg["seg_device"])
        raise ValueError(kind)

    try:
        model = build()
    except Exception as e:                      # a dead server must not hang workers
        while not stop_ev.is_set():
            try:
                m = req_q.get(timeout=0.5)
            except Exception:
                continue
            if m.get("t") == "q":
                _reply(m, None, err=f"{kind} server failed to load: {e}")
        return

    _ppid0 = os.getppid()
    while not stop_ev.is_set():
        if os.getppid() != _ppid0:   # re-parented -> our parent died; do not linger
            break
        try:
            msg = req_q.get(timeout=0.5)
        except Exception:
            continue
        if msg.get("t") != "q":
            continue

        batch = [msg]
        # Opportunistic batching: fuse cross-stream requests into one device call.
        # embed always (NPU dispatch-bound); det/face only when enabled (iGPU).
        if kind in BATCHABLE:
            cap = DET_BATCH_MAX if kind == "det" else BATCH_MAX
            deadline = time.perf_counter() + BATCH_WAIT_MS / 1000.0
            while len(batch) < cap and time.perf_counter() < deadline:
                try:
                    nxt = req_q.get(timeout=max(0.0, deadline - time.perf_counter()))
                except Exception:
                    break
                if nxt.get("t") == "q":
                    batch.append(nxt)

        # timing: dequeue stamp (after any batch linger) minus each request's send
        # stamp = that request's queue-wait; compute stamp taken after the device call.
        deq = time.time() if PROFILE_FLOW else 0.0

        def _rep(m, payload, done=None, sub=None):
            if PROFILE_FLOW and "_t0" in m:
                _reply(m, payload, tw=max(0.0, deq - m["_t0"]),
                       tc=max(0.0, (done if done is not None else time.time()) - deq), sub=sub)
            else:
                _reply(m, payload)

        try:
            if kind == "embed" or (kind == "face" and FACE_BATCH):
                # crop-list servers: concat crops across streams, one call, scatter
                flat, spans = [], []
                for m in batch:
                    crops = m["crops"]
                    spans.append((m, len(flat), len(crops)))
                    flat.extend(crops)
                want_meta = kind == "face" and any(m.get("meta") for m in batch)
                if flat and want_meta and hasattr(model, "embed_with_meta"):
                    out, meta = model.embed_with_meta(flat)
                else:
                    out = model.embed(flat) if flat else []
                    meta = [{} for _ in flat]
                done = time.time() if PROFILE_FLOW else None
                for m, off, n in spans:
                    embs = list(out[off:off + n]) if n else []
                    metas = list(meta[off:off + n]) if n else []
                    _rep(m, (embs, metas) if m.get("meta") else embs, done)
            elif kind == "det":
                want_scores = any(m.get("scores") for m in batch)
                if len(batch) > 1:
                    ts = time.time() if PROFILE_FLOW else 0.0
                    frames = [rx.get(m["frame"]) for m in batch]
                    shm = (time.time() - ts) * 1000.0 / len(batch) if PROFILE_FLOW else 0.0
                    outs = (model.detect_batch_with_scores(frames)
                            if want_scores and hasattr(model, "detect_batch_with_scores")
                            else model.detect_batch(frames))   # one iGPU dispatch
                    done = time.time() if PROFILE_FLOW else None
                    sub = ({**model.last_sub, "shm": round(shm, 2)} if PROFILE_FLOW else None)
                    for m, o in zip(batch, outs):
                        if m.get("scores") and want_scores:
                            _rep(m, o, done, sub)
                        elif m.get("scores"):
                            _rep(m, (o, [1.0] * len(o)), done, sub)
                        else:
                            _rep(m, o[0] if want_scores else o, done, sub)
                else:
                    ts = time.time() if PROFILE_FLOW else 0.0
                    fr = rx.get(msg["frame"])
                    shm = (time.time() - ts) * 1000.0 if PROFILE_FLOW else 0.0
                    r = (model.detect_with_scores(fr)
                         if msg.get("scores") and hasattr(model, "detect_with_scores")
                         else model.detect(fr))
                    sub = ({**model.last_sub, "shm": round(shm, 2)} if PROFILE_FLOW else None)
                    _rep(msg, r, sub=sub)
            elif kind == "face":
                crops = msg["crops"]
                if msg.get("meta") and hasattr(model, "embed_with_meta"):
                    _rep(msg, model.embed_with_meta(crops) if crops else ([], []))
                else:
                    _rep(msg, model.embed(crops) if crops else [])
            elif kind == "gait":
                frame = rx.get(msg["frame"])
                tracks = [_Track(lid, bb) for lid, bb in msg["tracks"]]
                # older OnlineGaitEmbedder builds on the box take no frame_idx
                try:
                    out = model.embed_tracks(frame, tracks, msg["camera"], msg.get("frame_idx"))
                except TypeError:
                    out = model.embed_tracks(frame, tracks, msg["camera"])
                _rep(msg, out)
        except Exception as e:
            for m in batch:
                _reply(m, None, err=f"{kind}: {e}")

    rx.close()


# ---------------------------------------------------------------- client side
class _Client:
    def __init__(self, sid, req_q, resp_q):
        self.sid, self.req_q, self.resp_q = sid, req_q, resp_q
        self._rid = 0
        # last-call flow split (ms): queue-wait, device-compute, full round-trip.
        self.last_wait = self.last_compute = self.last_rt = 0.0
        self.last_sub: dict = {}   # det inside-server breakdown {shm,pre,infer,post}

    def _call(self, **kw):
        self._rid += 1
        msg = {"t": "q", "sid": self.sid, "rid": self._rid, "resp": self.resp_q, **kw}
        if PROFILE_FLOW:
            t0 = time.time()
            msg["_t0"] = t0
        self.req_q.put(msg)
        r = self.resp_q.get(timeout=REQ_TIMEOUT)   # blocking: our frame buffer stays valid
        if PROFILE_FLOW:
            self.last_rt = (time.time() - t0) * 1000.0
            self.last_wait = float(r.get("_tw", 0.0)) * 1000.0
            self.last_compute = float(r.get("_tc", 0.0)) * 1000.0
            self.last_sub = r.get("_sub", {}) or {}
        if r.get("err"):
            raise RuntimeError(r["err"])
        return r.get("r")

    def close(self):
        pass


class DetClient(_Client):
    def __init__(self, sid, req_q, resp_q, tx: FrameTx):
        super().__init__(sid, req_q, resp_q)
        self.tx = tx
        self.last_confidences: list[float] = []

    def detect(self, frame):
        try:
            r = self._call(frame=self.tx.put(frame), scores=True)
        except RuntimeError as e:
            msg = str(e)
            if "No such file or directory" not in msg or "psm_" not in msg:
                raise
            self.tx.reset()
            r = self._call(frame=self.tx.put(frame), scores=True)
        if isinstance(r, tuple) and len(r) == 2:
            boxes, scores = r
            self.last_confidences = list(scores)
            return boxes
        self.last_confidences = [1.0] * len(r)
        return r


class EmbedClient(_Client):
    def embed(self, crops):
        return self._call(crops=list(crops)) if crops else []


class FaceClient(_Client):
    def embed(self, crops):
        return self._call(crops=list(crops)) if crops else []

    def embed_with_meta(self, crops):
        return self._call(crops=list(crops), meta=True) if crops else ([], [])


class GaitClient(_Client):
    def __init__(self, sid, req_q, resp_q, tx: FrameTx):
        super().__init__(sid, req_q, resp_q)
        self.tx = tx

    def embed_tracks(self, frame, tracks, camera, frame_idx=None):
        if not tracks:
            return []
        return self._call(frame=self.tx.put(frame), camera=camera, frame_idx=frame_idx,
                          tracks=[(int(t.local_id), np.asarray(t.bbox, np.float32)) for t in tracks])
