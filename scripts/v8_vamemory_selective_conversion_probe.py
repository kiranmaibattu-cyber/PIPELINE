#!/usr/bin/env python3
"""Qualify retained VAMemory -> selective BGR conversion for Sentinel V8.

The source graph keeps decoded frames in VA memory through DL Streamer
detection/tracking.  Only every Nth retained sample is submitted to a separate
VA post-processing graph.  PTS and pixels are checked before the result is
accepted.  This is a qualification utility; it does not produce Events.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402
from gstgva import VideoFrame  # noqa: E402


def quote(value: str) -> str:
    return json.dumps(str(value))


class VAMemoryBgrConverter:
    """Convert only explicitly submitted VAMemory samples."""

    def __init__(self, caps: Gst.Caps) -> None:
        structure = caps.get_structure(0)
        self.width = int(structure.get_value("width"))
        self.height = int(structure.get_value("height"))
        self.pipeline = Gst.parse_launch(
            "appsrc name=source format=time block=true max-buffers=1 max-bytes=0 "
            "! vapostproc ! video/x-raw,format=BGRx "
            "! videoconvert ! video/x-raw,format=BGR "
            "! appsink name=sink sync=false max-buffers=1 drop=false "
            "wait-on-eos=false enable-last-sample=false"
        )
        self.source = self.pipeline.get_by_name("source")
        self.sink = self.pipeline.get_by_name("sink")
        self.source.set_property("caps", caps)
        self.pending_pts: int | None = None
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("selective VAMemory converter failed to start")

    def convert(self, sample: Gst.Sample) -> tuple[Gst.Sample, float]:
        if self.pending_pts is not None:
            raise RuntimeError("selective conversion already pending")
        source_pts = int(sample.get_buffer().pts)
        started = time.perf_counter()
        if self.source.emit("push-sample", sample) != Gst.FlowReturn.OK:
            raise RuntimeError("selective converter rejected VAMemory sample")
        self.pending_pts = source_pts
        result = self.sink.emit("try-pull-sample", 10 * Gst.SECOND)
        elapsed = time.perf_counter() - started
        if result is None:
            raise TimeoutError("selective VAMemory conversion timed out")
        result_pts = int(result.get_buffer().pts)
        self.pending_pts = None
        if source_pts != result_pts:
            raise RuntimeError(
                f"selective conversion PTS mismatch: {source_pts} != {result_pts}"
            )
        return result, elapsed

    def close(self) -> None:
        if getattr(self, "pipeline", None) is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None


def map_checksum(sample: Gst.Sample) -> tuple[int, int, int]:
    info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
    ok, mapped = sample.get_buffer().map(Gst.MapFlags.READ)
    if not ok:
        raise RuntimeError("could not map converted BGR sample")
    try:
        payload = mapped.data
        if not payload:
            raise RuntimeError("converted BGR sample is empty")
        stride = int(info.stride[0])
        expected = stride * int(info.height)
        if len(payload) < expected:
            raise RuntimeError("converted BGR sample is truncated")
        # Bounded checksum proves readable, non-empty pixels without dumping data.
        step = max(1, expected // 4096)
        checksum = sum(payload[index] for index in range(0, expected, step))
        return int(info.width), int(info.height), int(checksum)
    finally:
        sample.get_buffer().unmap(mapped)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--convert-every", type=int, default=10)
    parser.add_argument(
        "--model", default="/models/traffic/openvino/vehicle_yolo26n_fp16_dynamic.xml"
    )
    parser.add_argument(
        "--model-proc", default="/opt/traffic-pilot/config/vehicle_yolo26_model_proc.json"
    )
    args = parser.parse_args()
    source = Path(args.source)
    if not source.is_file():
        parser.error(f"source is not a file: {source}")
    Gst.init([])
    description = (
        f"filesrc location={quote(str(source))} ! qtdemux ! h264parse ! vah264dec "
        "! vapostproc ! video/x-raw(memory:VAMemory),format=NV12 "
        f"! gvadetect model={quote(args.model)} model-proc={quote(args.model_proc)} "
        "device=GPU pre-process-backend=va-surface-sharing threshold=0.3 nireq=4 "
        "batch-size=1 model-instance-id=probe-primary "
        "! gvatrack tracking-type=zero-term-imageless "
        "! appsink name=surface sync=false max-buffers=2 drop=false "
        "wait-on-eos=false enable-last-sample=false"
    )
    pipeline = Gst.parse_launch(description)
    sink = pipeline.get_by_name("surface")
    converter = None
    processed = converted = regions = 0
    conversion_seconds = 0.0
    checksums: list[int] = []
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("source DL Streamer graph failed to start")
        while processed < args.frames:
            sample = sink.emit("try-pull-sample", 30 * Gst.SECOND)
            if sample is None:
                message = pipeline.get_bus().pop_filtered(
                    Gst.MessageType.ERROR | Gst.MessageType.EOS
                )
                if message is not None and message.type == Gst.MessageType.ERROR:
                    error, _debug = message.parse_error()
                    raise RuntimeError(error.message)
                break
            caps = sample.get_caps()
            features = caps.get_features(0)
            if not features.contains("memory:VAMemory"):
                raise RuntimeError(f"source sample is not VAMemory: {caps.to_string()}")
            frame = VideoFrame(sample.get_buffer(), caps=caps)
            regions += len(list(frame.regions()))
            if processed % max(1, args.convert_every) == 0:
                if converter is None:
                    converter = VAMemoryBgrConverter(caps)
                bgr, elapsed = converter.convert(sample)
                width, height, checksum = map_checksum(bgr)
                if width <= 0 or height <= 0 or checksum <= 0:
                    raise RuntimeError("invalid converted BGR result")
                checksums.append(checksum)
                conversion_seconds += elapsed
                converted += 1
            processed += 1
    finally:
        if converter is not None:
            converter.close()
        pipeline.set_state(Gst.State.NULL)
    result = {
        "frames_processed": processed,
        "frames_converted": converted,
        "frames_left_in_va_memory": processed - converted,
        "regions_observed": regions,
        "pts_pairing": "passed" if converted else "not_exercised",
        "pixel_mapping": "passed" if checksums else "not_exercised",
        "mean_selected_conversion_ms": (
            1000.0 * conversion_seconds / converted if converted else 0.0
        ),
        "unique_pixel_checksums": len(set(checksums)),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if processed and converted else 1


if __name__ == "__main__":
    raise SystemExit(main())
