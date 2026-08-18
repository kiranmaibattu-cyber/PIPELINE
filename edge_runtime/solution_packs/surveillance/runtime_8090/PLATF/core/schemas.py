"""Platform data contracts -- the interfaces every plugin communicates through.

These are the schemas the architecture spec locks (see
docs/superpowers/specs/2026-07-29-platform-architecture-design.md). They are kept
deliberately thin: dataclasses for records, typing.Protocol for the two galleries so
plugins can be unit-tested against fakes without the heavy real gallery.

The persistent-Person record itself lives in `person.py` (it owns the store + lifecycle);
this module holds the SATELLITE contracts that attach to a Person -- identity linkage,
the gallery boundary, camera topology, and the canonical event-type set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


# --- canonical event types (the bus vocabulary) --------------------------------
class EventType:
    IDENTITY = "identity"              # a track bound / cross-cam linked to a Person
    PERSON_MERGED = "person_merged"    # two hypotheses folded into one Person (evidence)
    FACE_RECOGNIZED = "face_recognized"  # a Person linked to an enrolled identity
    INTRUSION = "intrusion"
    LOITERING = "loitering"
    COUNT = "count"
    ABSENCE = "absence"

    ALL = (IDENTITY, PERSON_MERGED, FACE_RECOGNIZED,
           INTRUSION, LOITERING, COUNT, ABSENCE)


# --- identity linkage (Person <-> enrolled employee) ---------------------------
@dataclass
class IdentityLink:
    """A confidence-scored link from a persistent Person to an ENROLLED identity.

    Metadata, never a key: the Person exists (and keeps its history) whether or not it
    is linked. Deleting an employee from enrollment revokes the link, not the tracking.
    """
    employee_id: str
    name: str = ""
    confidence: float = 0.0
    source: str = "face"               # "face" | "manual"
    last_verified: float = 0.0


class IdentityMapping:
    """person_uuid -> IdentityLink. A separate table, so identities can be updated,
    revoked, or re-verified without ever mutating a Person or the enrollment gallery."""

    def __init__(self):
        self._by_person: dict[str, IdentityLink] = {}

    def link(self, person_uuid: str, link: IdentityLink) -> None:
        self._by_person[str(person_uuid)] = link

    def get(self, person_uuid: str) -> Optional[IdentityLink]:
        return self._by_person.get(str(person_uuid))

    def revoke(self, person_uuid: str) -> None:
        self._by_person.pop(str(person_uuid), None)

    def reassign(self, from_uuid: str, to_uuid: str) -> None:
        """Move a link when its Person is merged into another (keep the strongest)."""
        src = self._by_person.pop(str(from_uuid), None)
        if src is None:
            return
        cur = self._by_person.get(str(to_uuid))
        if cur is None or src.confidence > cur.confidence:
            self._by_person[str(to_uuid)] = src


# --- gallery boundary: two galleries, never mixed ------------------------------
@runtime_checkable
class EnrollmentFaceGallery(Protocol):
    """PERMANENT, READ-ONLY. Enrolled employees (curated AdaFace exemplars). Answers
    'who is this?'. The platform SEARCHES it and never writes it."""

    def search(self, face_emb, k: int = 1) -> list:
        """Return [(employee_id, distance), ...] nearest enrolled identities."""
        ...


@runtime_checkable
class TrackGallery(Protocol):
    """TEMPORARY. The reused MTMC gallery (RealReID / MultiModalGallery). Answers 'same
    person?'. OWNED BY THE BACKBONE: the platform consumes the gid it already assigned
    and does NOT instantiate a second one. This protocol documents the boundary; the
    identity-ingest plugin depends only on the gid carried on the observation."""

    def match(self, app, face, camera, t, gait, exclude_gids=None) -> tuple:
        ...

    def reinforce(self, gid, app, face, camera, t, gait) -> None:
        ...


# --- camera topology (interface now; backbone applies it internally) -----------
@dataclass
class CameraTopology:
    """Learned per-pair transition windows: (cam_a, cam_b) -> (min_s, max_s). On the
    current footage its value is limited (ch9<->ch10 mutual overlap, no frontal faces);
    kept as an interface so spatial/BEV work can slot in later."""
    windows: dict = field(default_factory=dict)   # {(cam_a, cam_b): (min_s, max_s)}

    def window(self, cam_a: str, cam_b: str) -> Optional[tuple]:
        return self.windows.get((cam_a, cam_b)) or self.windows.get((cam_b, cam_a))

    @classmethod
    def from_learned(cls, learned: dict) -> "CameraTopology":
        """Build from MTMC/reports/learned_transitions.json shape:
        {"ch9->ch10": {"min": .., "max": ..}, ...}"""
        w = {}
        for key, v in (learned or {}).items():
            if "->" in key and isinstance(v, dict):
                a, b = key.split("->", 1)
                w[(a.strip(), b.strip())] = (float(v.get("min", 0.0)), float(v.get("max", 0.0)))
        return cls(windows=w)
