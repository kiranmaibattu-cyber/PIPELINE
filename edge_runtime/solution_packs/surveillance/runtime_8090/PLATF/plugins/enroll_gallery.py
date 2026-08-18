"""Adapter from the vendored FACE enrollment gallery to PLATF's face protocol."""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np


class EnrollmentGalleryAdapter:
    def __init__(self, gallery, root=None):
        self.gallery = gallery
        self.root = Path(root or gallery.root)
        self._lock = threading.RLock()
        self._signature = self._disk_signature()

    def _disk_signature(self):
        """A cheap fingerprint of the atomic index/vector pair."""
        try:
            return tuple(
                (p.stat().st_mtime_ns, p.stat().st_size)
                for p in (self.root / "index.json", self.root / "vectors.npy")
            )
        except OSError:
            return None

    def reload_if_changed(self, force: bool = False) -> bool:
        """Atomically swap in a newly enrolled gallery without restarting PLATF.

        The enrollment tool writes both files atomically.  We construct and validate a
        complete replacement before publishing it, so a transient/invalid gallery can
        never take recognition down or replace the last known-good in-memory copy.
        """
        sig = self._disk_signature()
        if sig is None or (not force and sig == self._signature):
            return False
        from PLATF.face_enroll_gallery import Gallery

        replacement = Gallery(self.root)
        if not replacement.vecs:
            return False
        with self._lock:
            self.gallery = replacement
            self._signature = sig
        return True

    def search(self, face_emb, k: int = 1) -> list:
        try:
            self.reload_if_changed()
        except Exception:
            pass
        with self._lock:
            res = self.gallery.identify(face_emb)
        if res.get("matched"):
            return [(res.get("name"), round(1.0 - float(res.get("score", 0.0)), 4))]
        return []

    def status(self) -> dict:
        with self._lock:
            people = self.gallery.people()
            return {"loaded": True, "path": str(self.root), "people": people,
                    "person_count": len(people), "vectors": len(self.gallery.vecs)}

    def consider(self, person: str, obs, source: str = "camera") -> dict:
        """Offer one measured FaceObs to the gallery's own storage decision.

        This is the REAL enrollment path (pose cells, quality gates, impostor guard,
        mode discovery, capacity eviction) as opposed to `enroll()` below, which
        appends flat frontal vectors from face embeddings that carry no pose.

        Persists on every accepted sample: a session that dies at minute three must
        keep minutes one and two. The relative chip path is added to the result so a
        caller can roll its own writes back.
        """
        from PLATF.face_enroll_gallery import safe_name

        person = safe_name(str(person))
        with self._lock:
            res = self.gallery.consider(person, obs, source=source)
            if res.get("action") in ("add", "replace"):
                # gallery.consider() reports the chip as an absolute path; store the
                # gallery-relative one, which is what Vec.chip_path holds.
                try:
                    res["chip_path"] = str(
                        Path(res["chip"]).relative_to(self.root)).replace("\\", "/")
                except (KeyError, ValueError):
                    res["chip_path"] = self.gallery.vecs[-1].chip_path
                self.gallery.save()
                self._signature = self._disk_signature()
        return res

    def coverage(self, person: str) -> dict:
        """Per-pose-bin vector and mode counts -- what the reference tool's sidebar
        showed so the operator knew which way to turn."""
        from PLATF.face_core import POSE_BINS
        from PLATF.face_enroll_gallery import safe_name

        person = safe_name(str(person))
        with self._lock:
            mine = self.gallery.of(person)
        out = {}
        for cell in POSE_BINS:
            vs = [v for v in mine if v.cell == cell]
            out[cell] = {"vectors": len(vs), "modes": len({v.mode for v in vs})}
        return out

    def drop_chips(self, chip_paths: list) -> int:
        """Remove specific vectors (and their chips) -- the rollback for a cancelled
        session. Scoped to exact chip paths so a cancel can never delete vectors that
        were already in the gallery before this session started."""
        wanted = {str(p) for p in chip_paths if p}
        if not wanted:
            return 0
        with self._lock:
            doomed = [v for v in self.gallery.vecs if v.chip_path in wanted]
            for v in doomed:
                self.gallery.vecs.remove(v)
                try:
                    (self.root / v.chip_path).unlink()
                except OSError:
                    pass
            if doomed:
                self.gallery.save()
                self._signature = self._disk_signature()
        return len(doomed)

    def enroll(self, name: str, exemplars: list) -> dict:
        """Enroll a tracked Person from several already aligned AdaFace embeddings.

        This is deliberately stricter than recognition: one transient observation may
        never write permanent identity. Samples must be coherent and must not resemble
        another enrolled person strongly enough to trip the gallery impostor guard.
        """
        from PLATF.face_enroll_gallery import Vec, safe_name

        if not str(name).strip():
            raise ValueError("name is required")
        name = safe_name(str(name))
        vecs = []
        for item in exemplars:
            arr = np.asarray(getattr(item, "emb", item), dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(arr))
            if arr.size == 512 and norm > 1e-6:
                vecs.append(arr / norm)
        if len(vecs) < 3:
            raise ValueError("need at least 3 valid face observations")
        vecs = vecs[:5]
        centroid = np.mean(vecs, axis=0)
        centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
        coherence = [float(v @ centroid) for v in vecs]
        if float(np.median(coherence)) < 0.55:
            raise ValueError("face observations are not coherent enough to enroll")

        with self._lock:
            others = [v for v in self.gallery.vecs if v.person != name]
            if others:
                other_mat = np.stack([v.vec for v in others]).astype(np.float32)
                best = max(float(np.max(other_mat @ v)) for v in vecs)
                if best >= float(self.gallery.cfg["impostor_guard"]):
                    raise ValueError(f"face resembles another enrolled person ({best:.2f})")
            # Replace platform-captured samples for this name; curated FACE/live.py
            # samples remain intact and can coexist with them.
            self.gallery.vecs = [v for v in self.gallery.vecs
                                 if not (v.person == name and v.source == "platform")]
            for i, v in enumerate(vecs):
                self.gallery.vecs.append(Vec(
                    person=name, cell="frontal", mode=0, vec=v,
                    quality=coherence[i], chip_path="", source="platform",
                    meta={"capture": "tracked_face_embedding"}))
            self.gallery.save()
            self._signature = self._disk_signature()
            return {**self.status(), "enrolled": name, "samples": len(vecs),
                    "coherence": round(float(np.median(coherence)), 3)}

    @classmethod
    def load(cls, path):
        try:
            root = Path(path)
            if not (root / "index.json").exists() or not (root / "vectors.npy").exists():
                return None
            from PLATF.face_enroll_gallery import Gallery

            gallery = Gallery(root)
            if len(getattr(gallery, "vecs", [])) == 0:
                return None
            return cls(gallery, root)
        except Exception:
            return None
