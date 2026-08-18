"""Dual-encoder searchers: encode person crops and free-text queries into one space.

  clip_zeroshot — OpenCLIP ViT-B-16 (openai weights). Guaranteed baseline.
  irra          — IRRA (CVPR'23) fine-tuned on CUHK-PEDES; CLIP ViT-B-16 backbone
                  specialized for pedestrian text-to-image retrieval.
  rde           — RDE (CVPR'24) IRRA-based dual embedding (BGE global +
                  TSE token-selection); final score = (BGE+TSE)/2, reproduced as
                  a single concatenated vector scaled by 1/sqrt(2) each.
  aptm          — APTM (ACM MM'23) ALBEF-style Swin+BERT, fine-tuned CUHK-PEDES.

All expose: encode_images(list[bgr]) -> (N,D) L2-normed; encode_text(str) -> (D,).
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent


def _l2_rows(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return (m / np.maximum(n, 1e-12)).astype(np.float32)


class ClipZeroShot:
    def __init__(self) -> None:
        import open_clip
        import torch

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-B-16", pretrained="openai",
            cache_dir=str(_ROOT / "models" / "open_clip"))
        self.tokenizer = open_clip.get_tokenizer("ViT-B-16")
        self.model.eval().to(self.device)
        self.backend = f"open_clip:ViT-B-16:openai:{self.device}"

    def encode_images(self, crops: list[np.ndarray]) -> np.ndarray:
        from PIL import Image
        batch = [self.preprocess(Image.fromarray(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)))
                 for c in crops]
        tensor = self.torch.stack(batch).to(self.device)
        with self.torch.no_grad():
            feats = self.model.encode_image(tensor)
        return _l2_rows(feats.cpu().numpy())

    def encode_text(self, query: str) -> np.ndarray:
        tokens = self.tokenizer([query]).to(self.device)
        with self.torch.no_grad():
            feat = self.model.encode_text(tokens)
        return _l2_rows(feat.cpu().numpy())[0]


class IRRASearcher:
    """IRRA fine-tuned CLIP. Loads the repo's model with the CUHK-PEDES ckpt."""

    def __init__(self) -> None:
        import torch

        repo = _ROOT / "repos" / "irra"
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from model import build_model  # IRRA repo (model/__init__ or model/build)
        from utils.simple_tokenizer import SimpleTokenizer  # IRRA repo

        ckpt_path = _ROOT / "models" / "irra" / "extracted" / "best.pth"
        ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)

        class _Args:  # minimal args IRRA's build_model expects
            pretrain_choice = "ViT-B/16"
            img_size = (384, 128)
            stride_size = 16
            temperature = 0.02
            img_aug = False
            cmt_depth = 4
            masked_token_rate = 0.8
            masked_token_unchanged_rate = 0.1
            lr_factor = 5.0
            training = False
            loss_names = ""
            vocab_size = 49408

        self.model = build_model(_Args(), num_classes=11003)
        state = ckpt.get("model", ckpt)
        missing = self.model.load_state_dict(state, strict=False)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.eval().to(self.device)
        self.torch = torch
        self.tokenizer = SimpleTokenizer()
        self.backend = f"irra:cuhk_pedes:{self.device}"

    def _tokenize(self, text: str, length: int = 77) -> "np.ndarray":
        sot = self.tokenizer.encoder["<|startoftext|>"]
        eot = self.tokenizer.encoder["<|endoftext|>"]
        tokens = [sot] + self.tokenizer.encode(text)[: length - 2] + [eot]
        out = np.zeros(length, dtype=np.int64)
        out[: len(tokens)] = tokens
        return out

    def encode_images(self, crops: list[np.ndarray]) -> np.ndarray:
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        batch = []
        for c in crops:
            img = cv2.cvtColor(c, cv2.COLOR_BGR2RGB)
            img = cv2.resize(img, (128, 384)).astype(np.float32) / 255.0
            img = (img - mean) / std
            batch.append(img.transpose(2, 0, 1))
        tensor = self.torch.from_numpy(np.ascontiguousarray(np.stack(batch))).float().to(self.device)
        with self.torch.no_grad():
            feats = self.model.encode_image(tensor)
        return _l2_rows(feats.cpu().numpy())

    def encode_text(self, query: str) -> np.ndarray:
        tokens = self.torch.from_numpy(self._tokenize(query)[None]).to(self.device)
        with self.torch.no_grad():
            feat = self.model.encode_text(tokens)
        return _l2_rows(feat.cpu().numpy())[0]


class RDESearcher:
    """RDE (CVPR'24): dual BGE+TSE embeddings. Final score = (BGE+TSE)/2.
    We L2-norm each branch, scale by 1/sqrt(2), and concatenate so a single
    dot product == RDE's averaged similarity — keeps the one-vector index."""

    def __init__(self) -> None:
        import torch

        repo = _ROOT / "repos" / "rde" / "2024-CVPR-RDE"
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from model.build import build_model  # noqa: PLC0415
        from utils.simple_tokenizer import SimpleTokenizer  # noqa: PLC0415

        class _Args:
            pretrain_choice = "ViT-B/16"
            img_size = (384, 128)
            stride_size = 16
            select_ratio = 0.3
            temperature = 0.02
            tau = 0.015
            margin = 0.1
            loss_names = "TAL"
            img_aug = False

        self.model = build_model(_Args(), num_classes=11003)
        ckpt = torch.load(_ROOT / "models" / "rde" / "rde_cuhk_best.pth",
                          map_location="cpu", weights_only=False)
        state = ckpt.get("model", ckpt)
        self.model.load_state_dict(state, strict=False)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.eval().to(self.device)
        self.torch = torch
        self.tokenizer = SimpleTokenizer()
        self._sqrt2 = float(np.sqrt(2.0))
        self.backend = f"rde:cuhk_pedes:bge+tse:{self.device}"

    def _tokenize(self, text: str, length: int = 77) -> np.ndarray:
        sot = self.tokenizer.encoder["<|startoftext|>"]
        eot = self.tokenizer.encoder["<|endoftext|>"]
        tokens = [sot] + self.tokenizer.encode(text) + [eot]
        out = np.zeros(length, dtype=np.int64)
        out[: min(len(tokens), length)] = tokens[:length]
        return out

    def _img_tensor(self, crops: list[np.ndarray]):
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        batch = []
        for c in crops:
            img = cv2.cvtColor(c, cv2.COLOR_BGR2RGB)
            img = cv2.resize(img, (128, 384)).astype(np.float32) / 255.0
            batch.append(((img - mean) / std).transpose(2, 0, 1))
        return self.torch.from_numpy(np.ascontiguousarray(np.stack(batch))).float().to(self.device)

    def encode_images(self, crops: list[np.ndarray]) -> np.ndarray:
        tensor = self._img_tensor(crops)
        with self.torch.no_grad():
            bge = self.model.encode_image(tensor).cpu().numpy()
            tse = self.model.encode_image_tse(tensor).cpu().numpy()
        return np.hstack([_l2_rows(bge) / self._sqrt2, _l2_rows(tse) / self._sqrt2]).astype(np.float32)

    def encode_text(self, query: str) -> np.ndarray:
        tokens = self.torch.from_numpy(self._tokenize(query)[None]).to(self.device)
        with self.torch.no_grad():
            bge = self.model.encode_text(tokens).cpu().numpy()
            tse = self.model.encode_text_tse(tokens).cpu().numpy()
        v = np.hstack([_l2_rows(bge) / self._sqrt2, _l2_rows(tse) / self._sqrt2]).astype(np.float32)
        return v[0]


def load_searcher(key: str):
    if key == "clip_zeroshot":
        return ClipZeroShot()
    if key == "irra":
        return IRRASearcher()
    if key == "rde":
        return RDESearcher()
    if key == "aptm":
        from MTMC.text_search.aptm_searcher import APTMSearcher
        return APTMSearcher()
    raise ValueError(f"unknown searcher: {key}")
