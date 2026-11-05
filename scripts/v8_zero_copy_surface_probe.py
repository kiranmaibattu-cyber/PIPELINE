#!/usr/bin/env python3
"""Compare V8's authoritative BGR detector with VA RemoteTensor inference.

This probe never instantiates tracking, event policy, evidence, or delivery.
Both detectors consume the same decoded frame/PTS and results are compared in
source coordinates. It is intended to qualify zero-copy before promotion.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--frames", type=int, default=100)
    parser.add_argument("--confidence", type=float, default=0.30)
    parser.add_argument("--shadow-confidence", type=float)
    parser.add_argument("--examples", type=int, default=3)
    parser.add_argument("--live-sink-drop", action="store_true")
    args = parser.parse_args()
    shadow_confidence = (
        args.confidence
        if args.shadow_confidence is None
        else args.shadow_confidence
    )
    source_uri = str(args.source)
    source_parts = urlsplit(source_uri)
    if not source_parts.scheme and not Path(source_uri).is_file():
        parser.error(f"source is not a file: {source_uri}")

    os.environ["GST_REGISTRY"] = f"/tmp/zero-copy-probe-{os.getpid()}.bin"
    os.environ.setdefault("GST_VAAPI_ALL_DRIVERS", "1")
    try:
        os.unlink(os.environ["GST_REGISTRY"])
    except FileNotFoundError:
        pass

    worker = Path("/opt/traffic-pilot/services/worker")
    if worker.is_dir():
        sys.path.insert(0, str(worker))

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)

    from detectors.backends.openvino_detector import OpenVINOYOLODetector
    from pipeline.surface_stream import SelectiveBgrConverter, VaBridge, _map_bgr
    from pipeline.surface_vehicle import SurfaceVehicleDetector
    from pipeline.zero_copy_shadow import ZeroCopyShadowComparator

    class_names = {
        0: "bus", 1: "car", 2: "minivan", 3: "motorcycle",
        4: "rickshaw", 5: "truck", 6: "bicycle", 7: "pedestrian",
    }
    class_ids = set(class_names)
    model = "/models/traffic/openvino/vehicle_yolo26n_fp16_dynamic.xml"
    location = json.dumps(source_uri)
    if source_parts.scheme == "tcp":
        if not source_parts.hostname or not source_parts.port:
            parser.error("TCP source requires host and port")
        src = (
            f"tcpclientsrc host={json.dumps(source_parts.hostname)} "
            f"port={source_parts.port} timeout=30 ! tsdemux ! "
        )
    elif source_parts.scheme in {"rtsp", "rtsps"}:
        src = (
            f"rtspsrc location={location} protocols=tcp latency=100 ! "
            "rtph264depay ! "
        )
    else:
        src = f"filesrc location={location} ! qtdemux ! "
    sink_drop = "true" if args.live_sink_drop else "false"
    description = (
        src + "h264parse ! "
        "vaapih264dec name=decoder ! "
        "video/x-raw(memory:VASurface),format=NV12,width=1920,height=1080 ! "
        f"appsink name=vasink sync=false max-buffers=1 drop={sink_drop} "
        "wait-on-eos=false enable-last-sample=false"
    )
    pipeline = Gst.parse_launch(description)
    bridge = converter = shadow = None
    comparator = ZeroCopyShadowComparator("surface-probe", capacity=256)
    authoritative = OpenVINOYOLODetector(
        model,
        "vehicle",
        device="GPU",
        imgsz=640,
        confidence=args.confidence,
        class_ids=class_ids,
        class_names=class_names,
        performance_hint="LATENCY",
    )
    authoritative.warmup()
    timings = {"authoritative_seconds": 0.0, "shadow_seconds": 0.0}
    processed = 0
    mismatch_examples = []
    matched_examples = []
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("VAAPI pipeline failed to enter PLAYING")
        sink = pipeline.get_by_name("vasink")
        decoder = pipeline.get_by_name("decoder")
        sample = sink.emit("try-pull-sample", 35 * Gst.SECOND)
        if sample is None:
            raise RuntimeError("VAAPI decoder produced no sample")
        bridge = VaBridge(hash(decoder))
        converter = SelectiveBgrConverter(decoder, sample.get_caps(), {})
        shadow = SurfaceVehicleDetector(
            hash(decoder), model, confidence=shadow_confidence,
            class_ids=class_ids, class_names=class_names,
        )
        # Warm native graph on the retained first surface.
        shadow.detect_surface(bridge.surface_id(sample))
        while sample is not None and processed < args.frames:
            pts = int(sample.get_buffer().pts)
            bgr = _map_bgr(converter.convert(sample, measured=False))
            started = time.perf_counter()
            reference = authoritative.detect(bgr)
            timings["authoritative_seconds"] += time.perf_counter() - started
            started = time.perf_counter()
            observed = shadow.detect_surface(bridge.surface_id(sample))
            timings["shadow_seconds"] += time.perf_counter() - started
            if (
                len(matched_examples) < max(0, args.examples)
                and reference and observed
            ):
                matched_examples.append({
                    "frame": processed,
                    "authoritative": [
                        {"class": item.class_name, "confidence": item.confidence,
                         "bbox": item.bbox}
                        for item in reference[:10]
                    ],
                    "shadow": [
                        {"class": item.class_name, "confidence": item.confidence,
                         "bbox": item.bbox}
                        for item in observed[:10]
                    ],
                })
            if (
                len(mismatch_examples) < max(0, args.examples)
                and (
                    len(reference) != len(observed)
                    or any(
                        left.class_id != right.class_id
                        or abs(left.confidence - right.confidence) > 0.05
                        for left, right in zip(reference, observed)
                    )
                )
            ):
                mismatch_examples.append({
                    "frame": processed,
                    "pts": pts,
                    "authoritative": [
                        {"class": item.class_name, "confidence": item.confidence,
                         "bbox": item.bbox}
                        for item in reference[:10]
                    ],
                    "shadow": [
                        {"class": item.class_name, "confidence": item.confidence,
                         "bbox": item.bbox}
                        for item in observed[:10]
                    ],
                })
            comparator.submit_authoritative(pts, reference)
            comparator.submit_shadow(pts, observed)
            processed += 1
            sample = sink.emit("try-pull-sample", 35 * Gst.SECOND)
    finally:
        # Objects backed by the decoder display must be destroyed first.
        if converter is not None:
            converter.close()
        if shadow is not None:
            shadow.close()
        comparator.close()
        if bridge is not None:
            bridge.close()
        pipeline.set_state(Gst.State.NULL)

    result = comparator.snapshot()
    result.update({
        "frames_requested": args.frames,
        "frames_processed": processed,
        "authoritative_mean_ms": (
            1000 * timings["authoritative_seconds"] / processed
            if processed else 0.0
        ),
        "zero_copy_shadow_mean_ms": (
            1000 * timings["shadow_seconds"] / processed
            if processed else 0.0
        ),
        "mismatch_examples": mismatch_examples,
        "matched_examples": matched_examples,
    })
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if processed else 1


if __name__ == "__main__":
    raise SystemExit(main())
