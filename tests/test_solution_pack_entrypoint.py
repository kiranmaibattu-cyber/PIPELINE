from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from edge_runtime.runtime.solution_pack_entrypoint import (
    RuntimeStatus,
    _enrich_event,
    _is_runtime_api_path,
    _proxy_runtime_request,
    _resolve_snapshot_path,
)


class _RunningChild:
    def poll(self) -> None:
        return None


class _HeaderDict(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class SolutionPackEntrypointTest(unittest.TestCase):
    def test_event_snapshot_ref_gets_url(self) -> None:
        status = RuntimeStatus(
            solution_pack="surveillance",
            plan_path=Path("/plans/surveillance.runtime_plan.json"),
            state_dir=Path("/state/surveillance"),
        )
        event = _enrich_event(status, {
            "camera_id": "cam1",
            "event_type": "intrusion_alert",
            "snapshot_ref": "snapshots/cam1_event_1.jpg",
        })

        self.assertEqual("1.0", event["schema_version"])
        self.assertEqual("cam1", event["camera_id"])
        self.assertEqual("intrusion", event["application"])
        self.assertEqual("intrusion_event", event["event_type"])
        self.assertEqual("/snapshots/snapshots/cam1_event_1.jpg", event["payload"]["snapshot_url"])
        self.assertEqual("image/jpeg", event["payload"]["snapshot_content_type"])

    def test_nested_payload_snapshot_ref_gets_url(self) -> None:
        status = RuntimeStatus(
            solution_pack="traffic",
            plan_path=Path("/plans/traffic.runtime_plan.json"),
            state_dir=Path("/state/traffic"),
        )
        event = _enrich_event(status, {
            "payload": {
                "event_type": "anpr",
                "crop_path": "crops/plate_1.jpg",
            }
        })

        self.assertEqual("crops/plate_1.jpg", event["payload"]["snapshot_ref"])
        self.assertEqual("/snapshots/crops/plate_1.jpg", event["payload"]["snapshot_url"])

    def test_event_contract_redacts_camera_source(self) -> None:
        status = RuntimeStatus(
            solution_pack="traffic",
            plan_path=Path("/plans/traffic.runtime_plan.json"),
            state_dir=Path("/tmp/apexfabric/state/traffic"),
        )
        event = _enrich_event(status, {
            "camera_id": "cam4",
            "app_id": "anpr",
            "event_type": "plate_read_event",
            "timestamp_utc": "2026-08-20T10:00:00+05:30",
            "payload": {
                "plate": "KA52P1295",
                "source": "rtsp://admin:password@camera/stream",
            },
        })

        self.assertEqual("2026-08-20T04:30:00Z", event["timestamp"])
        self.assertEqual("anpr", event["application"])
        self.assertNotIn("source", event["payload"])

    def test_traffic_runtime_names_are_normalized_to_contract(self) -> None:
        status = RuntimeStatus(
            solution_pack="traffic",
            plan_path=Path("/plans/traffic.runtime_plan.json"),
            state_dir=Path("/state/traffic"),
        )
        event = _enrich_event(status, {
            "camera_id": "cam1",
            "use_case": "wrong_way_driving_detection",
            "type": "wrong_way",
        })

        self.assertEqual("wrong_way", event["application"])
        self.assertEqual("wrong_way_event", event["event_type"])

    def test_snapshot_path_is_resolved_inside_state_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state" / "surveillance"
            image = state / "snapshots" / "event.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"jpeg")
            status = RuntimeStatus(
                solution_pack="surveillance",
                plan_path=Path("/plans/surveillance.runtime_plan.json"),
                state_dir=state,
            )

            self.assertEqual(image, _resolve_snapshot_path(status, "snapshots/event.jpg"))
            self.assertIsNone(_resolve_snapshot_path(status, "../outside.jpg"))

    def test_surveillance_api_paths_are_proxied_only_for_surveillance(self) -> None:
        surveillance = RuntimeStatus(
            solution_pack="surveillance",
            plan_path=Path("/plans/surveillance.runtime_plan.json"),
            state_dir=Path("/state/surveillance"),
            runtime_api_url="http://127.0.0.1:8090",
        )
        traffic = RuntimeStatus(
            solution_pack="traffic",
            plan_path=Path("/plans/traffic.runtime_plan.json"),
            state_dir=Path("/state/traffic"),
            runtime_api_url="http://127.0.0.1:8091",
        )

        self.assertTrue(_is_runtime_api_path(surveillance, "/api/search?q=red%20shirt"))
        self.assertTrue(_is_runtime_api_path(surveillance, "/api/face_gallery/reload"))
        self.assertFalse(_is_runtime_api_path(surveillance, "/api/streams"))
        self.assertFalse(_is_runtime_api_path(traffic, "/api/search?q=red%20shirt"))

    def test_runtime_proxy_forwards_get_to_child_runtime(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps({"path": self.path}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status = RuntimeStatus(
                solution_pack="surveillance",
                plan_path=Path("/plans/surveillance.runtime_plan.json"),
                state_dir=Path("/state/surveillance"),
                child=_RunningChild(),
                runtime_api_url=f"http://127.0.0.1:{server.server_port}",
            )
            response = _proxy_runtime_request(
                status,
                "GET",
                "/api/search?q=person",
                _HeaderDict(),
                None,
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(200, response["status"])
        self.assertEqual({"path": "/api/search?q=person"}, json.loads(response["body"]))

    def test_runtime_proxy_returns_unavailable_when_child_is_down(self) -> None:
        status = RuntimeStatus(
            solution_pack="surveillance",
            plan_path=Path("/plans/surveillance.runtime_plan.json"),
            state_dir=Path("/state/surveillance"),
            runtime_api_url="http://127.0.0.1:8090",
        )

        response = _proxy_runtime_request(status, "GET", "/api/search", _HeaderDict(), None)

        self.assertEqual(503, response["status"])
        self.assertEqual("runtime_not_running", json.loads(response["body"])["error"])


if __name__ == "__main__":
    unittest.main()
