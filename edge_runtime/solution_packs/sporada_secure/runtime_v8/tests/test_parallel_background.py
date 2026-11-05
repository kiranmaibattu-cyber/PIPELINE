from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/worker'))
from pipeline import parallel_background as pb
from pipeline.person_reid import OpenVINOGaitExtractor


@pytest.fixture(autouse=True)
def single_opencv_thread(monkeypatch):
    old = cv2.getNumThreads()
    cv2.setNumThreads(1)
    monkeypatch.setattr(pb, '_cpu_capacity', lambda: 4)
    yield
    cv2.setNumThreads(old)


@pytest.mark.parametrize('dtype', [np.uint8, np.float32])
def test_foreground_and_background_are_exact_across_shape_changes(dtype):
    original = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)
    candidate = pb.RowPartitionedMog2(workers=4, minimum_pixels=0)
    rng = np.random.default_rng(80)
    try:
        assert candidate.getBackgroundImage() is None
        for i in range(80):
            shape = (127, 193, 3) if i < 40 else (91, 137, 3)
            frame = rng.integers(0, 256, shape, np.uint8).astype(dtype)
            if i % 2:
                frame = frame[:, ::-1]  # Non-contiguous source pixels.
            rate = [0, .02, 1, -1][i % 4]
            a = original.apply(frame, learningRate=rate)
            b = candidate.apply(frame, learningRate=rate)
            np.testing.assert_array_equal(a, b)
            np.testing.assert_array_equal(original.getBackgroundImage(), candidate.getBackgroundImage())
    finally:
        candidate.close()


def test_shared_pool_keeps_independent_camera_models_and_closes_last_owner():
    candidates = [pb.RowPartitionedMog2(workers=4, minimum_pixels=0) for _ in range(2)]
    originals = [cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as cameras:
            for i in range(30):
                frames = [np.full((97, 161, 3), (i * k) % 255, np.uint8) for k in [3, 19]]
                jobs = [cameras.submit(c.apply, f) for c, f in zip(candidates, frames)]
                for original, frame, job in zip(originals, frames, jobs):
                    np.testing.assert_array_equal(original.apply(frame), job.result())
        assert candidates[0]._pool is candidates[1]._pool
        key = candidates[0]._pool_key
        assert pb._pools[key][1] == 2
        candidates[0].close()
        assert pb._pools[key][1] == 1
        candidates[1].apply(np.zeros((97, 161, 3), np.uint8))
        candidates[1].close()
        assert key not in pb._pools
    finally:
        for c in candidates:
            c.close()


def test_small_frames_use_native_state_without_a_worker_pool():
    c = pb.RowPartitionedMog2(workers=4)
    original = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)
    try:
        for value in [0, 80, 12, 120]:
            frame = np.full((72, 128, 3), value, np.uint8)
            np.testing.assert_array_equal(c.apply(frame), original.apply(frame))
            np.testing.assert_array_equal(c.getBackgroundImage(), original.getBackgroundImage())
        assert c.last_partitions == 1 and c._pool is None
    finally:
        c.close()


def test_worker_failure_drains_all_writers_and_poisoned_state_is_not_reused():
    c = pb.RowPartitionedMog2(workers=4, minimum_pixels=0)
    frame = np.zeros((80, 120, 3), np.uint8)
    c.apply(frame)
    submitted = []
    real_submit = c._pool.submit
    def traced(*args):
        job = real_submit(*args)
        submitted.append(job)
        return job
    c._pool.submit = traced
    class Broken:
        def apply(self, *args):
            raise ValueError('injected writer failure')
    c._models[1] = Broken()
    try:
        with pytest.raises(ValueError, match='injected writer'):
            c.apply(frame)
        assert len(submitted) == 4 and all(f.done() for f in submitted)
        with pytest.raises(RuntimeError, match='failed'):
            c.apply(frame)
    finally:
        c.close()


def test_factory_rollback_and_single_core_guard(monkeypatch):
    monkeypatch.setenv('SENTINEL_GAIT_MOG_PARTITIONS', '1')
    assert not isinstance(pb.create_gait_background(), pb.RowPartitionedMog2)
    monkeypatch.setenv('SENTINEL_GAIT_MOG_PARTITIONS', '4')
    monkeypatch.setattr(pb, '_cpu_capacity', lambda: 1)
    assert not isinstance(pb.create_gait_background(), pb.RowPartitionedMog2)


def test_factory_avoids_nested_opencv_parallelism(monkeypatch):
    monkeypatch.setenv('SENTINEL_GAIT_MOG_PARTITIONS', '4')
    monkeypatch.setattr(pb.cv2, 'getNumThreads', lambda: 2)
    assert not isinstance(pb.create_gait_background(), pb.RowPartitionedMog2)


def test_cpu_limit_respects_affinity_and_container_quota(monkeypatch, tmp_path):
    from pipeline.resource_metrics import CgroupSampler
    monkeypatch.undo()  # Test the real capacity resolver, not the fixture stub.
    monkeypatch.setattr(pb.os, 'sched_getaffinity', lambda unused: set(range(6)))
    monkeypatch.setattr(CgroupSampler, '_process_cgroup', staticmethod(lambda unused: tmp_path))
    for quota, expected in [('max 100000', 6), ('250000 100000', 2), ('50000 100000', 1)]:
        (tmp_path / 'cpu.max').write_text(quota)
        assert pb._cpu_capacity() == expected


def test_gait_sequences_and_model_inputs_are_unchanged(monkeypatch):
    import pipeline.person_reid as module
    class Model:
        def __init__(self):
            self.inputs = []
        def __call__(self, inputs):
            self.inputs.append(np.asarray(inputs[0]).copy())
            return {0: np.arange(1, 4097, dtype=np.float32)[None]}
    def make():
        c = OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
        c.backgrounds, c.buffers, c.buffer_times, c.cache, c.centers = {}, defaultdict(list), defaultdict(list), {}, {}
        c.minimum_motion_pixels, c.max_sequence, c.min_sequence, c.sequence_length = .5, 30, 4, 30
        c.gait, c.gait_output = Model(), 0
        return c
    a, b = make(), make()
    native = lambda: cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)
    split = lambda: pb.RowPartitionedMog2(workers=4, minimum_pixels=0)
    try:
        for i in range(40):
            frame = np.zeros((120, 160, 3), np.uint8)
            cv2.rectangle(frame, (i * 2 % 90, 5), (i * 2 % 90 + 40, 110), (190, 80, 210), -1)
            people = [SimpleNamespace(bbox=[i % 20, 0, 70 + i % 20, 119], metadata={'track_id': 1}),
                      SimpleNamespace(bbox=[60 + i % 20, 0, 159, 119], metadata={'track_id': 2})]
            for factory, extractor in [(native, a), (split, b)]:
                monkeypatch.setattr(module, 'create_gait_background', factory)
                for person in people:
                    extractor.collect(frame, [person], 'camera', {1: 'p1', 2: 'p2'}, captured_at=100 + i / 8,
                                      frame_key=('session', i), accept_sequence=lambda tid, n: n >= 4 and i % 3 == 0)
                extractor.release_frame('camera', ('session', i))
            for key in a.buffers:
                np.testing.assert_array_equal(a.buffers[key], b.buffers[key])
            assert a.buffer_times == b.buffer_times
            np.testing.assert_array_equal(a.backgrounds['camera'].getBackgroundImage(), b.backgrounds['camera'].getBackgroundImage())
        assert a.gait.inputs and len(a.gait.inputs) == len(b.gait.inputs)
        for x, y in zip(a.gait.inputs, b.gait.inputs):
            np.testing.assert_array_equal(x, y)
        for key in a.cache:
            np.testing.assert_array_equal(a.cache[key], b.cache[key])
    finally:
        a.close(); b.close()
