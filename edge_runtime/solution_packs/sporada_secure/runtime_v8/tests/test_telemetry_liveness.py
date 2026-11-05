import json
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services' / 'worker'))
from pipeline.runtime_telemetry import CameraTelemetry


def test_reporting_continues_when_camera_stops_making_calls(tmp_path, monkeypatch):
    monkeypatch.setenv('APEXFABRIC_STATE_ROOT', str(tmp_path))
    telemetry = CameraTelemetry('blocked-camera', target_fps=10, interval_seconds=.5)
    try:
        telemetry.count('decoded_frames', 5)
        telemetry.count('inference_frames', 5)
        first = telemetry.flush(force=True)
        assert first['processing']['inference_fps'] > 0
        # No subsequent camera call: model execution could be blocked.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            latest = json.loads(telemetry.path.read_text())
            if latest['observed_at'] != first['observed_at'] and latest['processing']['inference_fps'] == 0:
                break
            time.sleep(.02)
        else:
            raise AssertionError('reporting froze with camera processing')
        assert latest['processing']['totals']['inference_frames'] == 5
        assert latest['processing']['decoded_fps'] == 0
    finally:
        telemetry.close()
    assert not telemetry._reporter.is_alive()


def test_concurrent_flushes_do_not_lose_counts_or_race_atomic_file(tmp_path, monkeypatch):
    monkeypatch.setenv('APEXFABRIC_STATE_ROOT', str(tmp_path))
    telemetry = CameraTelemetry('flush-race', target_fps=10, interval_seconds=.5)
    errors = []
    def produce():
        try:
            for _ in range(30):
                telemetry.count('inference_frames')
                telemetry.flush(force=True)
        except Exception as exc:
            errors.append(exc)
    workers = [threading.Thread(target=produce) for _ in range(3)]
    try:
        for worker in workers: worker.start()
        for worker in workers: worker.join(3)
        assert not any(worker.is_alive() for worker in workers)
        assert not errors
        last = telemetry.flush(force=True)
        assert last['processing']['totals']['inference_frames'] == 90
        assert json.loads(telemetry.path.read_text()) == last
    finally:
        telemetry.close()
