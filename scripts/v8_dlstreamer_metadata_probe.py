#!/usr/bin/env python3
"""Print bounded DL Streamer ROI/tensor metadata for cascade qualification."""
from __future__ import annotations

import argparse
import json
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from gstgva import VideoFrame  # noqa: E402


def _tensor(tensor):
    data = tensor.data()
    return {
        "name": tensor.name(),
        "model_name": tensor.model_name(),
        "layer_name": tensor.layer_name(),
        "dims": tensor.dims(),
        "fields": tensor.fields(),
        "values": None if data is None else int(data.size),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--frames", type=int, default=30)
    args = parser.parse_args()
    Gst.init([])
    pipeline = Gst.parse_launch(args.pipeline)
    sink = pipeline.get_by_name("probe")
    if sink is None:
        raise ValueError("pipeline must contain appsink name=probe")
    pipeline.set_state(Gst.State.PLAYING)
    emitted = 0
    try:
        deadline = time.monotonic() + 90
        while emitted < args.frames and time.monotonic() < deadline:
            sample = sink.emit("try-pull-sample", int(2 * Gst.SECOND))
            if sample is None:
                continue
            frame = VideoFrame(sample.get_buffer(), caps=sample.get_caps())
            regions = []
            for roi in frame.regions():
                rect = roi.rect()
                regions.append({
                    "label": roi.label(),
                    "confidence": float(roi.confidence()),
                    "object_id": roi.object_id(),
                    "rect": [rect.x, rect.y, rect.w, rect.h],
                    "tensors": [_tensor(tensor) for tensor in roi.tensors()],
                })
            if any(region["label"] == "face" for region in regions):
                print(json.dumps({
                    "pts": int(sample.get_buffer().pts),
                    "frame_tensors": [_tensor(tensor) for tensor in frame.tensors()],
                    "regions": regions,
                }, default=str))
                emitted += 1
    finally:
        pipeline.set_state(Gst.State.NULL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
