"""Event upload boundary for management-server integration."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from edge_runtime.runtime.api_tags import ManagementEnvelope, STATUS_GRAPH


@dataclass(frozen=True)
class ManagementEvent:
    edge_id: str
    revision: int
    event_type: str
    payload: dict[str, Any]


class EventUploader:
    """Uploader interface.

    This file-backed implementation keeps phase 1 independent from a management
    server. A HTTP implementation can replace it without touching graph planning.
    """

    def __init__(self, outbox: Path) -> None:
        self._outbox = outbox
        self._outbox.parent.mkdir(parents=True, exist_ok=True)

    def publish(self, event: ManagementEvent) -> None:
        solution_pack = str(event.payload.get("solution_pack") or "edge")
        envelope = ManagementEnvelope(
            edge_id=event.edge_id,
            revision=event.revision,
            solution_pack=solution_pack,
            tag=STATUS_GRAPH,
            event_type=event.event_type,
            payload=event.payload,
        )
        self.publish_envelope(envelope)

    def publish_envelope(self, envelope: ManagementEnvelope) -> None:
        with self._outbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope.as_dict(), sort_keys=True) + "\n")
