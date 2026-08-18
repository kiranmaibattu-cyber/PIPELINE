"""Online GaitBase embeddings for MTMC fusion.

The embedder keeps a short silhouette buffer per camera/local-track. It returns
None until a track has enough normalized silhouettes for GaitBase inference.
"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from MTMC.gait_screen import _build_gaitbase, _embed_sequence, opengait_pretreat

_ROOT = Path(__file__).resolve().parent.parent


class OnlineGaitEmbedder:
    def __init__(
        self,
        min_len: int = 20,
        max_len: int = 60,
        trace_file: str | None = None,
        infer_every_n_frames: int = 1,
        max_embeds_per_frame: int = 0,
        backend: str = "torch",
        ov_xml: str | None = None,
        ov_device: str = "NPU",
        ov_seg_xml: str | None = None,
        ov_seg_device: str = "GPU",
    ) -> None:
        self.trace_file = Path(trace_file) if trace_file else None
        if self.trace_file:
            self.trace_file.parent.mkdir(parents=True, exist_ok=True)
            self.trace_file.write_text("", encoding="utf-8")
        self.backend_kind = backend
        # silhouette source for GaitBase. "bgsub" = per-camera background subtraction (MOG2)
        # -- the native gait-dataset silhouette, cheap on CPU, and fixed cameras suit it. It
        # crops the foreground to each track's DETECTION box (we already have it), so it
        # needs no instance-seg model. "seg" = the old yolov8n-seg (heavy full-frame mask
        # decode -- the CPU hog). Default bgsub.
        self.gait_sil = os.environ.get("GAIT_SILHOUETTE", "bgsub").lower()
        self._bg: dict[str, Any] = {}          # camera -> MOG2 background model
        self.seg = None
        self.seg_ov = None
        self.seg_device = "bgsub"
        if backend == "openvino":
            from MTMC.ov_backends import OVCore
            # GaitBase embedding on OV
            if not ov_xml:
                ov_xml = str(_ROOT / "models" / "gaitbase_int8.xml")
            self.ov = OVCore.compile(ov_xml, ov_device)
            self.ov_out = self.ov.output(0)
            self.ov_T = int(list(self.ov.input(0).shape)[1])  # fixed sequence length
            self.model, self.device = None, f"openvino:{ov_device}"
            if self.gait_sil == "seg":
                # yolov8n-seg segmentation on OV (iGPU) — no torch/torchvision
                if not ov_seg_xml:
                    ov_seg_xml = str(_ROOT / "models" / "yolov8n_seg.xml")
                self.seg_ov = OVCore.compile(ov_seg_xml, ov_seg_device)
                self.seg_ov_outs = [self.seg_ov.output(0), self.seg_ov.output(1)]
                self.seg_sz = int(list(self.seg_ov.input(0).shape)[2])  # 640
                self.seg_device = f"openvino:{ov_seg_device}"
        else:
            self.model, self.device = _build_gaitbase()
            if self.gait_sil == "seg":
                import torch
                from ultralytics import YOLO
                self.seg = YOLO("yolov8n-seg.pt")
                self.seg_device = 0 if torch.cuda.is_available() else "cpu"
                try:
                    self.seg.to("cuda" if torch.cuda.is_available() else "cpu")
                except Exception:
                    pass
        self.min_len = min_len
        self.max_len = max_len
        self.infer_every_n_frames = max(1, int(infer_every_n_frames))
        self.max_embeds_per_frame = max(0, int(max_embeds_per_frame))
        self.buffers: dict[tuple[str, int], list[np.ndarray]] = defaultdict(list)
        self.cache: dict[tuple[str, int], np.ndarray] = {}
        self.last_infer_frame: dict[tuple[str, int], int] = {}
        self.backend = f"gaitbase_gait3d:{self.device}:seg={self.seg_device}"

    def _trace(self, msg: str) -> None:
        if not self.trace_file:
            return
        with self.trace_file.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.flush()

    @staticmethod
    def _iou(a: np.ndarray, b: np.ndarray) -> float:
        x1 = max(float(a[0]), float(b[0]))
        y1 = max(float(a[1]), float(b[1]))
        x2 = min(float(a[2]), float(b[2]))
        y2 = min(float(a[3]), float(b[3]))
        inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        area_a = max(0.0, float(a[2] - a[0])) * max(0.0, float(a[3] - a[1]))
        area_b = max(0.0, float(b[2] - b[0])) * max(0.0, float(b[3] - b[1]))
        denom = area_a + area_b - inter
        return inter / denom if denom > 0 else 0.0

    def _seg_infer(self, frame: np.ndarray):
        """Return (boxes: list[xyxy np.ndarray], masks: np.ndarray[N,mh,mw] float in
        original-frame proportions). torch path uses ultralytics; OV path decodes
        yolov8-seg proto masks. Either -> None,None when nothing found."""
        if self.backend_kind != "openvino":
            res = self.seg.predict(frame, classes=[0], conf=0.4, verbose=False,
                                   device=self.seg_device)[0]
            if res.masks is None or res.boxes is None:
                return None, None
            boxes = [b.xyxy[0].cpu().numpy() for b in res.boxes]
            return boxes, res.masks.data.cpu().numpy()

        sz = self.seg_sz
        h0, w0 = frame.shape[:2]
        r = min(sz / h0, sz / w0)
        nh, nw = int(round(h0 * r)), int(round(w0 * r))
        top, left = (sz - nh) // 2, (sz - nw) // 2
        canvas = np.full((sz, sz, 3), 114, np.uint8)
        canvas[top:top + nh, left:left + nw] = cv2.resize(frame, (nw, nh))
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = blob.transpose(2, 0, 1)[None]
        res = self.seg_ov(blob)
        det = np.asarray(res[self.seg_ov_outs[0]])[0].T   # (8400, 116)
        proto = np.asarray(res[self.seg_ov_outs[1]])[0]   # (32, 160, 160)
        cls0 = det[:, 4]                                   # person score
        keep = cls0 >= 0.4
        if not keep.any():
            return None, None
        box, conf, coef = det[keep, :4], cls0[keep], det[keep, 84:]
        cx, cy, w, h = box[:, 0], box[:, 1], box[:, 2], box[:, 3]
        rects = [[float(cx[i] - w[i] / 2), float(cy[i] - h[i] / 2),
                  float(w[i]), float(h[i])] for i in range(len(conf))]
        idxs = cv2.dnn.NMSBoxes(rects, conf.tolist(), 0.4, 0.5)
        if len(idxs) == 0:
            return None, None
        ph, pw = proto.shape[1], proto.shape[2]
        proto_flat = proto.reshape(proto.shape[0], -1)     # (32, 25600)
        boxes_out, masks_out = [], []
        for i in np.array(idxs).reshape(-1):
            m = 1.0 / (1.0 + np.exp(-(coef[i] @ proto_flat)))
            m = cv2.resize(m.reshape(ph, pw), (sz, sz))     # -> 640 letterbox
            masks_out.append(m[top:top + nh, left:left + nw])  # crop to content (nh,nw)
            bx1 = (cx[i] - w[i] / 2 - left) / r
            by1 = (cy[i] - h[i] / 2 - top) / r
            bx2 = (cx[i] + w[i] / 2 - left) / r
            by2 = (cy[i] + h[i] / 2 - top) / r
            boxes_out.append(np.array([bx1, by1, bx2, by2], dtype=np.float32))
        return boxes_out, np.stack(masks_out)

    def _ov_embed(self, seq: np.ndarray) -> np.ndarray:
        """seq (L,64,44) uint8 -> fixed-T OV GaitBase inference -> (C,P) array."""
        T = self.ov_T
        L = seq.shape[0]
        if L >= T:
            s = seq[:T]
        else:
            idx = np.linspace(0, L - 1, T).round().astype(int)
            s = seq[idx]
        x = (s.astype(np.float32) / 255.0)[None]  # (1,T,64,44)
        return np.asarray(self.ov(x)[self.ov_out])

    def _bgsub_mask(self, frame: np.ndarray, camera: str) -> np.ndarray:
        """Per-camera MOG2 foreground mask (0/255). Fixed CCTV -> a stable background is
        learned over the run; the person is the foreground. Cheap CPU, no seg model."""
        bg = self._bg.get(camera)
        if bg is None:
            bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25,
                                                    detectShadows=False)
            self._bg[camera] = bg
        fg = bg.apply(frame)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k, iterations=2)
        return fg

    def _silhouettes(self, frame, tracks, camera):
        """One GaitBase silhouette per track (or None). bgsub crops the foreground to each
        track's detection box; seg matches yolov8-seg masks to the boxes (legacy)."""
        fh, fw = frame.shape[:2]
        if self.gait_sil == "bgsub":
            fg = self._bgsub_mask(frame, camera)
            res = []
            for tr in tracks:
                x1, y1, x2, y2 = [int(v) for v in tr.bbox]
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(fw, x2), min(fh, y2)
                if x2 <= x1 or y2 <= y1:
                    res.append(None); continue
                res.append(opengait_pretreat(fg[y1:y2, x1:x2]))
            return res
        boxes, masks = self._seg_infer(frame)
        if boxes is None or masks is None or len(boxes) == 0:
            return [None] * len(tracks)
        res = []
        for tr in tracks:
            best_i, best_iou = -1, 0.0
            for i, box in enumerate(boxes):
                iou = self._iou(np.asarray(box), tr.bbox)
                if iou > best_iou:
                    best_i, best_iou = i, iou
            if best_i < 0 or best_iou < 0.5:
                res.append(None); continue
            mask = cv2.resize(masks[best_i], (fw, fh), interpolation=cv2.INTER_NEAREST)
            x1, y1, x2, y2 = tr.bbox.astype(int)
            sub = (mask[max(0, y1):y2, max(0, x1):x2] > 0.5).astype(np.uint8) * 255
            res.append(opengait_pretreat(sub))
        return res

    def embed_tracks(
        self,
        frame: np.ndarray,
        tracks: list[Any],
        camera: str,
        frame_idx: int | None = None,
    ) -> list[np.ndarray | None]:
        if not tracks:
            return []
        tag = f"frame={frame_idx} camera={camera} tracks={len(tracks)}"
        self._trace(f"{tag} sil_start src={self.gait_sil}")
        sils = self._silhouettes(frame, tracks, camera)
        self._trace(f"{tag} sil_done n={sum(1 for s in sils if s is not None)}")
        out: list[np.ndarray | None] = []
        embeds_this_frame = 0

        for tr, sil in zip(tracks, sils):
            key = (camera, int(tr.local_id))
            if sil is None:
                out.append(None)
                continue

            buf = self.buffers[key]
            buf.append(sil)
            if len(buf) > self.max_len:
                del buf[0]
            if len(buf) < self.min_len:
                out.append(None)
                continue
            cached = self.cache.get(key)
            last_frame = self.last_infer_frame.get(key, -10**9)
            due = frame_idx is None or (frame_idx - last_frame) >= self.infer_every_n_frames
            under_budget = not self.max_embeds_per_frame or embeds_this_frame < self.max_embeds_per_frame
            if cached is not None and (not due or not under_budget):
                out.append(cached.copy())
                continue
            self._trace(f"{tag} embed_start local_id={int(tr.local_id)} buf={len(buf)}")
            if self.backend_kind == "openvino":
                feat = self._ov_embed(np.stack(buf))
            else:
                feat = _embed_sequence(self.model, self.device, np.stack(buf))
            self._trace(f"{tag} embed_done local_id={int(tr.local_id)}")
            flat = feat.reshape(-1)
            n = np.linalg.norm(flat)
            emb = flat / n if n > 1e-12 else flat
            self.cache[key] = emb.copy()
            if frame_idx is not None:
                self.last_infer_frame[key] = int(frame_idx)
            embeds_this_frame += 1
            out.append(emb)
        return out
