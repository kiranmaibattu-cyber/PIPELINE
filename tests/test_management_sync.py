import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from edge_runtime.runtime.management_sync import ManagementSync
from edge_runtime.runtime.sync_store import SyncStore


class ManagementSyncTest(unittest.TestCase):
    def test_queue_survives_reopen_and_deduplicates_pending_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SyncStore(tmp)
            store.bind_edge("edge-1")
            with self.assertRaisesRegex(ValueError, "different edge_id"):
                store.bind_edge("edge-2")
            store.enqueue("r1", "reid_observation", {"value": 1})
            store.enqueue("r1", "reid_observation", {"value": 1})
            reopened = SyncStore(tmp)
            self.assertEqual(1, reopened.count())
            self.assertEqual("r1", reopened.pending()[0][0])
            reopened.ack("r1")
            self.assertEqual(0, store.count())

    def test_http_requires_explicit_loopback_test_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "token"
            token.write_text("test-token")
            status = SimpleNamespace(state_dir=root, edge_id="edge-1")
            config = {"url": "http://management.invalid", "token_file": str(token)}
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                ManagementSync(status, config)
            config.update(url="http://127.0.0.1:12345", allow_insecure_loopback=True)
            ManagementSync(status, config)

    def test_negative_ack_and_transport_failure_keep_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = root / "token"
            token.write_text("test-token")
            status = SimpleNamespace(state_dir=root, edge_id="edge-1", as_payload=lambda: {})
            sync = ManagementSync(status, {"url": "http://localhost", "allow_insecure_loopback": True,
                                           "token_file": str(token)})
            sync.store.enqueue("r1", "reid_observation", {"artifacts": {}})
            def wrong_ack(path, payload=None, *args):
                return {"commands": []} if path.endswith("/commands") else {"record_id": "wrong"}
            sync.request = wrong_ack
            with self.assertRaisesRegex(ValueError, "acknowledgement"):
                sync.cycle()
            self.assertEqual(1, sync.store.count())
            def unavailable(*args):
                raise OSError("network down")
            sync.request = unavailable
            with self.assertRaises(OSError):
                sync.cycle()
            self.assertEqual(1, SyncStore(sync.store.root).count())
            sync.request = lambda path, *args: {"commands": [], "record_id": "r1"}
            sync.cycle()
            self.assertEqual(0, sync.store.count())
