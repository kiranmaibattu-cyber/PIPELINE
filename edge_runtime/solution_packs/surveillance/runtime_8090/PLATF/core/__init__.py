"""PLATF core substrate: the Person Object, event bus, and plugin contract."""
from .observation import TrackObservation
from .person import Person, PersonStore, Exemplar, MODALITIES
from .events import Event, EventBus
from .plugin import Plugin, PluginContext, PluginHost
from .schemas import (CameraTopology, EnrollmentFaceGallery, EventType,
                      IdentityLink, IdentityMapping, TrackGallery)

__all__ = [
    "TrackObservation",
    "Person", "PersonStore", "Exemplar", "MODALITIES",
    "Event", "EventBus",
    "Plugin", "PluginContext", "PluginHost",
    "EventType", "IdentityLink", "IdentityMapping",
    "EnrollmentFaceGallery", "TrackGallery", "CameraTopology",
]
