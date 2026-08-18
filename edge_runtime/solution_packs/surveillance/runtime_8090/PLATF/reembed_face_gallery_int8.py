"""Rebuild an enrollment gallery with the live OpenVINO INT8 AdaFace model.

INT8 describes the model's internal inference precision. Its 512-D output is
still L2-normalized float32; storing the output itself as int8 would damage
cosine similarity. This tool re-infers every saved aligned chip instead.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

from MTMC.ov_backends import OVCore
from PLATF.face_enroll_gallery import Gallery


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    return v / max(float(np.linalg.norm(v)), 1e-12)


def _embed_chip(compiled, output, chip_path: Path) -> np.ndarray:
    chip = cv2.imread(str(chip_path), cv2.IMREAD_COLOR)
    if chip is None:
        raise RuntimeError(f"cannot read enrollment chip: {chip_path}")
    if chip.shape[:2] != (112, 112):
        chip = cv2.resize(chip, (112, 112), interpolation=cv2.INTER_LINEAR)
    batch = ((chip.astype(np.float32) / 255.0) - 0.5) / 0.5
    batch = np.ascontiguousarray(batch.transpose(2, 0, 1)[None])
    return _unit(np.asarray(compiled(batch)[output]))


def _cross_backend_report(old: np.ndarray, new: np.ndarray, names: list[str]) -> dict:
    paired = np.sum(old * new, axis=1)

    def direction(queries, gallery):
        correct = 0
        genuine, impostor = [], []
        for i, q in enumerate(queries):
            sims = gallery @ q
            # Exclude the same source chip: this tests other poses/samples rather
            # than rewarding the exact JPEG that created the gallery row.
            sims[i] = -2.0
            same = np.array([n == names[i] for n in names])
            same[i] = False
            genuine.append(float(np.max(sims[same])))
            impostor.append(float(np.max(sims[~same])))
            correct += names[int(np.argmax(sims))] == names[i]
        return {
            "top1_excluding_same_chip": correct / len(names),
            "genuine_min": min(genuine),
            "genuine_mean": float(np.mean(genuine)),
            "impostor_max": max(impostor),
            "margin_min": min(g - b for g, b in zip(genuine, impostor)),
        }

    return {
        "paired_fp32_vs_int8_cosine": {
            "min": float(np.min(paired)),
            "mean": float(np.mean(paired)),
            "max": float(np.max(paired)),
        },
        "fp32_queries_to_int8_gallery": direction(old, new),
        "int8_queries_to_fp32_gallery": direction(new, old),
    }


def rebuild(source: Path, destination: Path, model: Path, device: str) -> dict:
    if destination.exists():
        raise RuntimeError(f"destination already exists: {destination}")
    gallery = Gallery(source)
    if not gallery.vecs:
        raise RuntimeError(f"source gallery is empty: {source}")
    missing = [source / v.chip_path for v in gallery.vecs
               if not v.chip_path or not (source / v.chip_path).is_file()]
    if missing:
        raise RuntimeError(f"{len(missing)} gallery rows have no usable source chip")

    compiled = OVCore.compile(model, device)
    output = compiled.output(0)
    new = np.stack([_embed_chip(compiled, output, source / v.chip_path)
                    for v in gallery.vecs]).astype(np.float32)
    old = np.stack([_unit(v.vec) for v in gallery.vecs]).astype(np.float32)
    names = [v.person for v in gallery.vecs]
    report = _cross_backend_report(old, new, names)

    shutil.copytree(source, destination)
    np.save(destination / "vectors.npy", new)
    provenance = {
        "created_at": time.time(),
        "source_gallery": str(source.resolve()),
        "model": str(model.resolve()),
        "device": device,
        "model_precision": "INT8",
        "stored_embedding_dtype": "float32",
        "stored_embedding_normalization": "L2",
        "vectors": len(new),
        "people": sorted(set(names)),
        "comparison": report,
    }
    (destination / "embedding_provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8")
    # Reopen to catch row/vector mismatch or corrupt output before it can be used.
    checked = Gallery(destination)
    if len(checked.vecs) != len(gallery.vecs):
        raise RuntimeError("rebuilt gallery failed validation")
    return provenance


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="FACE/gallery")
    ap.add_argument("--destination", default="FACE/gallery_int8")
    ap.add_argument("--model", default="models/adaface_ir101_int8.xml")
    ap.add_argument("--device", default="GPU")
    args = ap.parse_args()
    report = rebuild(Path(args.source), Path(args.destination),
                     Path(args.model), args.device)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
