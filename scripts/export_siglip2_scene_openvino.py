#!/usr/bin/env python3
"""Export the pinned SigLIP 2 image tower used by V18 scene search."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import openvino as ov
import torch



MODEL_ID = "google/siglip2-base-patch16-224"
MODEL_REVISION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
DIMENSIONS = 768


class ImageTower(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, pixel_values: torch.Tensor) -> torch.Tensor:
        return self.model.get_image_features(pixel_values=pixel_values)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default="PIPELINE/models/scene/openvino",
        help="Directory for the OpenVINO image-tower IR and metadata.",
    )
    parser.add_argument("--cache-dir")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Some workstation torch/torchvision combinations omit schemas that
    # torchvision registers fake kernels for. SigLIP does not call these ops;
    # defining their schemas keeps this export process isolated from that mismatch.
    compatibility = torch.library.Library("torchvision", "DEF")
    compatibility.define("nms(Tensor boxes, Tensor scores, float iou_threshold) -> Tensor")
    compatibility.define("qnms(Tensor boxes, Tensor scores, float iou_threshold) -> Tensor")
    from transformers import SiglipModel

    model = SiglipModel.from_pretrained(
        MODEL_ID,
        revision=MODEL_REVISION,
        cache_dir=args.cache_dir,
        trust_remote_code=False,
    )
    model.eval()
    wrapper = ImageTower(model)
    example = torch.zeros((1, 3, 224, 224), dtype=torch.float32)
    with torch.no_grad():
        output = wrapper(example)
    if tuple(output.shape) != (1, DIMENSIONS):
        raise RuntimeError(f"unexpected image embedding shape: {tuple(output.shape)}")

    converted = ov.convert_model(wrapper, example_input=example)
    xml_path = output_dir / "siglip2-base-patch16-224.xml"
    ov.save_model(converted, xml_path, compress_to_fp16=True)
    bin_path = xml_path.with_suffix(".bin")

    core = ov.Core()
    compiled = core.compile_model(str(xml_path), "CPU")
    result = np.asarray(compiled([np.zeros((1, 3, 224, 224), np.float32)])[compiled.output(0)])
    if result.shape != (1, DIMENSIONS) or not np.isfinite(result).all():
        raise RuntimeError(f"invalid exported model output: {result.shape}")
    reference = output.detach().cpu().numpy()
    validation_cosine = float(
        np.dot(reference.reshape(-1), result.reshape(-1))
        / (np.linalg.norm(reference) * np.linalg.norm(result))
    )
    if validation_cosine < 0.999:
        raise RuntimeError(
            f"OpenVINO export differs from source model: cosine={validation_cosine:.6f}"
        )

    metadata = {
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "embedding_space": (
            f"{MODEL_ID}@{MODEL_REVISION}:image-text:l2:{DIMENSIONS}"
        ),
        "dimensions": DIMENSIONS,
        "input_shape": [1, 3, 224, 224],
        "input_color": "RGB",
        "input_normalization": "(pixel/255-0.5)/0.5",
        "precision": "FP16",
        "source_validation_cosine": validation_cosine,
        "xml_sha256": sha256(xml_path),
        "bin_sha256": sha256(bin_path),
    }
    metadata_path = output_dir / "siglip2-base-patch16-224.metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
