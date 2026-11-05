#!/usr/bin/env python3
"""Wrap GaitBase's 30-frame tensor input as one DL Streamer BGR montage."""
from __future__ import annotations

import argparse
from pathlib import Path

import openvino as ov
try:
    from openvino import opset13 as ops
except ImportError:  # OpenVINO 2024 compatibility used by the build host.
    from openvino.runtime import opset13 as ops


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    core = ov.Core()
    gait = core.read_model(str(args.source))
    old_input = gait.input(0).get_node()

    # Thirty 64x44 silhouettes are arranged row-major in a 5x6 montage.
    image = ops.parameter([1, 3, 320, 264], ov.Type.f32, name="gait_montage")
    first_channel = ops.gather(image, ops.constant(0), ops.constant(1))
    tiled = ops.reshape(first_channel, ops.constant([1, 5, 64, 6, 44]), False)
    ordered = ops.transpose(tiled, ops.constant([0, 1, 3, 2, 4]))
    sequence = ops.reshape(ordered, ops.constant([1, 30, 64, 44]), False)
    sequence = ops.divide(sequence, ops.constant(255.0, ov.Type.f32))
    old_input.output(0).replace(sequence.output(0))
    wrapped = ov.Model(gait.outputs, [image], "gaitbase_dlstreamer_montage")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ov.save_model(wrapped, str(args.output), compress_to_fp16=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
