"""Management API/event tag contracts for edge runtime messages."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


API_VERSION = "edge-api/v1"

INPUT_STREAM = "input.camera.stream"
OUTPUT_EVENT = "output.analytics.event"
OUTPUT_FRAME = "output.frame.overlay"
OUTPUT_SNAPSHOT = "output.event.snapshot"
METRIC_RUNTIME = "metric.runtime"
METRIC_CAMERA = "metric.camera"
HEALTH_RUNTIME = "health.runtime"
STATUS_GRAPH = "status.graph"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ApiTag:
    """Stable management routing label for one edge-side input/output surface."""

    api_version: str
    tag: str
    edge_id: str
    revision: int
    solution_pack: str
    direction: str
    payload_type: str
    camera_id: str | None = None
    app_id: str | None = None
    service: str | None = None
    device: str | None = None
    topic: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "api_version": self.api_version,
            "tag": self.tag,
            "edge_id": self.edge_id,
            "revision": self.revision,
            "solution_pack": self.solution_pack,
            "direction": self.direction,
            "payload_type": self.payload_type,
        }
        for key in ("camera_id", "app_id", "service", "device", "topic"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


@dataclass(frozen=True)
class ManagementEnvelope:
    """Message envelope sent to management or written to the phase-1 outbox."""

    edge_id: str
    revision: int
    solution_pack: str
    tag: str
    event_type: str
    payload: dict[str, Any]
    camera_id: str | None = None
    app_id: str | None = None
    service: str | None = None
    payload_type: str = "application/json"
    api_version: str = API_VERSION
    timestamp_utc: str = field(default_factory=_now_utc)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "api_version": self.api_version,
            "edge_id": self.edge_id,
            "revision": self.revision,
            "solution_pack": self.solution_pack,
            "tag": self.tag,
            "event_type": self.event_type,
            "payload_type": self.payload_type,
            "timestamp_utc": self.timestamp_utc,
            "payload": self.payload,
        }
        for key in ("camera_id", "app_id", "service"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data


class ApiTagBuilder:
    """Builds management-facing API tags from graph entities."""

    def camera_tags(
        self,
        edge_id: str,
        revision: int,
        solution_pack: str,
        camera_id: str,
        apps: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "input": self._tag(
                edge_id,
                revision,
                solution_pack,
                INPUT_STREAM,
                "input",
                "rtsp",
                camera_id=camera_id,
            ),
            "metrics": self._tag(
                edge_id,
                revision,
                solution_pack,
                METRIC_CAMERA,
                "output",
                "application/json",
                camera_id=camera_id,
            ),
            "frame_overlay": self._tag(
                edge_id,
                revision,
                solution_pack,
                OUTPUT_FRAME,
                "output",
                "image/jpeg",
                camera_id=camera_id,
            ),
            "apps": {
                app: self._tag(
                    edge_id,
                    revision,
                    solution_pack,
                    OUTPUT_EVENT,
                    "output",
                    "application/json",
                    camera_id=camera_id,
                    app_id=app,
                )
                for app in apps
            },
            "snapshot": self._tag(
                edge_id,
                revision,
                solution_pack,
                OUTPUT_SNAPSHOT,
                "output",
                "image/jpeg",
                camera_id=camera_id,
            ),
        }

    def service_tags(
        self,
        edge_id: str,
        revision: int,
        solution_pack: str,
        service: str,
        device: str,
    ) -> dict[str, Any]:
        return {
            "metrics": self._tag(
                edge_id,
                revision,
                solution_pack,
                METRIC_RUNTIME,
                "output",
                "application/json",
                service=service,
                device=device,
            ),
            "health": self._tag(
                edge_id,
                revision,
                solution_pack,
                HEALTH_RUNTIME,
                "output",
                "application/json",
                service=service,
                device=device,
            ),
        }

    def solution_tags(self, edge_id: str, revision: int, solution_pack: str) -> dict[str, Any]:
        return {
            "graph_status": self._tag(
                edge_id,
                revision,
                solution_pack,
                STATUS_GRAPH,
                "output",
                "application/json",
            ),
            "runtime_health": self._tag(
                edge_id,
                revision,
                solution_pack,
                HEALTH_RUNTIME,
                "output",
                "application/json",
            ),
            "runtime_metrics": self._tag(
                edge_id,
                revision,
                solution_pack,
                METRIC_RUNTIME,
                "output",
                "application/json",
            ),
        }

    def _tag(
        self,
        edge_id: str,
        revision: int,
        solution_pack: str,
        tag: str,
        direction: str,
        payload_type: str,
        camera_id: str | None = None,
        app_id: str | None = None,
        service: str | None = None,
        device: str | None = None,
    ) -> dict[str, Any]:
        topic = ".".join(
            part for part in (edge_id, solution_pack, camera_id, app_id, service, tag) if part
        )
        return ApiTag(
            api_version=API_VERSION,
            tag=tag,
            edge_id=edge_id,
            revision=revision,
            solution_pack=solution_pack,
            direction=direction,
            payload_type=payload_type,
            camera_id=camera_id,
            app_id=app_id,
            service=service,
            device=device,
            topic=topic,
        ).as_dict()
