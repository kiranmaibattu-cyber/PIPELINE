from pathlib import Path
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "worker"))
from pipeline.dlstreamer_bridge import DlsMotionPolicyGate, AdmittedFrame


def gate():
    return DlsMotionPolicyGate(None, SimpleNamespace(set_gauge=lambda *args: None),
                              object(), 8, 1000000000, maximum_pending=2)


def record(n):
    return AdmittedFrame(n, n * 100, 1000.0 + n, 1000.0 + n, "TRACKING", "active")


def test_reservation_waits_before_inference_and_releases_on_consumption():
    g = gate()
    assert g._reserve(record(1)) and g._reserve(record(2))
    started, finished = threading.Event(), threading.Event()
    accepted = []

    def producer():
        started.set()
        accepted.append(g._reserve(record(3)))
        finished.set()

    t = threading.Thread(target=producer)
    t.start()
    try:
        assert started.wait(1)
        assert not finished.wait(.05)
        assert g.pending_admissions == 2
        assert g.take(100) == record(1)
        assert finished.wait(1)
        assert accepted == [True]
        assert g.pending_admissions == 2
        assert g.take(200) == record(2)
        assert g.take(300) == record(3)
    finally:
        g.close()
        t.join(1)
    assert not t.is_alive()


def test_reset_discards_waiting_old_epoch_without_reassigning_timestamp():
    g = gate()
    g._reserve(record(1)); g._reserve(record(2))
    result = []
    t = threading.Thread(target=lambda: result.append(g._reserve(record(3))))
    t.start()
    # Wait until the condition wait has released its lock.
    import time
    time.sleep(.05)
    g.reset_session(object())
    t.join(1)
    assert not t.is_alive()
    assert result == [False]
    assert g.pending_admissions == 0
    assert g._reserve(record(4))
    assert g.take(400).observed_at == 1004.0


def test_close_releases_blocked_upstream_producer():
    g = gate()
    g._reserve(record(1)); g._reserve(record(2))
    result = []
    t = threading.Thread(target=lambda: result.append(g._reserve(record(3))))
    t.start()
    g.close()
    t.join(1)
    assert not t.is_alive()
    assert result == [False]
