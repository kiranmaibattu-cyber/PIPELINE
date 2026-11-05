#!/usr/bin/env python3
"""Materialize the dynamic SigLIP2 image encoder as a static DL Streamer IR."""
from __future__ import annotations

import argparse
from pathlib import Path

import openvino as ov


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    core = ov.Core()
    model = core.read_model(str(args.source))
    if len(model.inputs) != 1:
        raise ValueError("SigLIP2 image encoder must expose one input")
    model.reshape({model.input(0): [1, 3, 224, 224]})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ov.serialize(model, str(args.output), str(args.output.with_suffix(".bin")))


if __name__ == "__main__":
    main()
