"""Sample searchable multimodal observations into the persistent sync queue."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import numpy as np

from edge_runtime.runtime.frame_evidence import read_evidence
from edge_runtime.runtime.sync_store import SyncStore


class ObservationExporter:
    def __init__(self, root):
        self.store = SyncStore(root)
        self.last = {}
        self.spaces = {}
        for modality, key in (("body", "EMB_MODEL"), ("face", "ADAFACE_INT8_XML"), ("gait", "GAIT_MODEL")):
            path = Path(os.environ[key])
            digest = hashlib.sha256(path.read_bytes() + path.with_suffix(".bin").read_bytes()).hexdigest()
            self.spaces[modality] = ("adaface-aligned112-l2-v1:" if modality == "face"
                                     else f"pipeline-{modality}-v1:") + digest

    def observe(self, obs, gid, central_person_id=None):
        evidence = obs.meta.get("evidence") or {}
        session = evidence.get("stream_session_id") or os.environ.get("EDGE_RUNTIME_SESSION_ID", "unknown")
        now = time.monotonic()
        embeddings = []
        for modality, value in (("body", obs.app_emb), ("face", obs.face_emb), ("gait", obs.gait_emb)):
            key = (obs.camera, session, obs.local_id, modality)
            if value is None or now - self.last.get(key, -100) < 5:
                continue
            vector = np.asarray(value, dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(vector))
            if not vector.size or not np.isfinite(vector).all() or norm < 1e-6:
                continue
            self.last[key] = now
            embeddings.append({"modality": modality, "embedding_space": self.spaces[modality],
                               "dimension": vector.size, "vector": (vector / norm).tolist()})
        if not embeddings:
            return
        mapping_file = self.store.root / "identity-mappings.json"
        mapping_revision = None
        if mapping_file.exists():
            mappings = json.loads(mapping_file.read_text())
            mapping_revision = mappings["revision"]
            for mapping in mappings["mappings"]:
                if (mapping["camera_id"], mapping["stream_session_id"], str(mapping["local_identity_id"])) == (obs.camera, session, str(gid)):
                    central_person_id = mapping["central_person_id"]
                    break
        if len(self.last) > 8192:
            self.last = {k: v for k, v in self.last.items() if now - v < 60}
        artifacts = {}
        jpeg = read_evidence(evidence)
        if jpeg:
            digest = hashlib.sha256(jpeg).hexdigest()
            directory = self.store.root / "artifacts"
            directory.mkdir(exist_ok=True)
            path = directory / (digest + ".jpg")
            if not path.exists():
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(jpeg)
                temporary.replace(path)
            artifacts["frame"] = {"ref": "management/artifacts/" + path.name, "content_type": "image/jpeg"}
        oid = str(uuid.uuid4())
        self.store.enqueue("observation:" + oid, "reid_observation", {
            "observation_id": oid, "camera_id": obs.camera, "stream_session_id": session,
            "track_id": obs.local_id, "local_identity_id": gid,
            "central_person_id": central_person_id, "captured_at": evidence.get("captured_at", obs.t),
            "mapping_revision": mapping_revision,
            "frame_id": obs.frame_idx, "bbox": list(obs.bbox), "quality": float(obs.quality),
            "embeddings": embeddings, "artifacts": artifacts})
