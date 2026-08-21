from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from edge_runtime.runtime.desired_state_reload import (
    CompiledRuntimePlan,
    DesiredStateReloadError,
    DesiredStateSnapshot,
    DesiredStateWatcher,
)
from edge_runtime.runtime.solution_pack_entrypoint import RuntimeStatus, _apply_desired_state


class _Child:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.terminated = False

    def poll(self):
        return None if self.running else 1

    def terminate(self) -> None:
        self.terminated = True
        self.running = False

    def wait(self, timeout=None):  # noqa: ARG002
        return 0

    def kill(self) -> None:
        self.running = False


class _Compiler:
    def __init__(self, payload: dict) -> None:
        self.plan = CompiledRuntimePlan(
            content=json.dumps(payload).encode("utf-8"),
            payload=payload,
        )

    def compile(self, snapshot):  # noqa: ARG002
        return self.plan


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        reload_startup_grace=0,
        runtime_module="unused",
        plan="unused",
        generated_dir="unused",
        state_dir="unused",
        models_dir="unused",
        runtime_port=None,
    )


def _plan(revision: int, cameras: list[dict] | None = None) -> dict:
    return {
        "edge_id": "edge-1",
        "revision": revision,
        "solution_pack": "traffic",
        "cameras": cameras or [],
        "shared_services": [],
        "status": "accepted",
    }


class DesiredStateReloadTest(unittest.TestCase):
    def test_watcher_hashes_exact_content_and_reads_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "desired.json"
            path.write_text('{"revision": 7, "cameras": []}', encoding="utf-8")
            snapshot = DesiredStateWatcher(path).snapshot()

        self.assertEqual(7, snapshot.revision)
        self.assertEqual(64, len(snapshot.digest))

    def test_watcher_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "desired.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(DesiredStateReloadError):
                DesiredStateWatcher(path).snapshot()

    def test_valid_candidate_restarts_child_and_updates_active_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "traffic.runtime_plan.json"
            plan_path.write_text(json.dumps(_plan(1)), encoding="utf-8")
            old_child = _Child()
            new_child = _Child()
            status = RuntimeStatus(
                solution_pack="traffic",
                plan_path=plan_path,
                state_dir=Path(tmp) / "state",
                child=old_child,
                plan_loaded=True,
                revision=1,
                active_desired_hash="old",
            )
            snapshot = DesiredStateSnapshot(b"{}", "new", 2)

            with patch(
                "edge_runtime.runtime.solution_pack_entrypoint._start_child",
                return_value=new_child,
            ):
                applied = _apply_desired_state(
                    _args(), status, snapshot,
                    _Compiler(_plan(2, [{"camera_id": "cam1"}])),
                )

            self.assertTrue(applied)
            self.assertTrue(old_child.terminated)
            self.assertIs(new_child, status.child)
            self.assertEqual(2, status.revision)
            self.assertEqual("new", status.active_desired_hash)
            self.assertEqual(1, status.reload_applied)

    def test_failed_candidate_child_restores_previous_plan_and_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "traffic.runtime_plan.json"
            original = _plan(1, [{"camera_id": "old"}])
            plan_path.write_text(json.dumps(original), encoding="utf-8")
            old_child = _Child()
            failed_child = _Child(running=False)
            rollback_child = _Child()
            status = RuntimeStatus(
                solution_pack="traffic",
                plan_path=plan_path,
                state_dir=Path(tmp) / "state",
                child=old_child,
                plan_loaded=True,
                camera_count=1,
                revision=1,
                active_desired_hash="old",
            )
            snapshot = DesiredStateSnapshot(b"{}", "bad", 2)

            with patch(
                "edge_runtime.runtime.solution_pack_entrypoint._start_child",
                side_effect=[failed_child, rollback_child],
            ):
                applied = _apply_desired_state(
                    _args(), status, snapshot,
                    _Compiler(_plan(2, [{"camera_id": "new"}])),
                )

            self.assertFalse(applied)
            self.assertEqual(original, json.loads(plan_path.read_text(encoding="utf-8")))
            self.assertEqual(1, status.revision)
            self.assertEqual("old", status.active_desired_hash)
            self.assertIs(rollback_child, status.child)
            self.assertEqual("rejected", status.reload_state)
            self.assertEqual(1, status.reload_rejected)


if __name__ == "__main__":
    unittest.main()
