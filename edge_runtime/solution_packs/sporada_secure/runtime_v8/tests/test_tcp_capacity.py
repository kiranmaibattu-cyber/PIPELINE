from pathlib import Path
import sys
import threading
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "worker"))
from pipeline.dlstreamer_bridge import TcpMpegTsFeeder


class Source:
    def __init__(self):
        self.level = 1024
        self.maximum = 1024
        self.checked = threading.Event()

    def get_property(self, key):
        if key == "max-bytes":
            return self.maximum
        self.checked.set()
        return self.level


@pytest.mark.parametrize("cancel", [False, True])
def test_capacity_wait_is_bounded_and_cancellable(cancel):
    source = Source()
    feeder = TcpMpegTsFeeder(source, "127.0.0.1", 9001, camera_id="test")
    result, done = [], threading.Event()

    def wait():
        result.append(feeder._wait_for_capacity())
        done.set()

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        assert source.checked.wait(1)
        assert not done.wait(.03)
        if cancel:
            feeder._stop.set()
        else:
            source.level = 0
        assert done.wait(1)
        assert result == [not cancel]
    finally:
        feeder._stop.set()
        thread.join(1)
    assert not thread.is_alive()


def test_unbounded_transport_queue_is_rejected():
    source = Source()
    source.maximum = 0
    feeder = TcpMpegTsFeeder(source, "127.0.0.1", 9001, camera_id="test")
    with pytest.raises(RuntimeError, match="positive queue byte bound"):
        feeder._wait_for_capacity()
