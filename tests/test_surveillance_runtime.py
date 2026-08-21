from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


RUNTIME_ROOT = (
    Path(__file__).resolve().parents[1]
    / "edge_runtime/solution_packs/surveillance/runtime_8090"
)
sys.path.insert(0, str(RUNTIME_ROOT))

from PLATF.face_enroll_gallery import Gallery, Vec  # noqa: E402
from PLATF.plugins.analytics import CountingPlugin  # noqa: E402
from PLATF.plugins.enroll_gallery import EnrollmentGalleryAdapter  # noqa: E402
from PLATF.plugins.zones import zones_from_dict  # noqa: E402

import numpy as np  # noqa: E402


class _Context:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def _observation(x: float, y: float, t: float, local_id: int = 1):
    return SimpleNamespace(
        camera="cam1",
        local_id=local_id,
        person_id=local_id,
        foot_point=(x, y),
        meta={"frame_wh": [100, 100]},
        t=t,
    )


class SurveillanceRuntimeTest(unittest.TestCase):
    def test_empty_face_gallery_loads_and_person_can_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gallery = Gallery(root)
            gallery.save()
            adapter = EnrollmentGalleryAdapter.load(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(0, adapter.status()["person_count"])

            gallery.vecs.append(Vec(
                person="Alice", cell="frontal", mode=0,
                vec=np.ones(512, dtype=np.float32) / np.sqrt(512),
                quality=1.0, chip_path="", source="platform",
            ))
            gallery.save()
            adapter.reload_if_changed(force=True)
            deleted = adapter.delete_person("Alice")
            self.assertEqual("Alice", deleted["deleted"])
            self.assertEqual(0, deleted["person_count"])

    def test_people_counting_without_line_emits_occupancy(self) -> None:
        plugin = CountingPlugin(zones_from_dict({"frame": [100, 100]}))
        ctx = _Context()
        plugin.process(_observation(20, 80, 1.0), None, ctx)
        plugin.process(_observation(40, 80, 1.0, local_id=2), None, ctx)
        plugin.on_tick(1.0, ctx)

        self.assertEqual(1, len(ctx.events))
        self.assertEqual({"mode": "occupancy", "count": 2}, ctx.events[0].payload)

        plugin._active_ttl_s = 0.0
        time.sleep(0.001)
        plugin.on_idle(ctx)
        self.assertEqual({"mode": "occupancy", "count": 0}, ctx.events[-1].payload)

    def test_people_counting_with_line_emits_in_out_tally(self) -> None:
        cfg = zones_from_dict({
            "frame": [100, 100],
            "cameras": {"cam1": {"lines": [{
                "name": "door", "a": [0.1, 0.5], "b": [0.9, 0.5],
                "in_side": "right",
            }]}}
        })
        plugin = CountingPlugin(cfg)
        ctx = _Context()
        plugin.process(_observation(50, 60, 1.0), None, ctx)
        plugin.process(_observation(50, 40, 2.0), None, ctx)

        self.assertEqual(1, len(ctx.events))
        self.assertEqual("line_crossing", ctx.events[0].payload["mode"])
        self.assertEqual("in", ctx.events[0].payload["direction"])
        self.assertEqual({"in": 1, "out": 0}, ctx.events[0].payload["tally"])


if __name__ == "__main__":
    unittest.main()
