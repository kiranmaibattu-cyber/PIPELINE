"""AdaFace face embedder for fusion: detect+align inside person crops, embed.

Winner of the Stage-2 screen (AUC 0.922 on ch10 CCTV faces). Uses insightface
buffalo_s SCRFD for detection/alignment and AdaFace ir101 for the embedding.
Returns a zero vector when no face is visible (fusion treats that as
"modality missing" and renormalizes weights).
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent

FACE_DIM = 512


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _face_chip_quality(chip: np.ndarray, face_w: float, face_h: float,
                       det_score: float, min_face_px: int) -> tuple[float, dict]:
    """Quality for retaining face exemplars, not for deciding recognition.

    The embedding model still gets every detected face above min_face_px. This score
    only decides which of the bounded face exemplars survive once a gid has K faces.
    """
    if chip is None or chip.size == 0:
        meta = {"face_q": 0.0, "det_score": 0.0, "face_w": 0.0, "face_h": 0.0,
                "size": 0.0, "sharp": 0.0, "exposure": 0.0}
        return 0.0, meta
    gray = cv2.cvtColor(chip, cv2.COLOR_BGR2GRAY) if chip.ndim == 3 else chip
    sharp_raw = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharp = _clip01(sharp_raw / 1800.0)
    mean = float(gray.mean())
    exposure = _clip01(1.0 - abs(mean - 127.5) / 127.5)
    face_px = min(float(face_w), float(face_h))
    size = _clip01((face_px - float(min_face_px)) / max(1.0, 80.0 - float(min_face_px)))
    det = _clip01((float(det_score) - 0.45) / 0.50)
    q = _clip01(0.35 * det + 0.30 * size + 0.25 * sharp + 0.10 * exposure)
    meta = {"face_q": round(q, 4), "det_score": round(float(det_score), 4),
            "face_w": round(float(face_w), 1), "face_h": round(float(face_h), 1),
            "size": round(size, 4), "sharp": round(sharp, 4),
            "exposure": round(exposure, 4), "sharp_raw": round(sharp_raw, 1)}
    return q, meta


class OVScrfd:
    """SCRFD face detector on the iGPU via onnxruntime OpenVINO EP (device_type=GPU).
    Bypasses insightface (which drops provider_options -> stays on CPU). Returns
    (dets Nx5 [x1,y1,x2,y2,score], kpss Nx5x2) in the input crop's coords.
    det_500m output order: scores(s8,s16,s32), bbox(...), kps(...); 2 anchors/cell."""

    def __init__(self, onnx_path, device="GPU", thresh=0.5, nms=0.4, input_size=320):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(
            str(onnx_path),
            providers=[("OpenVINOExecutionProvider", {"device_type": device}), "CPUExecutionProvider"])
        self.in_name = self.sess.get_inputs()[0].name
        self.out_names = [o.name for o in self.sess.get_outputs()]
        self.strides = [8, 16, 32]
        self.fmc = 3
        self.na = 2
        self.thresh = thresh
        self.nms = nms
        self.S = input_size
        self._centers = {}
        self.providers = self.sess.get_providers()

    def _anchors(self, h, w, stride):
        key = (h, w, stride)
        if key not in self._centers:
            gy, gx = np.mgrid[0:h, 0:w]
            ac = np.stack([gx, gy], axis=-1).astype(np.float32).reshape(-1, 2) * stride
            if self.na > 1:
                ac = np.stack([ac] * self.na, axis=1).reshape(-1, 2)
            self._centers[key] = ac
        return self._centers[key]

    def detect(self, img_bgr):
        S = self.S
        h0, w0 = img_bgr.shape[:2]
        r = min(S / h0, S / w0)
        nh, nw = int(round(h0 * r)), int(round(w0 * r))
        canvas = np.zeros((S, S, 3), np.uint8)
        canvas[:nh, :nw] = cv2.resize(img_bgr, (nw, nh))
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128, (S, S), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.sess.run(self.out_names, {self.in_name: blob})
        sl, bl, kl = [], [], []
        for i, stride in enumerate(self.strides):
            scores = outs[i].ravel()
            bpred = outs[i + self.fmc] * stride
            kpred = outs[i + self.fmc * 2] * stride
            hh = ww = S // stride
            ac = self._anchors(hh, ww, stride)
            pos = np.where(scores >= self.thresh)[0]
            if len(pos) == 0:
                continue
            x1 = ac[:, 0] - bpred[:, 0]; y1 = ac[:, 1] - bpred[:, 1]
            x2 = ac[:, 0] + bpred[:, 2]; y2 = ac[:, 1] + bpred[:, 3]
            boxes = np.stack([x1, y1, x2, y2], axis=-1)
            kps = np.zeros((ac.shape[0], 5, 2), np.float32)
            for k in range(5):
                kps[:, k, 0] = ac[:, 0] + kpred[:, k * 2]
                kps[:, k, 1] = ac[:, 1] + kpred[:, k * 2 + 1]
            sl.append(scores[pos]); bl.append(boxes[pos] / r); kl.append(kps[pos] / r)
        if not sl:
            return np.zeros((0, 5), np.float32), np.zeros((0, 5, 2), np.float32)
        scores = np.concatenate(sl); boxes = np.vstack(bl); kpss = np.vstack(kl)
        rects = boxes.copy(); rects[:, 2:] -= rects[:, :2]
        keep = cv2.dnn.NMSBoxes(rects.tolist(), scores.tolist(), self.thresh, self.nms)
        if len(keep) == 0:
            return np.zeros((0, 5), np.float32), np.zeros((0, 5, 2), np.float32)
        keep = np.array(keep).reshape(-1)
        dets = np.hstack([boxes[keep], scores[keep, None]]).astype(np.float32)
        return dets, kpss[keep]


class AdaFaceEmbedder:
    def __init__(self, min_face_px: int = 24, backend: str = "torch",
                 ov_xml: str | None = None, ov_device: str = "NPU") -> None:
        import onnxruntime as ort
        from insightface.app import FaceAnalysis
        from insightface.utils import face_align

        self._face_align = face_align
        self.min_face_px = min_face_px
        self.backend_kind = backend

        if backend == "openvino":
            self.device = "cpu"
            # insightface SCRFD face-detect on the iGPU (EXACT insightface detection):
            # tuple provider with device_type=GPU + prepare(ctx_id>=0) so it is NOT
            # reset to CPU. face EMBED (AdaFace) on the NPU (OV).
            if "OpenVINOExecutionProvider" in ort.get_available_providers():
                providers = [("OpenVINOExecutionProvider", {"device_type": "GPU"}), "CPUExecutionProvider"]
                scrfd_dev = "iGPU"
            else:
                providers = ["CPUExecutionProvider"]
                scrfd_dev = "CPU"
            self.app = FaceAnalysis(
                name="buffalo_s", root=str(_ROOT / "models" / "face_reid"),
                providers=providers, allowed_modules=["detection"])
            self.app.prepare(ctx_id=0, det_size=(320, 320))  # ctx_id>=0 => keep GPU provider
            from MTMC.ov_backends import OVCore
            if not ov_xml:
                ov_xml = str(_ROOT / "models" / "adaface_ir101_int8.xml")
            self.ov = OVCore.compile(ov_xml, ov_device)
            self.ov_out = self.ov.output(0)
            self.backend = f"adaface:ir101:openvino:{ov_device}:scrfd@{scrfd_dev}"
        else:
            import torch
            self.torch = torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            available_providers = ort.get_available_providers()
            if self.device == "cuda" and "CUDAExecutionProvider" in available_providers:
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
                ctx_id = 0
            else:
                providers = ["CPUExecutionProvider"]
                ctx_id = -1
            self.app = FaceAnalysis(
                name="buffalo_s", root=str(_ROOT / "models" / "face_reid"),
                providers=providers, allowed_modules=["detection"])
            self.app.prepare(ctx_id=ctx_id, det_size=(320, 320))
            repo = _ROOT / "repos" / "adaface"
            if str(repo) not in sys.path:
                sys.path.insert(0, str(repo))
            import net

            ckpt_path = _ROOT / "models" / "adaface" / "adaface_ir101_webface12m.ckpt"
            self.model = net.build_model("ir_101")
            ckpt = self.torch.load(ckpt_path, map_location="cpu", weights_only=False)
            state = {k[6:]: v for k, v in ckpt["state_dict"].items() if k.startswith("model.")}
            self.model.load_state_dict(state)
            self.model.eval().to(self.device)
            self.backend = f"adaface:ir101_webface12m:{self.device}:face_det={providers[0]}"

    def embed_with_meta(self, crops: list[np.ndarray]) -> tuple[np.ndarray, list[dict]]:
        """(embeddings, metadata); zero rows and face_q=0 where no face was found."""
        if not crops:
            return np.empty((0, FACE_DIM), dtype=np.float32), []
        chips, chip_idx, metas = [], [], [
            {"face_q": 0.0, "det_score": 0.0, "face_w": 0.0, "face_h": 0.0,
             "size": 0.0, "sharp": 0.0, "exposure": 0.0}
            for _ in crops
        ]
        for i, crop in enumerate(crops):
            if crop.shape[0] < 60:
                continue
            # insightface SCRFD (iGPU for openvino backend, CPU/CUDA for torch) — identical detection path.
            # Feed BGR: insightface's detector expects it (SCRFD's own blobFromImage does
            # swapRB=True internally) and AdaFace was trained on BGR chips, so a cv2 frame
            # flows through untouched. Converting to RGB here double-swapped the channels
            # for detection, degrading boxes/keypoints/det_score -- and the keypoints are
            # what norm_crop aligns the chip with. Matches FACE/face_core.py, which built
            # the reference gallery.
            faces = self.app.get(crop)
            if not faces:
                continue
            best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
            face_w = float(best.bbox[2] - best.bbox[0])
            face_h = float(best.bbox[3] - best.bbox[1])
            if face_w < self.min_face_px:
                continue
            chip = self._face_align.norm_crop(crop, best.kps)
            det_score = float(getattr(best, "det_score", getattr(best, "score", 0.5)))
            _, metas[i] = _face_chip_quality(chip, face_w, face_h, det_score, self.min_face_px)
            chips.append(chip)
            chip_idx.append(i)

        out = np.zeros((len(crops), FACE_DIM), dtype=np.float32)
        if chips:
            batch = np.stack([((c.astype(np.float32) / 255.0) - 0.5) / 0.5 for c in chips])
            batch = np.ascontiguousarray(batch.transpose(0, 3, 1, 2)).astype(np.float32)
            if self.backend_kind == "openvino":
                feats = np.stack([
                    np.asarray(self.ov(batch[k:k + 1])[self.ov_out]).reshape(-1)
                    for k in range(batch.shape[0])
                ])
            else:
                tensor = self.torch.from_numpy(batch).to(self.device)
                with self.torch.no_grad():
                    feats, _ = self.model(tensor)
                feats = feats.cpu().numpy()
            for j, i in enumerate(chip_idx):
                out[i] = _l2(feats[j])
        return out, metas

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        """(N, 512); zero rows where no face was found."""
        out, _ = self.embed_with_meta(crops)
        return out
