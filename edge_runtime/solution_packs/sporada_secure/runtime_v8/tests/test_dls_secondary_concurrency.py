"""Result ownership and timeout regressions for shared secondary execution."""
from collections import deque
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "worker"))
from pipeline.dls_secondary import DlsRawImageInferencer, _InferenceJob, _SharedDlsInferencerHandle


def engine_stub():
    engine = DlsRawImageInferencer.__new__(DlsRawImageInferencer)
    engine._condition = threading.Condition()
    engine._owner_queues = {}
    engine._owner_order = deque()
    engine._last_metrics = {}
    engine._closed = False
    engine._sequence = 0
    engine._trace_jobs = False
    engine.name = "test"
    return engine


def test_two_lanes_overlap_without_crossing_results():
    engine = engine_stub()
    barrier = threading.Barrier(2)
    first = _InferenceJob("camera-a", np.full((1, 1, 3), 11))
    second = _InferenceJob("camera-b", np.full((1, 1, 3), 22))
    engine._owner_queues = {"camera-a": deque([first]), "camera-b": deque([second])}
    engine._owner_order = deque(["camera-a", "camera-b"])
    sequences = []

    def infer(image, *, lane, sequence):
        sequences.append(sequence)
        barrier.wait(timeout=2)  # Fails if submission accidentally serializes.
        return image.copy()

    engine._infer_direct = infer
    workers = [threading.Thread(target=engine._run, args=(SimpleNamespace(index=i),))
               for i in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert first.completed.wait(3) and second.completed.wait(3)
        assert first.error is None and second.error is None
        assert first.result[0, 0, 0] == 11
        assert second.result[0, 0, 0] == 22
        assert sorted(sequences) == [0, 1]
    finally:
        with engine._condition:
            engine._closed = True
            engine._condition.notify_all()
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()


def test_expired_queued_job_never_reaches_model():
    engine = engine_stub()
    job = _InferenceJob("camera-old", np.zeros((1, 1, 3)))
    job.cancelled.set()
    jobs = iter([job, None])
    engine._take_job = lambda: next(jobs)
    engine._infer_direct = lambda *a, **k: pytest.fail("expired job was executed")
    engine._run()
    assert job.completed.is_set()
    assert isinstance(job.error, TimeoutError)


def test_expired_inflight_job_does_not_publish_result():
    engine = engine_stub()
    job = _InferenceJob("camera-old", np.zeros((1, 1, 3)))
    jobs = iter([job, None])
    engine._take_job = lambda: next(jobs)

    def infer(*args, **kwargs):
        job.cancelled.set()
        return np.ones(4)

    engine._infer_direct = infer
    engine._run()
    assert job.completed.is_set()
    assert job.result is None


def test_closed_camera_handle_rejects_completion():
    engine = SimpleNamespace(actual_device="test")
    handle = _SharedDlsInferencerHandle(("fixture",), engine, "camera-session-old")

    def infer(*args, **kwargs):
        handle._closed = True  # Camera/session retired while inference ran.
        return np.ones(2)

    engine.infer = infer
    with pytest.raises(RuntimeError, match="closed before completion"):
        handle.infer(np.zeros((1, 1, 3)))


def sample(pts, tensors):
    return SimpleNamespace(
        get_buffer=lambda: SimpleNamespace(pts=pts, tensors=tensors),
        get_caps=lambda: None,
    )


def native_stub(monkeypatch, samples):
    engine = engine_stub()
    engine.timeout_seconds = .1
    engine.expected_elements = 2
    engine.Gst = SimpleNamespace(
        SECOND=1000000000, FlowReturn=SimpleNamespace(OK=0),
        MessageType=SimpleNamespace(ERROR=1),
        Buffer=SimpleNamespace(new_allocate=lambda *args: SimpleNamespace(fill=lambda *args: None)),
    )
    engine._lanes = [SimpleNamespace(
        source=SimpleNamespace(emit=lambda *args: 0),
        sink=SimpleNamespace(emit=lambda *args: samples.pop(0) if samples else None),
        pipeline=SimpleNamespace(get_bus=lambda: SimpleNamespace(pop_filtered=lambda *args: None)),
    )]
    monkeypatch.setitem(sys.modules, "gstgva", SimpleNamespace(
        VideoFrame=lambda buffer, **kwargs: SimpleNamespace(tensors=lambda: buffer.tensors)))
    return engine


def test_late_tensor_is_drained_not_assigned_to_next_crop(monkeypatch):
    old = SimpleNamespace(data=lambda: np.array([11, 11]))
    current = SimpleNamespace(data=lambda: np.array([22, 22]))
    engine = native_stub(monkeypatch, [sample(1000000000, [old]), sample(2000000000, [current])])
    result = engine._infer_direct(np.zeros((1, 1, 3)), sequence=2)
    assert result.tolist() == [22, 22]


@pytest.mark.parametrize("output, message", [
    (sample(3000000000, []), "PTS is ahead"),
    (sample(2000000000, []), "no output tensor"),
    (sample(2000000000, [SimpleNamespace(data=lambda: np.ones(3))]), "size mismatch"),
])
def test_invalid_native_results_fail_explicitly(monkeypatch, output, message):
    engine = native_stub(monkeypatch, [output])
    with pytest.raises(RuntimeError, match=message):
        engine._infer_direct(np.zeros((1, 1, 3)), sequence=2)
