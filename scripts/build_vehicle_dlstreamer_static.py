#!/usr/bin/env python3
"""Prepare a fixed traffic-detector batch for shared DL Streamer execution.

The source model and its output/class metadata are unchanged.  A static input
is required because DL Streamer 2026.2 does not transition multiple elements
sharing this dynamic-batch IR beyond READY on the Intel GPU.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import openvino as ov


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--batch-size", type=int, default=1)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    core = ov.Core()
    model = core.read_model(str(args.source))
    original_outputs = [str(output.get_partial_shape()) for output in model.outputs]
    quantizers = sum(op.get_type_name() == "FakeQuantize" for op in model.get_ops())
    model.reshape({model.input(0): [args.batch_size, 3, 640, 640]})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ov.save_model(model, str(args.output), compress_to_fp16=False)
    # Serialization must preserve the trained QAFT quantizers. This does not
    # compile a device, run inference, recalibrate or change the source IR.
    saved = core.read_model(str(args.output))
    if sum(op.get_type_name() == "FakeQuantize" for op in saved.get_ops()) != quantizers:
        raise RuntimeError("static detector conversion changed quantizer count")
    print(
        f"saved {args.output}: input={model.input(0).get_partial_shape()} "
        f"outputs={original_outputs} FakeQuantize={quantizers}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
