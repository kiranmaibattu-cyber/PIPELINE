"""OpenVINO inference backends for the MTMC pipeline (Intel NPU + iGPU).

Drop-in replacements for the 4 heavy torch/CUDA models, honoring the exact
interfaces the pipeline expects:
  - OVDetector       : .detect(frame)->list[xyxy np.ndarray] + .last_confidences   (yolo11s, iGPU)
  - OVReidEmbedder   : .embed(list[bgr])->(N,D) L2-normalized                       (transreid_ssl, NPU)
The face (AdaFace) and gait (GaitBase) neural steps are swapped to OV inside
face_embedder.py / gait_embedder.py via the same OVCore below.

Devices are PINNED (no CPU fallback): compile_model raises if the target
device can't run the model, by design.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# split detect() into pre(CPU letterbox/normalize)/infer(GPU)/post(CPU NMS) when on
_PROF = bool(int(os.environ.get("PROFILE_FLOW", "0")))
# async detection: pipeline a batch of frames through an AsyncInferQueue so one
# frame's CPU letterbox overlaps the previous frame's GPU infer (fills the GPU
# bubble a synchronous detect() leaves). Off by default -> sequential, unchanged.
_DET_ASYNC = bool(int(os.environ.get("DET_ASYNC", "0")))

try:
    import openvino as ov
except Exception as exc:  # pragma: no cover
    ov = None
    _OV_IMPORT_ERR = exc


def l2_rows(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.maximum(n, 1e-12)


class OVCore:
    """Shared ov.Core + compiled-model cache. Pins device, no fallback."""

    _core = None

    @classmethod
    def core(cls):
        if ov is None:
            raise RuntimeError(f"openvino import failed: {_OV_IMPORT_ERR}")
        if cls._core is None:
            cls._core = ov.Core()
        return cls._core

    @classmethod
    def _check_device(cls, device: str, what: str):
        core = cls.core()
        want = device.split(".")[0]
        if want not in core.available_devices:
            raise RuntimeError(
                f"device {device!r} not available for {what} "
                f"(have {core.available_devices}); refusing CPU fallback"
            )
        return core

    @classmethod
    def compile(cls, xml_path: str | Path, device: str):
        core = cls._check_device(device, Path(xml_path).name)
        return core.compile_model(core.read_model(str(xml_path)), device)

    @classmethod
    def read(cls, xml_path: str | Path):
        return cls.core().read_model(str(xml_path))

    @classmethod
    def compile_model(cls, model, device: str, what: str = "model"):
        """Compile an already-read/reshaped ov.Model, same no-CPU-fallback guard."""
        core = cls._check_device(device, what)
        return core.compile_model(model, device)


class OVReidEmbedder:
    """transreid-family appearance embedder on OpenVINO.

    Replicates MTMC.new_models._TransreidFamilyEmbedder preprocessing:
      BGR->RGB, Resize(H,W from IR), /255, (x-0.5)/0.5, NCHW.
    Returns (N, D) L2-normalized. Wrap in adapters.TTAEmbedder for flip-TTA
    (same as the torch path).
    """

    def __init__(self, xml_path: str | Path, device: str = "NPU", key: str = "transreid_ssl"):
        self.compiled = OVCore.compile(xml_path, device)
        self.key = key
        self.backend = f"openvino:{key}:{device}"
        ishape = list(self.compiled.input(0).shape)  # [1,3,H,W]
        self._h, self._w = int(ishape[2]), int(ishape[3])
        self._out = self.compiled.output(0)
        # Async infer queue: single-crop sync calls leave the NPU submission-starved
        # (idle between host round-trips). Pipelining N in-flight requests keeps it
        # fed and overlaps CPU preprocessing with NPU compute. Output is IDENTICAL
        # (same crops, same order via userdata) — pure speedup, ~1.4x on this NPU.
        self._njobs = max(1, int(os.environ.get("OV_INFER_JOBS", "4")))
        try:
            self._q = ov.AsyncInferQueue(self.compiled, self._njobs) if self._njobs > 1 else None
        except Exception:
            self._q = None

    def _prep(self, crop: np.ndarray) -> np.ndarray:
        # MUST match the torch path EXACTLY: torchvision ToPILImage->Resize uses
        # PIL bilinear, NOT cv2.resize. cv2 shifts every embedding ~0.001-0.01,
        # which flips borderline re-id matches on this collapsed feature space.
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        rgb = np.asarray(Image.fromarray(rgb).resize((self._w, self._h), Image.BILINEAR))
        rgb = rgb.astype(np.float32) / 255.0
        rgb = (rgb - 0.5) / 0.5
        return rgb.transpose(2, 0, 1)[None]  # (1,3,H,W)

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        if not crops:
            return np.empty((0, 1), dtype=np.float32)
        if self._q is None:
            feats = [np.asarray(self.compiled(self._prep(c))[self._out]).reshape(-1) for c in crops]
            return l2_rows(np.stack(feats).astype(np.float32))
        feats: list = [None] * len(crops)

        def _cb(request, idx):
            feats[idx] = request.get_output_tensor(0).data.reshape(-1).copy()

        self._q.set_callback(_cb)
        for i, c in enumerate(crops):
            self._q.start_async(self._prep(c), i)
        self._q.wait_all()
        return l2_rows(np.stack(feats).astype(np.float32))


class OVDetector:
    """yolo11s object detector on OpenVINO (iGPU), matching MultiClassDetector.

    detect(frame) -> list[np.ndarray(4,) xyxy float] for kept class ids, and
    sets .last_confidences (parallel list[float]). Anchor-free YOLO head:
    output (1, 4+nc, 8400) = [cx,cy,w,h, class scores...] in 640-letterbox space.
    """

    def __init__(self, xml_path: str | Path, conf: float, iou: float,
                 class_ids: set[int], device: str = "GPU", dyn_batch: bool = False):
        self.conf = float(conf)
        self.iou = float(iou)
        self.class_ids = set(class_ids)
        self.last_confidences: list[float] = []
        self.last_sub: dict = {}   # {pre, infer, post} ms, when PROFILE_FLOW
        self.backend = f"openvino:yolo11s:{device}"
        # dyn_batch: reshape the input's batch dim to dynamic so detect_batch() can
        # stack N frames into ONE iGPU dispatch. If the plugin rejects a dynamic
        # batch (some INT8 GPU builds do), fall back to the fixed batch=1 model and
        # detect_batch() loops per frame -- still GPU, no CPU fallback, just no fusion.
        self._batched = False
        model = OVCore.read(xml_path)
        if dyn_batch:
            try:
                inp = model.input(0)
                ps = inp.get_partial_shape()
                ps[0] = -1
                model.reshape({inp: ps})
                self.compiled = OVCore.compile_model(model, device, Path(xml_path).name)
                self._batched = True
            except Exception as e:
                print(f"[det] dynamic-batch reshape failed ({e}); per-frame on {device}", flush=True)
                self.compiled = OVCore.compile(xml_path, device)
        else:
            self.compiled = OVCore.compile(xml_path, device)
        self._in = self.compiled.input(0)
        # batch dim may be dynamic (-> .shape throws); spatial dims are static.
        ps = self._in.get_partial_shape()
        self._sz = int(ps[2].get_length())
        self._out = self.compiled.output(0)
        # UNIFIED yolo11s-SEG support: a seg model has 2 outputs (det + mask proto)
        # and its det tensor is (4 + nc + 32) -- the trailing 32 are MASK COEFFICIENTS,
        # not class scores. Detect that and slice only the nc class columns so the same
        # model serves detection here (and gait uses its mask proto elsewhere).
        try:
            och = int(self.compiled.output(0).get_partial_shape()[1].get_length())
        except Exception:
            och = int(list(self.compiled.output(0).shape)[1])
        self._is_seg = len(self.compiled.outputs) > 1
        self._nc = och - 4 - (32 if self._is_seg else 0)
        # A dynamic-batch RESHAPE+COMPILE can still succeed while batched INFERENCE
        # throws -- some exported graphs bake batch=1 into an internal reshape node.
        # Probe with a real 2-sample infer; if it fails, drop to the per-frame path.
        if self._batched:
            try:
                probe = np.zeros((2, 3, self._sz, self._sz), np.float32)
                self.compiled(probe)
            except Exception as e:
                print(f"[det] batched infer unsupported by this graph ({str(e)[:80]}); "
                      f"per-frame on {device}", flush=True)
                self.compiled = OVCore.compile(xml_path, device)
                self._in = self.compiled.input(0)
                self._out = self.compiled.output(0)
                self._batched = False
        # async infer queue for pipelined multi-frame detection (see _DET_ASYNC)
        self._q_async = None
        if _DET_ASYNC:
            try:
                njobs = max(2, int(os.environ.get("DET_INFER_JOBS", "4")))
                self._q_async = ov.AsyncInferQueue(self.compiled, njobs)
                print(f"[det] async infer queue on ({njobs} jobs)", flush=True)
            except Exception as e:
                print(f"[det] async queue unavailable ({e}); sequential", flush=True)

    def _letterbox(self, img: np.ndarray):
        h, w = img.shape[:2]
        r = min(self._sz / h, self._sz / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        canvas = np.full((self._sz, self._sz, 3), 114, np.uint8)
        top, left = (self._sz - nh) // 2, (self._sz - nw) // 2
        canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh))
        return canvas, r, left, top

    def _blob(self, frame: np.ndarray):
        canvas, r, padx, pady = self._letterbox(frame)
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        return blob.transpose(2, 0, 1), (r, padx, pady)

    def _decode(self, out2d: np.ndarray, geom) -> tuple[list[np.ndarray], list[float]]:
        """out2d: (4+nc, 8400) for ONE image; geom: (r,padx,pady). -> (boxes, confs)."""
        r, padx, pady = geom
        out = out2d.T  # (8400, 4+nc[+32 mask coefs for seg])
        boxes_xywh = out[:, :4]
        scores_all = out[:, 4:4 + self._nc]   # class cols only (seg: skip 32 mask coefs)
        cls = scores_all.argmax(1)
        conf = scores_all.max(1)
        keep_mask = conf >= self.conf
        keep_mask &= np.isin(cls, list(self.class_ids))
        if not keep_mask.any():
            return [], []
        bx = boxes_xywh[keep_mask]
        cf = conf[keep_mask]
        # cx,cy,w,h (letterbox px) -> xyxy in original frame
        cx, cy, w, h = bx[:, 0], bx[:, 1], bx[:, 2], bx[:, 3]
        x1 = (cx - w / 2 - padx) / r
        y1 = (cy - h / 2 - pady) / r
        x2 = (cx + w / 2 - padx) / r
        y2 = (cy + h / 2 - pady) / r
        rects = [[float(x1[i]), float(y1[i]), float(x2[i] - x1[i]), float(y2[i] - y1[i])]
                 for i in range(len(cf))]
        idxs = cv2.dnn.NMSBoxes(rects, cf.tolist(), self.conf, self.iou)
        boxes: list[np.ndarray] = []
        confs: list[float] = []
        if len(idxs) == 0:
            return boxes, confs
        for i in np.array(idxs).reshape(-1):
            boxes.append(np.array([x1[i], y1[i], x2[i], y2[i]], dtype=np.float32))
            confs.append(float(cf[i]))
        return boxes, confs

    def detect(self, frame: np.ndarray) -> list[np.ndarray]:
        if _PROF:
            t0 = time.perf_counter(); blob, geom = self._blob(frame)
            t1 = time.perf_counter(); out = np.asarray(self.compiled(blob[None])[self._out])[0]
            t2 = time.perf_counter(); boxes, confs = self._decode(out, geom)
            t3 = time.perf_counter()
            self.last_sub = {"pre": (t1 - t0) * 1000, "infer": (t2 - t1) * 1000, "post": (t3 - t2) * 1000}
            self.last_confidences = confs
            return boxes
        blob, geom = self._blob(frame)
        out = np.asarray(self.compiled(blob[None])[self._out])[0]  # (4+nc, 8400)
        boxes, confs = self._decode(out, geom)
        self.last_confidences = confs
        return boxes

    def detect_with_scores(self, frame: np.ndarray) -> tuple[list[np.ndarray], list[float]]:
        boxes = self.detect(frame)
        return boxes, list(self.last_confidences)

    def detect_batch(self, frames: list[np.ndarray]) -> list[list[np.ndarray]]:
        """Detect N frames in ONE iGPU dispatch (dyn_batch), else loop per frame.
        Returns a per-frame list of box lists. Confidences are not tracked in the
        batched path (pooled workers discard them)."""
        if not frames:
            return []
        if self._q_async is not None and len(frames) > 1:
            return self._detect_batch_async(frames)
        if not self._batched or len(frames) == 1:
            return [self.detect(f) for f in frames]
        blobs, geoms = [], []
        for f in frames:
            b, g = self._blob(f)
            blobs.append(b); geoms.append(g)
        batch = np.stack(blobs, 0)                      # (N,3,sz,sz)
        out = np.asarray(self.compiled(batch)[self._out])  # (N,4+nc,8400)
        return [self._decode(out[i], geoms[i])[0] for i in range(len(frames))]

    def detect_batch_with_scores(self, frames: list[np.ndarray]) -> list[tuple[list[np.ndarray], list[float]]]:
        """Batch detector preserving per-box confidence scores."""
        if not frames:
            return []
        if self._q_async is not None and len(frames) > 1:
            return self._detect_batch_async_with_scores(frames)
        if not self._batched or len(frames) == 1:
            return [self.detect_with_scores(f) for f in frames]
        blobs, geoms = [], []
        for f in frames:
            b, g = self._blob(f)
            blobs.append(b)
            geoms.append(g)
        batch = np.stack(blobs, 0)
        out = np.asarray(self.compiled(batch)[self._out])
        return [self._decode(out[i], geoms[i]) for i in range(len(frames))]

    def _detect_batch_async(self, frames: list[np.ndarray]) -> list[list[np.ndarray]]:
        """Pipeline N frames through the async queue: frame i+1's CPU letterbox runs
        while frame i infers on the GPU, so the GPU never waits on preprocessing."""
        outs: list = [None] * len(frames)
        geoms: list = [None] * len(frames)

        def _cb(request, idx):
            outs[idx] = request.get_output_tensor(0).data.copy()

        self._q_async.set_callback(_cb)
        for i, f in enumerate(frames):
            blob, g = self._blob(f)     # CPU letterbox; overlaps prior GPU infers
            geoms[i] = g
            self._q_async.start_async(blob[None], i)
        self._q_async.wait_all()
        return [self._decode(np.asarray(outs[i])[0], geoms[i])[0]
                if outs[i] is not None else [] for i in range(len(frames))]

    def _detect_batch_async_with_scores(self, frames: list[np.ndarray]) -> list[tuple[list[np.ndarray], list[float]]]:
        outs: list = [None] * len(frames)
        geoms: list = [None] * len(frames)

        def _cb(request, idx):
            outs[idx] = request.get_output_tensor(0).data.copy()

        self._q_async.set_callback(_cb)
        for i, f in enumerate(frames):
            blob, g = self._blob(f)
            geoms[i] = g
            self._q_async.start_async(blob[None], i)
        self._q_async.wait_all()
        return [self._decode(np.asarray(outs[i])[0], geoms[i])
                if outs[i] is not None else ([], []) for i in range(len(frames))]
