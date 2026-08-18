"""TrackObservation -- the contract between the backbone and the platform.

The shared backbone (decode -> detect -> track) emits, per processed frame per
camera, one TrackObservation per tracked person. It is the ONLY thing plugins and
the Person-State service consume, so a plugin never touches raw video. Heavy
features (appearance / face / gait embeddings, crop) are OPTIONAL and attached only
when event-triggered extraction ran for that observation -- "track first, then
analyze": most observations carry just a box + foot-point (enough for zones /
counting / loitering); identity-heavy fields appear only on keyframes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import numpy as np
except Exception:  # numpy optional at import time
    np = None


@dataclass
class TrackObservation:
    camera: str
    local_id: int                    # per-camera tracker id (local to this camera)
    bbox: tuple                      # (x1, y1, x2, y2) in original frame pixels
    t: float                         # timestamp (shared video/wall clock, seconds)
    frame_idx: int = 0
    quality: float = 1.0             # detection/crop quality [0,1]

    # foot-point (bottom-centre of bbox): the ground contact used by zones + BEV.
    foot_point: Optional[tuple] = None

    # OPTIONAL event-triggered features (present only when extracted this frame):
    app_emb: Any = None              # appearance embedding (np.ndarray) — keyframe
    face_emb: Any = None             # face embedding — only when a face is visible
    gait_emb: Any = None             # gait embedding — only on a good motion sequence
    color: Any = None                # torso colour histogram — the cross-cam colour gate
    crop_jpeg: Optional[bytes] = None

    # authoritative global id from the BACKBONE's own re-id (local2global). When
    # present the identity plugin INGESTS it (single identity engine) instead of
    # matching a second gallery. None on legacy dumps / synthetic test streams.
    gid: Optional[int] = None

    # filled by the identity (re-id) plugin once resolved:
    person_id: Optional[int] = None

    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.foot_point is None:
            x1, y1, x2, y2 = self.bbox
            self.foot_point = ((x1 + x2) / 2.0, float(y2))

    @property
    def height_px(self) -> float:
        return float(self.bbox[3] - self.bbox[1])

    @staticmethod
    def _has(v) -> bool:
        if v is None:
            return False
        if np is not None and isinstance(v, np.ndarray):
            return v.size > 0 and float(np.linalg.norm(v)) > 1e-6
        return True

    def has_app(self) -> bool:
        return self._has(self.app_emb)

    def has_face(self) -> bool:
        return self._has(self.face_emb)

    def has_gait(self) -> bool:
        return self._has(self.gait_emb)
