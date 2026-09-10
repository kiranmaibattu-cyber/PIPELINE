"""Versioned identity inputs and staged enrollment, independent of management UI."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading

import numpy as np

from edge_runtime.runtime.sync_store import SyncStore


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".next")
    with temporary.open("w") as f:
        json.dump(data, f, allow_nan=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("identifier must contain 1-100 letters, digits, underscores or hyphens")
    return value


def embedding_space():
    model = Path(os.environ["ADAFACE_INT8_XML"])
    digest = hashlib.sha256(model.read_bytes() + model.with_suffix(".bin").read_bytes()).hexdigest()
    return "adaface-aligned112-l2-v1:" + digest


def enrollment_evidence(gallery, vectors, management_root):
    """Bind each template to its staged chip, never to a later camera frame."""
    root = Path(gallery.root).resolve()
    artifacts, samples = {}, []
    for index, vector in enumerate(vectors):
        sample = {"template_index": index, "pose": vector.cell,
                  "quality": float(vector.quality), "artifact_key": None}
        if vector.chip_path:
            path = (root / vector.chip_path).resolve()
            if not path.is_relative_to(root):
                raise ValueError("enrollment chip is outside the staging directory")
            if not path.is_file():
                raise ValueError("enrollment chip is missing")
            import cv2
            chip = cv2.imread(str(path))
            if chip is None:
                raise ValueError("enrollment chip cannot be decoded")
            ok, encoded = cv2.imencode(".jpg", chip)
            if not ok:
                raise ValueError("enrollment chip cannot be encoded")
            data = encoded.tobytes()
            digest = hashlib.sha256(data).hexdigest()
            directory = Path(management_root) / "artifacts"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / (digest + ".jpg")
            # Publish evidence before the candidate is made visible in the outbox.
            with target.with_suffix(".next").open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            target.with_suffix(".next").replace(target)
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            key = "face_sample_" + str(index)
            artifacts[key] = {"ref": "management/artifacts/" + target.name,
                              "content_type": "image/jpeg"}
            sample["artifact_key"] = key
        else:
            sample["missing_reason"] = "no_staged_chip"
        samples.append(sample)
    return artifacts, samples


def gallery_from_document(root, document, expected_space):
    from PLATF.face_enroll_gallery import Gallery, Vec
    if document.get("embedding_space") != expected_space:
        raise ValueError("incompatible face embedding space")
    revision = document.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("gallery revision must be a positive integer")
    people = document.get("people")
    if not isinstance(people, list) or len(people) > 1000:
        raise ValueError("people must be an array of at most 1000 identities")
    gallery = Gallery(root)
    gallery.vecs = []
    names = set()
    for person in people:
        pid = identifier(person["person_id"])
        if pid in names:
            raise ValueError("duplicate person_id")
        names.add(pid)
        if not isinstance(person.get("display_name"), str):
            raise ValueError("display_name is required")
        if person.get("group", "") not in {"", "authorised", "unauthorised"}:
            raise ValueError("invalid identity group")
        templates = person.get("templates")
        if not isinstance(templates, list) or not 1 <= len(templates) <= 20:
            raise ValueError("each identity needs 1-20 face templates")
        for template in templates:
            vector = np.asarray(template, dtype=np.float32)
            norm = float(np.linalg.norm(vector))
            if vector.shape != (512,) or not np.isfinite(vector).all() or norm < 1e-6:
                raise ValueError("face template must be a finite, nonzero 512-vector")
            gallery.vecs.append(Vec(person=pid, cell="frontal", mode=0, vec=vector / norm,
                                    quality=1.0, chip_path="", source="management"))
    return gallery


class ManagementIdentityService:
    def __init__(self, platform, session_factory=None):
        self.platform = platform
        self.root = Path(os.environ["MANAGEMENT_SYNC_ROOT"])
        self.store = SyncStore(self.root)
        self.lock = threading.RLock()
        self.session = None
        self.session_id = None
        self.session_factory = session_factory
        self.space = embedding_space()

    def execute(self, command):
        cid = identifier(command["command_id"])
        request = json.dumps(command, sort_keys=True, allow_nan=False)
        with self.lock:
            with self.store.connect() as db:
                row = db.execute("SELECT request,result FROM commands WHERE id=?", (cid,)).fetchone()
                if row:
                    if row[0] != request:
                        raise ValueError("command_id reused with different content")
                    return json.loads(row[1]) if row[1] else {
                        "command_id": cid, "status": "interrupted",
                        "error": "Command execution interrupted; inspect state before issuing a new command ID"}
                db.execute("INSERT INTO commands VALUES (?,?,NULL)", (cid, request))
            try:
                result = {"command_id": cid, "status": "applied", "result": self.apply(command)}
            except (ValueError, KeyError, RuntimeError, TypeError) as exc:
                result = {"command_id": cid, "status": "rejected", "error": str(exc)}
            with self.store.connect() as db:
                db.execute("UPDATE commands SET result=? WHERE id=?", (json.dumps(result), cid))
            return result

    def apply(self, command):
        action, data = command["type"], command.get("payload") or {}
        if action == "identity.mappings.replace":
            path = self.root / "identity-mappings.json"
            current = json.loads(path.read_text()) if path.exists() else {}
            revision = data.get("revision")
            if type(revision) is not int or revision < 1 or revision < current.get("revision", 0):
                raise ValueError("invalid identity mapping revision")
            if revision == current.get("revision") and data != current:
                raise ValueError("mapping revision reused")
            rows = data.get("mappings")
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError("invalid mapping list")
            keys = set()
            for row in rows:
                key = (identifier(row["camera_id"]), identifier(row["stream_session_id"]), str(row["local_identity_id"]))
                identifier(row["central_person_id"])
                if key in keys:
                    raise ValueError("duplicate identity mapping")
                keys.add(key)
            atomic_json(path, data)
            return {"revision": revision, "mappings": len(rows)}
        if action == "gallery.replace":
            adapter = self.platform.face_gallery
            document = data
            replacement = gallery_from_document(adapter.root, document, self.space)
            current = adapter.managed_document or {}
            if document["revision"] < current.get("revision", 0):
                raise ValueError("gallery revision is older than active revision")
            if document["revision"] == current.get("revision") and document != current:
                raise ValueError("gallery revision cannot be reused for different content")
            if document == current:
                return adapter.status()
            with self.platform.host._lock, adapter._lock:
                atomic_json(adapter.root / "managed.json", document)
                adapter.gallery = replacement
                adapter.managed_document = document
                self.platform.face_groups.clear()
                self.platform.face_groups.update({p["person_id"]: p.get("group", "") for p in document["people"]})
                for person in self.platform.store.all():
                    self.platform.store.identity.revoke(person.person_uuid)
                self.platform._track_name.clear()
                for plugin in self.platform.host.plugins:
                    if plugin.name == "face":
                        plugin._active.clear()
            return adapter.status()
        if action == "enrollment.start":
            sid = identifier(data["session_id"])
            pid = identifier(data["person_id"])
            if self.session is not None:
                raise ValueError("finish or cancel the current enrollment first")
            target = self.root / "enrollment" / sid
            if target.exists():
                raise ValueError("session_id already used; recover its staged data or use a new ID")
            source = self.platform.camera_source(data["camera_id"])
            if not source:
                raise ValueError("unknown camera")
            from PLATF.face_enroll_gallery import Gallery
            from PLATF.plugins.enroll_gallery import EnrollmentGalleryAdapter
            from PLATF.enrollment import EnrollmentSession
            target.mkdir(parents=True)
            gallery = Gallery(target)
            gallery.save()
            atomic_json(target / "session.json", data)
            self.session = (self.session_factory or EnrollmentSession)(
                EnrollmentGalleryAdapter(gallery), pid, data["camera_id"], source)
            self.session_id = sid
            return {"session_id": sid, "state": self.session.state}
        if action in {"enrollment.status", "enrollment.stop", "enrollment.cancel", "enrollment.save", "enrollment.retake"}:
            sid = identifier(data["session_id"])
            if self.session_id != sid or self.session is None:
                raise ValueError("no matching live enrollment; staged files survive restart")
            session = self.session
            if action != "enrollment.status":
                session.stop()
                if session._thread.is_alive():
                    raise ValueError("enrollment is still stopping; retry with a new command ID")
            if action == "enrollment.cancel":
                session.rollback()
                self.session = None
                return {"session_id": sid, "state": "cancelled"}
            if action == "enrollment.retake":
                session.rollback()
                from PLATF.enrollment import EnrollmentSession
                self.session = (self.session_factory or EnrollmentSession)(
                    session.adapter, session.name, session.camera, session.source)
                return {"session_id": sid, "state": self.session.state}
            if action == "enrollment.save":
                vectors = session.adapter.gallery.of(session.name)
                if not any(v.cell == "frontal" for v in vectors):
                    raise ValueError("need at least one frontal capture")
                if len(vectors) > 20:
                    raise ValueError("candidate exceeds the managed gallery limit of 20 templates")
                artifacts, samples = enrollment_evidence(session.adapter.gallery, vectors, self.root)
                candidate = {"session_id": sid, "person_id": session.name,
                             "camera_id": session.camera, "artifacts": artifacts, "samples": samples,
                             "embedding_space": self.space,
                             "templates": [v.vec.tolist() for v in vectors]}
                atomic_json(self.root / "enrollment" / sid / "candidate.json", candidate)
                self.store.enqueue("enrollment:" + sid, "enrollment_candidate", candidate)
                self.session = None
                return {"session_id": sid, "state": "awaiting_management_commit", "templates": len(vectors)}
            return {"session_id": sid, **session.status()}
        if action == "gallery.status":
            return {**self.platform.face_gallery.status(), "embedding_space": self.space}
        raise ValueError("unsupported management command")
