"""Detect -> align -> embed. The single face code path for ENROLLMENT.

Ported from the reference tool (D:\\RE_ID\\FACE\\face_core.py on the Windows box)
that built FACE/gallery. Enrollment and identification must not drift apart in crop
convention, colour order or input size -- a gallery built with one convention and
queried with another loses accuracy with no error anywhere.

Two deliberate differences from the reference, both forced by this box:

  embedding   The reference ran AdaFace ir101 from a torch checkpoint. Here the
              gallery (FACE/gallery_int8) was re-embedded with the INT8 OpenVINO
              model -- see FACE/gallery_int8/embedding_provenance.json -- and the
              live FacePlugin queries with it. Enrolling with torch fp32 would put
              a systematic offset between stored and queried vectors, so this uses
              the SAME INT8 model and the SAME preprocessing as
              PLATF/reembed_face_gallery_int8.py. (repos/adaface is absent here
              anyway, so the torch path could not run.)

  device      Detection prefers the iGPU via the OpenVINO EP, falling back to CPU.

Models:
  detect + 5-pt keypoints   insightface SCRFD   (buffalo_l/det_10g by default)
  head pose (deg)           insightface         1k3d68.onnx -- WITHOUT this module
                                                there is no yaw and no pose bin
  embed 512-d               AdaFace ir101 INT8  models/adaface_ir101_int8.xml

Colour order matters and is easy to get wrong in opposite directions: insightface's
detector expects BGR, and AdaFace was trained on BGR chips too. So a frame read by
cv2 flows through untouched. Do not "helpfully" convert to RGB.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
FACE_DIM = 512
CHIP = 112

# Pose bins, in degrees of yaw. The bins meet exactly at 15 and the reject boundary
# is the same 45 that ends the side bins, so no sample can fall through a gap.
# Beyond 45 deg the face is too oblique to be worth storing -- a bad wide-yaw vector
# does more damage in a gallery than no vector.
YAW_FRONTAL = 15.0
YAW_MAX = 45.0

POSE_BINS = ("frontal", "left", "right")


def pose_bin(yaw: float) -> str | None:
    """Bin by yaw in degrees. None means reject."""
    if abs(yaw) <= YAW_FRONTAL:
        return "frontal"
    if YAW_FRONTAL < yaw <= YAW_MAX:
        return "left"
    if -YAW_MAX <= yaw < -YAW_FRONTAL:
        return "right"
    return None


@dataclass
class FaceObs:
    """One detected face: everything downstream needs, nothing it does not.

    This is the object PLATF/face_enroll_gallery.py's Gallery.consider() consumes.
    """

    box: tuple[float, float, float, float]      # in source-frame pixels
    kps: np.ndarray                             # (5, 2) source-frame
    chip: np.ndarray                            # (112, 112, 3) BGR aligned
    vec: np.ndarray                             # (512,) L2-normalised
    yaw: float
    pitch: float
    roll: float
    pose: str | None                            # bin, None = rejected
    det_score: float
    face_px: float                              # box width in SOURCE pixels
    sharp: float                                # variance of Laplacian on chip
    norm: float                                 # AdaFace pre-L2 magnitude
    meta: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.pose is not None


def _l2(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def sharpness(chip: np.ndarray) -> float:
    return float(cv2.Laplacian(cv2.cvtColor(chip, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def illum_hint(chip: np.ndarray) -> str:
    """Recorded for auditing only -- never used as a storage key.

    A saturation test cannot reliably tell night IR from a grey daylight scene, so
    the gallery discovers appearance modes from the embeddings instead.
    """
    sat = cv2.cvtColor(chip, cv2.COLOR_BGR2HSV)[:, :, 1]
    return "ir?" if float(sat.mean()) < 12.0 else "rgb"


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / ua if ua > 0 else 0.0


def _dedupe(obs: list, chips: list, iou_thresh: float = 0.45):
    """Drop the same head found inside two overlapping person boxes.

    Left alone it double-counts identifications and offers enrollment the same
    observation twice, which looks like corroborating evidence but is one sample.
    Keep the highest detector score.
    """
    order = sorted(range(len(obs)), key=lambda i: -obs[i].det_score)
    keep: list[int] = []
    for i in order:
        if any(_iou(obs[i].box, obs[j].box) >= iou_thresh for j in keep):
            continue
        keep.append(i)
    keep.sort()
    return [obs[i] for i in keep], [chips[i] for i in keep]


class FaceCore:
    def __init__(self, det_size: int | None = None, min_face_px: int = 28,
                 pack: str | None = None, device: str | None = None) -> None:
        from insightface.app import FaceAnalysis
        from insightface.utils import face_align

        self._align = face_align
        self.min_face_px = int(min_face_px)
        det_size = int(det_size or os.environ.get("ENROLL_DET_SIZE", "800"))
        pack = pack or os.environ.get("ENROLL_PACK", "buffalo_l")
        device = device or os.environ.get("ENROLL_FACE_DEV", "GPU")

        # landmark_3d_68 is what supplies face.pose; without it there is no yaw and
        # therefore no pose bin. genderage / 2d106det stay off, unused.
        # Tuple providers + ctx_id>=0 keep the OpenVINO EP (insightface drops
        # provider_options and silently reverts to CPU otherwise).
        try:
            import onnxruntime as ort
            ov = "OpenVINOExecutionProvider" in ort.get_available_providers()
        except Exception:
            ov = False
        if ov:
            providers = [("OpenVINOExecutionProvider", {"device_type": device}),
                         "CPUExecutionProvider"]
            ctx_id = 0
        else:
            providers = ["CPUExecutionProvider"]
            ctx_id = -1
        self.app = FaceAnalysis(
            name=pack, root=str(ROOT / "models" / "face_reid"), providers=providers,
            allowed_modules=["detection", "landmark_3d_68"])
        self.app.prepare(ctx_id=ctx_id, det_size=(det_size, det_size))

        # SAME model + preprocessing as reembed_face_gallery_int8.py, or enrolled
        # vectors are not comparable with the gallery they are stored in.
        from MTMC.ov_backends import OVCore
        xml = os.environ.get("ADAFACE_INT8_XML", str(ROOT / "models" / "adaface_ir101_int8.xml"))
        self.ov = OVCore.compile(xml, os.environ.get("ADAFACE_DEV", device))
        self.ov_out = self.ov.output(0)
        self.backend = f"scrfd_{pack}@{'GPU' if ov else 'CPU'}+adaface_ir101_int8@{device}"

    # -- embedding -------------------------------------------------------

    def embed_chips(self, chips: list) -> tuple[np.ndarray, np.ndarray]:
        """(N, 512) L2-normalised vectors and (N,) pre-L2 magnitudes."""
        if not chips:
            return np.empty((0, FACE_DIM), np.float32), np.empty((0,), np.float32)
        vecs, norms = [], []
        for chip in chips:
            if chip.shape[:2] != (CHIP, CHIP):
                chip = cv2.resize(chip, (CHIP, CHIP), interpolation=cv2.INTER_LINEAR)
            batch = ((chip.astype(np.float32) / 255.0) - 0.5) / 0.5
            batch = np.ascontiguousarray(batch.transpose(2, 0, 1)[None])
            raw = np.asarray(self.ov(batch)[self.ov_out]).reshape(-1)
            norms.append(float(np.linalg.norm(raw)))
            vecs.append(_l2(raw))
        return np.stack(vecs).astype(np.float32), np.asarray(norms, np.float32)

    # -- full path -------------------------------------------------------

    def analyse(self, frame: np.ndarray,
                person_boxes: list | None = None,
                upscale_small: bool = True) -> list:
        """All faces in one BGR frame.

        person_boxes: when given, look for a face inside each person crop instead of
        over the whole frame. Distant faces are only a handful of pixels in a 1080p
        frame and the detector misses them; inside an upscaled person crop they
        survive. Enrollment (subject close to the camera) does not need this.
        """
        # Each entry keeps the exact image the face was detected in, because
        # alignment must run on those same pixels -- f.kps is in that image's
        # coordinates. Only the reported geometry is mapped back to the frame.
        found: list = []
        if person_boxes is None:
            found = [(f, frame, 0.0, 0.0, 1.0) for f in self.app.get(frame)]
        else:
            H, W = frame.shape[:2]
            for bx1, by1, bx2, by2 in person_boxes:
                x1, y1 = max(0, int(bx1)), max(0, int(by1))
                x2, y2 = min(W, int(bx2)), min(H, int(by2))
                if x2 - x1 < 16 or y2 - y1 < 32:
                    continue
                crop = frame[y1:y2, x1:x2]
                scale = 1.0
                if upscale_small and crop.shape[0] < 256:
                    scale = 256.0 / crop.shape[0]
                    crop = cv2.resize(crop, None, fx=scale, fy=scale,
                                      interpolation=cv2.INTER_CUBIC)
                for f in self.app.get(crop):
                    found.append((f, crop, x1, y1, scale))

        obs: list = []
        chips: list = []
        for f, img, ox, oy, scale in found:
            box = f.bbox
            # Size gate is applied in SOURCE pixels, so upscaling a person crop
            # cannot smuggle a 6-pixel face past it.
            face_px = float(box[2] - box[0]) / scale
            if face_px < self.min_face_px:
                continue
            pose = getattr(f, "pose", None)
            if pose is None:
                continue
            pitch, yaw, roll = float(pose[0]), float(pose[1]), float(pose[2])

            chip = self._align.norm_crop(img, f.kps, image_size=CHIP)

            kps_src = f.kps / scale + np.array([ox, oy], np.float32)
            box_src = (float(box[0]) / scale + ox, float(box[1]) / scale + oy,
                       float(box[2]) / scale + ox, float(box[3]) / scale + oy)
            obs.append(FaceObs(
                box=box_src, kps=kps_src, chip=chip, vec=np.zeros(FACE_DIM, np.float32),
                yaw=yaw, pitch=pitch, roll=roll, pose=pose_bin(yaw),
                det_score=float(f.det_score), face_px=face_px,
                sharp=sharpness(chip), norm=0.0,
                meta={"illum_hint": illum_hint(chip)}))
            chips.append(chip)

        if person_boxes is not None:
            obs, chips = _dedupe(obs, chips)

        if obs:
            vecs, norms = self.embed_chips(chips)
            for o, v, n in zip(obs, vecs, norms):
                o.vec = _l2(v)
                o.norm = float(n)
        return obs
