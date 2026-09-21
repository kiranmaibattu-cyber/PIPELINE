"""Pydantic models that match the processor-config schema the worker expects.

The worker's pipeline/processor_config.py parses cameras out of a dict shaped
exactly like this — keep the field names in sync there if you change them
here. Use-case keys come from USE_CASE_RULES in that module.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


UseCaseName = Literal[
    "vehicle_counting",
    "pedestrian_counting",
    "wrong_way_driving_detection",
    "stopped_vehicle_detection",
    "vehicle_in_pedestrian_zone_alert",
    "plate_detection",
    "parking_violation_detection",
    "fire_smoke_detection",
]


class Point(BaseModel):
    x: float
    y: float


class Geometry(BaseModel):
    """A line, zone, or mask drawn by the UI."""
    id: Optional[str] = None
    name: Optional[str] = None
    shape: Literal["polygon", "rectangle", "line"] = "polygon"
    points: List[Point] = Field(default_factory=list)
    purpose: Optional[str] = None
    type: Optional[str] = None
    direction: Optional[Literal["a_to_b", "b_to_a", "both"]] = None


class UseCaseConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    lines: List[Geometry] = Field(default_factory=list)
    zones: List[Geometry] = Field(default_factory=list)
    masks: List[Geometry] = Field(default_factory=list)


class CameraSource(BaseModel):
    type: Literal["file", "rtsp", "http", "https"] = "file"
    uri: str
    fps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


class CameraProcessing(BaseModel):
    fps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


class Camera(BaseModel):
    model_config = ConfigDict(extra="allow")
    camera_id: str
    name: str
    enabled: bool = True
    source: CameraSource
    processing: CameraProcessing = Field(default_factory=CameraProcessing)
    analytics: Dict[UseCaseName, UseCaseConfig] = Field(default_factory=dict)


class CameraCollection(BaseModel):
    cameras: List[Camera] = Field(default_factory=list)


class ZoneUpsert(BaseModel):
    """Body of POST /api/cameras/{camera_id}/use-cases/{use_case}/geometry."""
    kind: Literal["line", "zone", "mask"]
    geometry: Geometry


class AnalyticsEvent(BaseModel):
    """Pass-through payload from worker's Redis stream → UI WebSocket."""
    model_config = ConfigDict(extra="allow")
    schema_version: str
    message_type: str
    camera: Dict[str, Any]
    events: List[Dict[str, Any]] = Field(default_factory=list)
    objects: List[Dict[str, Any]] = Field(default_factory=list)
