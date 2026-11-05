"""Connection-only probe of the baked native camera trust and shared-bin hooks.

No models, CPU video decoding or production certificate bypass is involved.
The qualification fixture deliberately returns RTSP404 after successful TLS.
"""
import argparse
import json
import sys
import time

sys.path.insert(0, "/opt/traffic-pilot/services/worker")
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
from pipeline.source_tls import CameraSourceTls, CameraTlsConfigurationError
from pipeline.structured_logging import configure_structured_logging, sanitize_text
from pipeline.multi_camera_gst import MultiCameraGstPipeline

Gst.init([])
configure_structured_logging()
parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--mode", choices=["single", "shared", "bad-leader"], default="single")
args = parser.parse_args()
policy = CameraSourceTls("camera-04", "tls-validation-session")
description = (
    "rtspsrc name=camerasource protocols=tcp latency=100 tcp-timeout=3000000 ! "
    "application/x-rtp,media=video ! "
    "fakesink name=videosink sync=false signal-handoffs=true"
)
report = {"mode": args.mode, "rtp_received": False, "errors": []}
configured_states = []


def configure(branch):
    source = branch.get_by_name("camerasource")
    configured_states.append(int(source.get_state(0)[1]))
    source.set_property("location", args.url)
    if args.url.startswith("rtsps://"):
        policy.configure(source)


coordinator = None
pipeline = None
peer_buffers = []
try:
    if args.mode == "single":
        pipeline = Gst.parse_launch(description)
        configure(pipeline)
    else:
        coordinator = MultiCameraGstPipeline(["camera-04", "peer"])
        if args.mode == "bad-leader":
            try:
                coordinator.create_branch("camera-04", description, configure=configure)
            except CameraTlsConfigurationError:
                report["bad_leader_configuration_rejected"] = True
            else:
                raise AssertionError("invalid leader configuration was accepted")
        peer = coordinator.create_branch(
            "peer", "videotestsrc is-live=true pattern=black ! "
            "video/x-raw,width=16,height=16,framerate=5/1 ! "
            "fakesink name=peersink sync=true signal-handoffs=true",
        )
        peer.get_by_name("peersink").connect(
            "handoff", lambda *_: peer_buffers.append(time.monotonic())
        )
        assert peer.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        if args.mode != "bad-leader":
            pipeline = coordinator.create_branch("camera-04", description, configure=configure)
    if pipeline is not None:
        pipeline.get_by_name("videosink").connect(
            "handoff", lambda *_: report.update(rtp_received=True)
        )
        report["validation_flags"] = int(
            pipeline.get_by_name("camerasource").get_property("tls-validation-flags")
        )
        pipeline.set_state(Gst.State.PLAYING)
        bus = pipeline.get_bus()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not report["rtp_received"]:
            message = bus.pop_filtered(Gst.MessageType.ERROR)
            if message:
                error, debug = message.parse_error()
                detail = sanitize_text(f"{error.message}; {debug}")
                report["errors"].append(policy.describe(detail))
                report["reason"] = policy.reason(detail)
                break
            time.sleep(.02)
        report["certificate_error_flags"] = policy._flags
    if coordinator is not None:
        before = len(peer_buffers)
        time.sleep(.7)
        report["peer_progress_after_failure"] = len(peer_buffers) - before
        report["peer_state_playing"] = peer.bin.get_state(0)[1] == Gst.State.PLAYING
    report["configured_before_source_started"] = all(
        state == int(Gst.State.NULL) for state in configured_states
    )
except CameraTlsConfigurationError as exc:
    report["configuration_error"] = str(exc)
    report["reason"] = policy.reason(str(exc))
finally:
    if coordinator is not None:
        coordinator.close()
    elif pipeline is not None:
        pipeline.set_state(Gst.State.NULL)
print(json.dumps(report), flush=True)
