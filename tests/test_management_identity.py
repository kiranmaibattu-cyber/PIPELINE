import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge_runtime/solution_packs/surveillance/runtime_8090"))
from PLATF.face_enroll_gallery import Gallery, Vec
from PLATF.plugins.enroll_gallery import EnrollmentGalleryAdapter
from PLATF.management import ManagementIdentityService
from PLATF.core.person import PersonStore
from PLATF.plugins.face import FacePlugin
from edge_runtime.runtime.sync_store import SyncStore
from edge_runtime.runtime.management_sync import ManagementSync
from PLATF.observation_export import ObservationExporter


class FakeEnrollment:
    def __init__(self, adapter, name, camera, source):
        self.adapter, self.name, self.camera, self.source = adapter, name, camera, source
        self.state = "capturing"
        self._thread = SimpleNamespace(is_alive=lambda: False)
        vector = np.zeros(512, dtype=np.float32)
        vector[0] = 1
        adapter.gallery.vecs.append(Vec(person=name, cell="frontal", mode=0,
                                       vec=vector, quality=1, chip_path=""))
        adapter.gallery.save()

    def stop(self):
        self.state = "done"

    def rollback(self):
        self.adapter.gallery.vecs = []
        self.adapter.gallery.save()

    def status(self):
        return {"state": self.state, "captured": len(self.adapter.gallery.vecs)}


class ManagementIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        model = root / "face.xml"
        model.write_text("test model")
        model.with_suffix(".bin").write_bytes(b"test weights")
        env = patch.dict(os.environ, MANAGEMENT_SYNC_ROOT=str(root / "management"), ADAFACE_INT8_XML=str(model))
        env.start()
        self.addCleanup(env.stop)
        gallery = Gallery(root / "gallery")
        gallery.save()
        self.platform = SimpleNamespace(face_gallery=EnrollmentGalleryAdapter(gallery),
            face_groups={}, store=PersonStore(), _track_name={},
            host=SimpleNamespace(_lock=threading.RLock(), plugins=[]), camera_source=lambda c: "rtsp://test")
        self.service = ManagementIdentityService(self.platform, FakeEnrollment)

    def cmd(self, cid, action, payload):
        return self.service.execute(dict(command_id=cid, type=action, payload=payload))

    def gallery(self, revision=1):
        vector = [1.0] + [0.0] * 511
        return {"revision": revision, "embedding_space": self.service.space,
                "people": [{"person_id": "person-1", "display_name": "Synthetic Test",
                            "group": "authorised", "templates": [vector, vector, vector]}]}

    def test_gallery_replace_duplicate_revision_reject_and_delete_survive_restart(self):
        data = self.gallery()
        first = self.cmd("g1", "gallery.replace", data)
        self.assertEqual("applied", first["status"])
        self.assertEqual(first, self.cmd("g1", "gallery.replace", data))
        adapter = EnrollmentGalleryAdapter.load(self.platform.face_gallery.root)
        self.assertEqual(1, adapter.status()["managed_revision"])
        self.assertEqual(["person-1"], adapter.status()["people"])
        changed = self.gallery()
        changed["people"] = []
        self.assertEqual("rejected", self.cmd("conflict", "gallery.replace", changed)["status"])
        changed["revision"] = 2
        self.assertEqual("applied", self.cmd("delete", "gallery.replace", changed)["status"])
        self.assertEqual([], EnrollmentGalleryAdapter.load(adapter.root).status()["people"])
        self.assertEqual("rejected", self.cmd("old", "gallery.replace", data)["status"])
        with self.assertRaisesRegex(ValueError, "reused"):
            self.cmd("g1", "gallery.replace", changed)

    def test_incompatible_and_nonfinite_gallery_rejected_without_mutating(self):
        bad = self.gallery()
        bad["embedding_space"] = "different-model"
        self.assertEqual("rejected", self.cmd("bad", "gallery.replace", bad)["status"])
        bad = self.gallery()
        bad["people"][0]["templates"] = [[0] * 512]
        self.assertEqual("rejected", self.cmd("zero", "gallery.replace", bad)["status"])
        self.assertEqual([], self.platform.face_gallery.status()["people"])

    def test_managed_match_carries_central_identity_and_deletion_revokes_it(self):
        self.cmd("gallery", "gallery.replace", self.gallery())
        plugin = FacePlugin(self.platform.face_gallery)
        self.platform.host.plugins = [plugin]
        person = self.platform.store.mint(1)
        events = []
        ctx = SimpleNamespace(store=self.platform.store, emit=events.append)
        obs = SimpleNamespace(camera="cam1", t=1, meta={}, has_face=lambda: True,
                              face_emb=np.asarray([1.0] + [0.0]*511, dtype=np.float32))
        plugin.process(obs, person, ctx)
        self.assertTrue(events, "Synthetic compatible vector should match managed gallery")
        self.assertEqual("person-1", events[0].payload["central_person_id"])
        self.assertEqual(1, events[0].payload["gallery_revision"])
        self.cmd("delete", "gallery.replace", {"revision": 2, "embedding_space": self.service.space, "people": []})
        self.assertIsNone(self.platform.store.identity.get(person.person_uuid))
        self.assertEqual({}, plugin._active)

    def test_enrollment_stages_then_exports_without_activating(self):
        start = {"session_id": "s1", "person_id": "test-1", "camera_id": "cam1"}
        result = self.cmd("start", "enrollment.start", start)
        self.assertEqual("applied", result["status"])
        self.assertEqual(result, self.cmd("start", "enrollment.start", start))
        self.assertEqual([], self.platform.face_gallery.status()["people"])
        self.assertEqual("done", self.cmd("stop", "enrollment.stop", {"session_id": "s1"})["result"]["state"])
        result = self.cmd("save", "enrollment.save", {"session_id": "s1"})
        self.assertEqual("awaiting_management_commit", result["result"]["state"])
        self.assertEqual([], self.platform.face_gallery.status()["people"])
        store = SyncStore(self.service.root)
        self.assertEqual("enrollment_candidate", store.pending()[0][1])
        restarted = ManagementIdentityService(self.platform, FakeEnrollment)
        self.assertEqual(result, restarted.execute(dict(command_id="save", type="enrollment.save", payload={"session_id": "s1"})))

    def test_cancel_and_retake_are_scoped_to_session(self):
        self.cmd("start", "enrollment.start", {"session_id": "s2", "person_id": "p2", "camera_id": "cam1"})
        self.assertEqual("rejected", self.cmd("wrong", "enrollment.stop", {"session_id": "other"})["status"])
        self.assertEqual("applied", self.cmd("retake", "enrollment.retake", {"session_id": "s2"})["status"])
        self.assertEqual("cancelled", self.cmd("cancel", "enrollment.cancel", {"session_id": "s2"})["result"]["state"])
        self.assertEqual(0, self.service.store.count())

    def test_saved_chip_and_template_commit_then_recognize_and_delete(self):
        self.cmd("start", "enrollment.start", {"session_id": "chips", "person_id": "p1", "camera_id": "cam1"})
        gallery = self.service.session.adapter.gallery
        cv2.imwrite(str(gallery.root / "sample.png"), np.full((112, 112, 3), 80, np.uint8))
        gallery.vecs[0].chip_path = "sample.png"
        result = self.cmd("save", "enrollment.save", {"session_id": "chips"})
        self.assertEqual("applied", result["status"])
        candidate = self.service.store.pending()[0][2]
        sample = candidate["samples"][0]
        self.assertEqual(0, sample["template_index"])
        asset = candidate["artifacts"][sample["artifact_key"]]
        path = self.service.root / Path(asset["ref"]).relative_to("management")
        self.assertEqual((112, 112, 3), cv2.imread(str(path)).shape)
        token = self.service.root / "test-token"
        token.write_text("test-only")
        sync = ManagementSync(SimpleNamespace(state_dir=self.service.root.parent, edge_id="test",
                              as_payload=lambda: {}), {"url": "http://localhost",
                              "allow_insecure_loopback": True, "token_file": str(token)})
        received = {}
        def receiver(endpoint, payload=None, *args):
            if endpoint.endswith("/commands"):
                return {"commands": []}
            if "/artifacts/" in endpoint:
                digest = hashlib.sha256(payload).hexdigest()
                self.assertTrue(endpoint.endswith(digest))
                received[digest] = payload
                return {"sha256": digest}
            if endpoint.endswith("/records"):
                for item in payload["payload"]["artifacts"].values():
                    self.assertIn(item["artifact_id"], received)
                received["candidate"] = payload["payload"]
                return {"record_id": payload["record_id"]}
            return {}
        sync.request = receiver
        sync.cycle()
        self.assertEqual(0, self.service.store.count())
        candidate = received["candidate"]
        self.assertEqual([], self.platform.face_gallery.status()["people"])
        document = {"revision": 1, "embedding_space": candidate["embedding_space"], "people": [{
            "person_id": candidate["person_id"], "display_name": "Synthetic Test",
            "templates": candidate["templates"]}]}
        self.assertEqual("applied", self.cmd("commit", "gallery.replace", document)["status"])
        self.platform.face_gallery = EnrollmentGalleryAdapter.load(self.platform.face_gallery.root)
        plugin = FacePlugin(self.platform.face_gallery)
        self.platform.host.plugins = [plugin]
        person = self.platform.store.mint(1)
        events = []
        obs = SimpleNamespace(camera="cam1", t=1, meta={}, has_face=lambda: True,
                              face_emb=np.asarray(candidate["templates"][0], dtype=np.float32))
        plugin.process(obs, person, SimpleNamespace(store=self.platform.store, emit=events.append))
        self.assertEqual("p1", events[0].payload["central_person_id"])
        self.cmd("delete", "gallery.replace", dict(document, revision=2, people=[]))
        self.assertIsNone(self.platform.store.identity.get(person.person_uuid))

    def test_chip_outside_staging_is_rejected_without_candidate_upload(self):
        self.cmd("start", "enrollment.start", {"session_id": "badchip", "person_id": "p1", "camera_id": "cam1"})
        self.service.session.adapter.gallery.vecs[0].chip_path = "../../outside.jpg"
        self.assertEqual("rejected", self.cmd("save", "enrollment.save", {"session_id": "badchip"})["status"])
        self.assertEqual(0, self.service.store.count())

    def test_mapping_reaches_future_observations_and_is_session_scoped(self):
        model = os.environ["ADAFACE_INT8_XML"]
        with patch.dict(os.environ, EMB_MODEL=model, GAIT_MODEL=model):
            exporter = ObservationExporter(self.service.root)
        obs = SimpleNamespace(camera="c1", local_id=4, t=1, frame_idx=1, bbox=(0, 0, 10, 10),
                              quality=1, app_emb=np.array([1., 0.]), face_emb=None, gait_emb=None,
                              meta={"evidence": {"stream_session_id": "session1"}})
        payload = {"revision": 1, "mappings": [{"camera_id": "c1", "stream_session_id": "session1",
                   "local_identity_id": 7, "central_person_id": "p1"}]}
        self.cmd("mapping", "identity.mappings.replace", payload)
        exporter.observe(obs, 7)
        first = self.service.store.pending()[0][2]
        self.assertEqual("p1", first["central_person_id"])
        self.assertEqual(1, first["mapping_revision"])
        obs.meta["evidence"]["stream_session_id"] = "session2"
        exporter.observe(obs, 7)
        self.assertIsNone(self.service.store.pending()[1][2]["central_person_id"])

    def test_identity_mapping_is_versioned_and_does_not_merge_local_tracks(self):
        payload = {"revision": 1, "mappings": [{"camera_id": "c1", "stream_session_id": "session1",
                   "local_identity_id": 7, "central_person_id": "p1"}]}
        self.assertEqual("applied", self.cmd("mapping", "identity.mappings.replace", payload)["status"])
        self.assertEqual(payload, json.loads((self.service.root / "identity-mappings.json").read_text()))
