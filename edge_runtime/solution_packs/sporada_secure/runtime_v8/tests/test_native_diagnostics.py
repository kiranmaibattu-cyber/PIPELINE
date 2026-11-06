"""Offline diagnostic regressions; import only the two stdlib-only modules."""
import importlib.util
import io
import json
import logging
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


# Avoid importing worker/model dependencies or a different runtime's pipeline
# package when this file is collected alongside other runtime suites.
_PACKAGE = "_v8_native_diagnostics_pipeline"
_SOURCE = Path(__file__).resolve().parents[1] / "services/worker/pipeline"
package = ModuleType(_PACKAGE)
package.__path__ = [str(_SOURCE)]
sys.modules[_PACKAGE] = package


def load_module(name):
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE}.{name}", _SOURCE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


structured = load_module("structured_logging")
native = load_module("native_progress")
Trace = native.NativeProgressTrace


def formatted(message="diagnostic", **fields):
    record = logging.LogRecord("diagnostics", logging.INFO, __file__, 1, message, (), None)
    record.__dict__.update(fields)
    return json.loads(structured.JsonLogFormatter().format(record))


class StructuredLoggingTests(unittest.TestCase):
    def tearDown(self):
        structured.set_log_context()

    def test_nested_secret_keys_and_strings_are_redacted_without_mutation(self):
        source = {
            "stages": [{"password": "plain-password", "access_token": "access-value",
                        "apiKey": "api-value", "Authorization": "auth-value",
                        "secret_key": "secret-key-value", "private_key": "private-key-value",
                        "credentials": {"private": "nested-private"}}],
            "notes": ["Bearer bare-token", {"url": "rtsp://user:url-password@host/live"}],
            "tuple": ({"client_secret": "client-value"},),
        }
        payload = formatted(details=source, token="top-token")
        output = json.dumps(payload)
        for secret in ("plain-password", "access-value", "api-value", "auth-value",
                       "nested-private", "bare-token", "url-password", "client-value",
                       "top-token", "secret-key-value", "private-key-value"):
            self.assertNotIn(secret, output)
        self.assertEqual(payload["details"]["stages"][0]["apiKey"], "<redacted>")
        self.assertEqual(source["stages"][0]["password"], "plain-password")

    def test_quoted_and_escaped_json_secrets(self):
        values = {
            "password": 'space, comma and "quoted" password-tail',
            "refresh_token": "refresh-tail",
            "api_key": "api-tail",
            "Authorization": "Basic opaque-auth-tail",
            "url": "https://username:url-tail@camera/path?token=query-tail",
        }
        encoded = json.dumps(values)
        for diagnostic in (encoded, repr(values), json.dumps(encoded),
                           json.dumps(json.dumps(encoded)), "request=" + encoded):
            with self.subTest(diagnostic=diagnostic):
                output = json.dumps(formatted(details=[{"raw": diagnostic}]))
                for secret in ("password-tail", "refresh-tail", "api-tail",
                               "opaque-auth-tail", "url-tail", "query-tail"):
                    self.assertNotIn(secret, output)

    def test_bare_bearer_and_url_schemes(self):
        for value in ("bearer opaque-token", "BEARER opaque-token==; next=ok",
                      "rtsp://user:opaque-token@host/live",
                      "rtsps://user:opaque-token@host/live",
                      "https://user:opaque-token@host/path",
                      "wss://user:opaque-token@host/path",
                      r'https:\/\/user:opaque-token@host/path'):
            with self.subTest(value=value):
                self.assertNotIn("opaque-token", structured.sanitize_text(value))

    def test_telemetry_types_and_fields_survive(self):
        telemetry = {"count": 7, "age_seconds": 2.125, "pts_ns": 123456789,
                     "enabled": True, "dropped": False, "unknown": None}
        result = formatted(stages={"decoder.src": telemetry})["stages"]["decoder.src"]
        self.assertEqual(result, telemetry)
        for key in telemetry:
            self.assertIs(type(result[key]), type(telemetry[key]))

    def test_unterminated_and_truncated_quoted_secrets_do_not_leak_tails(self):
        for text in ('password="first secret-tail', 'password="first, secret-tail',
                     r'"token":\"first secret-tail',
                     'prefix {"password":"first, secret-tail ' + "x" * 10000):
            with self.subTest(text=text[:80]):
                output = structured.sanitize_text(text, limit=80)
                self.assertNotIn("first", output)
                self.assertNotIn("secret-tail", output)
                self.assertLessEqual(len(output), 80)

    def test_context_explicit_fields_keys_and_exception_are_safe(self):
        structured.set_log_context("cam-1", "session-1")
        payload = formatted()
        self.assertEqual((payload["camera_id"], payload["stream_session_id"]),
                         ("cam-1", "session-1"))
        payload = formatted(camera_id="rtsp://user:context-secret@host/live",
                            stream_session_id="Bearer session-secret",
                            event='{"password":"event-secret"}',
                            details={"https://u:key-secret@host": "safe"})
        try:
            raise RuntimeError('failure {"password":"exception-secret"}')
        except RuntimeError:
            record = logging.LogRecord("test", logging.ERROR, __file__, 1,
                                       "Bearer message-secret", (), sys.exc_info())
        output = json.dumps(payload) + structured.JsonLogFormatter().format(record)
        for secret in ("context-secret", "session-secret", "event-secret", "key-secret",
                       "exception-secret", "message-secret"):
            self.assertNotIn(secret, output)

    def test_cycles_depth_width_and_strings_are_bounded(self):
        cycle = {}
        cycle["self"] = cycle
        result = formatted(cycle=cycle, long="x" * 10000,
                           wide=list(range(10000)),
                           tree={str(i): list(range(10000)) for i in range(100)})
        self.assertIn("<truncated>", json.dumps(result))
        self.assertLessEqual(len(result["long"]), 4096)
        self.assertLessEqual(len(result["wide"]), 65)
        self.assertLess(len(json.dumps(result)), 20000)


class FakePad:
    def __init__(self):
        self.callbacks = []

    def add_probe(self, probe_type, callback):
        self.callbacks.append(callback)

    def emit(self, pts=None):
        buffer = SimpleNamespace(pts=pts) if pts is not None else None
        return [callback(self, SimpleNamespace(get_buffer=lambda: buffer))
                for callback in self.callbacks]


class FakeElement:
    def __init__(self, directions=("sink", "src")):
        self.pads = {direction: FakePad() for direction in directions}

    def get_static_pad(self, direction):
        return self.pads.get(direction)

    def get_property(self, name):
        raise AssertionError("native property queried by diagnostics")

    def find_property(self, name):
        raise AssertionError("native property queried by diagnostics")


class FakePipeline:
    def __init__(self, elements):
        self.elements = elements
        self.lookups_allowed = True

    def get_by_name(self, name):
        if not self.lookups_allowed:
            raise AssertionError("native lookup after attachment")
        return self.elements.get(name)


GST = SimpleNamespace(PadProbeType=SimpleNamespace(BUFFER=1),
                      PadProbeReturn=SimpleNamespace(OK=object()))


class NativeProgressTests(unittest.TestCase):
    def test_in_memory_snapshots_counts_ages_pts_and_phases(self):
        now = [100.0]
        with patch.object(native.time, "monotonic", side_effect=lambda: now[0]):
            trace = Trace("cam", "session")
            self.addCleanup(trace.close)
            self.assertEqual(trace.snapshot()["stages"], {})
            now[0] = 101.0
            trace.mark("decoder.src", 120)
            trace.phase("await_primary_output")
            now[0] = 102.0
            trace.mark("decoder.src", 240)
            trace.mark("primarydet.sink", 240)
            now[0] = 105.5
            snapshot = trace.snapshot()
            self.assertEqual(snapshot["camera_phase"], "await_primary_output")
            self.assertEqual(snapshot["phase_age_seconds"], 4.5)
            self.assertEqual(snapshot["stages"]["decoder.src"],
                             {"count": 2, "age_seconds": 3.5, "pts_ns": 240})
            snapshot["stages"]["decoder.src"]["count"] = 999
            now[0] = 110.0
            later = trace.snapshot()
            self.assertEqual(later["stages"]["decoder.src"]["count"], 2)
            self.assertEqual(later["stages"]["decoder.src"]["age_seconds"], 8.0)
            self.assertEqual(set(later), {"camera_id", "stream_session_id", "camera_phase",
                                         "phase_age_seconds", "stages"})
            self.assertNotIn("cause", json.dumps(later))
            self.assertNotIn("stalled", json.dumps(later))

    def test_named_probes_only_observe_real_buffers_and_attach_once(self):
        names = ("videoparser", "decoder", "postproc", "fpsgate", "policygate",
                 "objecttracker", "primarydet", "facedet", "faceclass",
                 "firefpsgate", "firedet", "vasink", "firesink")
        pipeline = FakePipeline({name: FakeElement() for name in names})
        trace = Trace("cam", "session")
        self.addCleanup(trace.close)
        trace.attach(pipeline, GST)
        trace.attach(pipeline, GST)
        pipeline.lookups_allowed = False
        initial = trace.snapshot()["stages"]
        for name in names:
            directions = ("sink",) if name in ("vasink", "firesink") else ("sink", "src")
            for direction in directions:
                stage = f"{name}.{direction}"
                self.assertEqual(initial[stage],
                                 {"count": 0, "age_seconds": None, "pts_ns": None})
                pad = pipeline.elements[name].pads[direction]
                self.assertEqual(len(pad.callbacks), 1)
                self.assertEqual(pad.emit(), [GST.PadProbeReturn.OK])
                self.assertEqual(trace.snapshot()["stages"][stage]["count"], 0)
                self.assertEqual(pad.emit(400), [GST.PadProbeReturn.OK])
                self.assertEqual(trace.snapshot()["stages"][stage]["count"], 1)
                self.assertEqual(trace.snapshot()["stages"][stage]["pts_ns"], 400)

    def test_missing_elements_and_pads_and_no_native_queries_in_snapshot(self):
        pipeline = FakePipeline({"decoder": FakeElement(("src",)),
                                 "tcpappsrc": FakeElement()})
        trace = Trace("cam", "session")
        self.addCleanup(trace.close)
        trace.attach(pipeline, GST)
        pipeline.lookups_allowed = False
        self.assertEqual(set(trace.snapshot()["stages"]), {"decoder.src"})
        trace.log_snapshot()

    def test_attached_probe_hot_path_skips_sanitization_and_int_conversion(self):
        class NativePts(int):
            def __int__(self):
                raise AssertionError("already-integer PTS was converted")

        pipeline = FakePipeline({"decoder": FakeElement()})
        trace = Trace("cam", "session")
        self.addCleanup(trace.close)
        trace.attach(pipeline, GST)
        with patch.object(native, "sanitize_text", side_effect=AssertionError(
            "registered stage was sanitized on the hot path"
        )):
            for pts in (100, NativePts(200)):
                for pad in pipeline.elements["decoder"].pads.values():
                    self.assertEqual(pad.emit(pts), [GST.PadProbeReturn.OK])
        for stage in trace.snapshot()["stages"].values():
            self.assertEqual(stage["count"], 2)
            self.assertEqual(stage["pts_ns"], 200)

    def test_new_stage_sanitized_once_and_external_pts_stay_numeric(self):
        trace = Trace("cam", "session")
        self.addCleanup(trace.close)
        with patch.object(native, "sanitize_text", wraps=native.sanitize_text) as sanitize:
            trace.mark("camera.consumed", "123")
            trace.mark("camera.consumed", 456)
            trace.mark("camera.consumed")
        sanitize.assert_called_once_with("camera.consumed", limit=128)
        self.assertEqual(trace.snapshot()["stages"]["camera.consumed"]["count"], 3)
        self.assertIsNone(trace.snapshot()["stages"]["camera.consumed"]["pts_ns"])
        trace.mark("camera.consumed", "789")
        self.assertIs(type(trace.snapshot()["stages"]["camera.consumed"]["pts_ns"]), int)

    def test_default_and_explicit_report_intervals_without_sleeping(self):
        for interval in (30.0, 2.0):
            with self.subTest(interval=interval):
                trace = Trace("cam", "session") if interval == 30 else Trace("cam", "session", 2)
                stop = SimpleNamespace(wait=unittest.mock.Mock(side_effect=[False, True]))
                with patch.object(trace, "_stop", stop), patch.object(trace, "log_snapshot") as log:
                    trace._report()
                self.assertEqual(stop.wait.call_args_list,
                                 [unittest.mock.call(interval), unittest.mock.call(interval)])
                log.assert_called_once_with()
                trace.close()

    def test_invalid_intervals_are_rejected(self):
        for interval in (0, -1, float("inf"), float("nan")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                Trace("cam", "session", interval)

    def test_stage_storage_is_bounded_and_existing_counts_continue(self):
        trace = Trace("cam", "session")
        self.addCleanup(trace.close)
        trace.mark("decoder.src", 0)
        for index in range(1000):
            trace.mark(f"stage-{index}", index)
        trace.mark("decoder.src", 10)
        stages = trace.snapshot()["stages"]
        self.assertEqual(len(stages), trace.MAX_STAGES)
        self.assertEqual(stages["decoder.src"]["count"], 2)
        self.assertEqual(stages["decoder.src"]["pts_ns"], 10)

    def test_concurrent_observers_do_not_lose_counts(self):
        trace = Trace("cam", "session")
        self.addCleanup(trace.close)

        def observe():
            for pts in range(500):
                trace.mark("decoder.src", pts)

        workers = [threading.Thread(target=observe) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(trace.snapshot()["stages"]["decoder.src"]["count"], 2000)

    def test_concurrent_new_stages_preserve_limit_and_registered_counts(self):
        trace = Trace("cam", "session")
        self.addCleanup(trace.close)
        trace.mark("decoder.src", 0)
        start = threading.Barrier(4)

        def observe(worker_id):
            start.wait(timeout=2)
            for pts in range(100):
                trace.mark(f"worker-{worker_id}-{pts}", pts)
                trace.mark("decoder.src", pts)

        workers = [threading.Thread(target=observe, args=(index,)) for index in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        stages = trace.snapshot()["stages"]
        self.assertEqual(len(stages), trace.MAX_STAGES)
        self.assertEqual(stages["decoder.src"]["count"], 401)

    def test_reporting_continues_without_camera_calls_and_close_stops_thread(self):
        trace = Trace("cam", "session", interval_seconds=0.01)
        snapshots = []
        reported = threading.Event()

        def report():
            snapshots.append(trace.snapshot())
            if len(snapshots) >= 2:
                reported.set()

        trace.mark("decoder.src", 123)
        with patch.object(trace, "log_snapshot", side_effect=report):
            try:
                trace.attach(FakePipeline({}), GST)
                self.assertTrue(reported.wait(2), "reporter stopped with camera calls")
            finally:
                trace.close()
        self.assertFalse(trace._thread.is_alive())
        self.assertGreater(snapshots[1]["stages"]["decoder.src"]["age_seconds"],
                           snapshots[0]["stages"]["decoder.src"]["age_seconds"])
        self.assertEqual(snapshots[1]["stages"]["decoder.src"]["count"], 1)
        trace.close()

    def test_timeout_log_snapshot_has_camera_session_and_redacts_values(self):
        trace = Trace("cam", "session")
        self.addCleanup(trace.close)
        trace.mark("decoder.src", 33)
        trace.phase('await output {"password":"phase-secret"}')
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(structured.JsonLogFormatter())
        logger = logging.getLogger(native.__name__)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            snapshot = trace.log_snapshot(event="native_timeout_snapshot")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
            handler.close()
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["camera_id"], "cam")
        self.assertEqual(payload["stream_session_id"], "session")
        self.assertEqual(payload["event"], "native_timeout_snapshot")
        self.assertEqual(payload["stages"], snapshot["stages"])
        self.assertNotIn("phase-secret", stream.getvalue())
        self.assertNotIn("phase-secret", json.dumps(snapshot))

    def test_raw_urls_and_bearer_are_not_stored_in_trace(self):
        trace = Trace("rtsp://user:camera-secret@host/live", "Bearer session-secret")
        self.addCleanup(trace.close)
        trace.mark("https://user:stage-secret@host/live", 1)
        trace.phase("Bearer phase-secret")
        output = json.dumps(trace.snapshot())
        for secret in ("camera-secret", "session-secret", "stage-secret", "phase-secret"):
            self.assertNotIn(secret, output)

    def test_close_before_attach_does_not_start_thread(self):
        trace = Trace("cam", "session")
        trace.close()
        trace.attach(FakePipeline({}), GST)
        trace.mark("decoder.src", 1)
        self.assertFalse(trace._thread.is_alive())
        self.assertEqual(trace.snapshot()["stages"], {})


if __name__ == "__main__":
    unittest.main()
