from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class Detection:
    bbox: List[int]
    class_id: int
    class_name: str
    confidence: float
    model_name: str
    parent_id: Optional[int] = None
    draw: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def shifted(self, dx: int, dy: int, parent_id: Optional[int] = None) -> "Detection":
        x1, y1, x2, y2 = self.bbox
        return Detection(
            bbox=[x1 + dx, y1 + dy, x2 + dx, y2 + dy],
            class_id=self.class_id,
            class_name=self.class_name,
            confidence=self.confidence,
            model_name=self.model_name,
            parent_id=parent_id if parent_id is not None else self.parent_id,
            draw=self.draw,
            metadata=dict(self.metadata),
        )


@dataclass
class FramePacket:
    index: int
    name: str
    frame: np.ndarray
    detections: List[Detection] = field(default_factory=list)
    probe_counts: Dict[str, int] = field(default_factory=dict)
    analytics_events: List[Dict[str, Any]] = field(default_factory=list)
    analytics_state: Dict[str, Any] = field(default_factory=dict)

    def add_probe(self, name: str, count: int = 1) -> None:
        if count <= 0:
            return
        self.probe_counts[name] = self.probe_counts.get(name, 0) + count

    def add_event(self, event: Dict[str, Any]) -> None:
        if event:
            self.analytics_events.append(event)
