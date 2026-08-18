"""APTM (ACM MM'23) searcher — ALBEF-style Swin-B + BERT, CUHK-PEDES fine-tuned.

Uses APTM's dual-encoder retrieval embeddings (the fast stage-1 path used for
coarse ranking): image_feat = normalize(vision_proj(vision_encoder(img)[:,0])),
text_feat = normalize(text_proj(bert(text)[:,0])), both 256-d in one space.
(APTM's optional ITM re-ranker needs joint image+text and is skipped to keep
the pre-indexed single-vector paradigm consistent with the other searchers.)

One-time repo patch required (repos/aptm is gitignored): APTM's models/bert.py
imports symbols relocated/removed in transformers 5.x. Fix its import block —
  from transformers.utils import (ModelOutput, add_code_sample_docstrings,
      add_start_docstrings, add_start_docstrings_to_model_forward,
      replace_return_docstrings)          # was transformers.file_utils
  from transformers.modeling_utils import PreTrainedModel
  from transformers.pytorch_utils import apply_chunking_to_forward, prune_linear_layer
and add a local find_pruneable_heads_and_indices (removed in 5.x; stable 8-liner).
All other 5.x gaps (all_tied_weights_keys, get_head_mask) are monkeypatched below
at runtime, and the stock transformers BertTokenizer replaces APTM's vendored one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
_REPO = _ROOT / "repos" / "aptm"
# CUHK-PEDES dataset normalization (from APTM dataset/__init__.py cuhk_norm)
_MEAN = np.array([0.38901278, 0.3651612, 0.34836376], dtype=np.float32)
_STD = np.array([0.24344306, 0.23738699, 0.23368555], dtype=np.float32)


def _l2_rows(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return (m / np.maximum(n, 1e-12)).astype(np.float32)


class APTMSearcher:
    def __init__(self) -> None:
        import torch
        import yaml

        if str(_REPO) not in sys.path:
            sys.path.insert(0, str(_REPO))
        cwd = os.getcwd()
        os.chdir(_REPO)  # config references relative paths (configs/, data/)
        try:
            from models.model_retrieval import APTM_Retrieval
            # APTM's vendored tokenization_bert imports symbols removed in
            # transformers 5.x; the stock BertTokenizer is byte-identical for
            # bert-base-uncased, so use it directly.
            from transformers import BertTokenizer

            config = yaml.load((_REPO / "configs" / "Retrieval_cuhk.yaml").read_text(),
                               Loader=yaml.Loader)
            config["load_params"] = False       # weights come from the fine-tuned ckpt
            config["load_pretrained"] = False
            config["pa100k"] = False
            config["eda"] = False
            # mlm=True so the BERT text encoder is actually constructed (APTM's
            # build_text_encoder only builds it under the mlm branch); the MLM
            # head is unused at inference and the fine-tuned ckpt overwrites the
            # base BERT weights loaded here.
            config["mlm"] = True
            config["batch_size_test_text"] = 256
            self.max_tokens = config.get("max_tokens", 56)

            # transformers 5.x removed several PreTrainedModel/ModuleUtilsMixin
            # helpers the vendored 4.12-era BERT relies on. Restore the stable
            # implementations + tie-weights bookkeeping so the old model runs on
            # the new base class. (Inference-only; head_mask is always None.)
            import torch as _torch
            import models.bert as _aptm_bert
            from transformers.modeling_utils import PreTrainedModel as _PTM

            for _cls_name in ("BertForMaskedLM", "BertModel", "BertPreTrainedModel"):
                _cls = getattr(_aptm_bert, _cls_name, None)
                if _cls is not None and not isinstance(getattr(_cls, "all_tied_weights_keys", None), dict):
                    _cls.all_tied_weights_keys = {}

            if not hasattr(_PTM, "get_head_mask"):
                def _get_head_mask(self, head_mask, num_hidden_layers, is_attention_chunked=False):
                    if head_mask is None:
                        return [None] * num_hidden_layers
                    if head_mask.dim() == 1:
                        head_mask = head_mask.unsqueeze(0).unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
                        head_mask = head_mask.expand(num_hidden_layers, -1, -1, -1, -1)
                    elif head_mask.dim() == 2:
                        head_mask = head_mask.unsqueeze(1).unsqueeze(-1).unsqueeze(-1)
                    if is_attention_chunked:
                        head_mask = head_mask.unsqueeze(-1)
                    return head_mask
                _PTM.get_head_mask = _get_head_mask

            self.tokenizer = BertTokenizer.from_pretrained(config["text_encoder"])
            model = APTM_Retrieval(config)
            ckpt = torch.load(_ROOT / "models" / "aptm_dl" / "checkpoints" / "ft_cuhk"
                              / "checkpoint_best.pth", map_location="cpu", weights_only=False)
            state = ckpt.get("model", ckpt)
            # interpolate swin relative-position embeds if the eval res differs
            model.load_state_dict(state, strict=False)
        finally:
            os.chdir(cwd)

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = model.eval().to(self.device)
        self.torch = torch
        self.h, self.w = config["h"], config["w"]
        self.backend = f"aptm:cuhk_pedes:swinB+bert:{self.device}"

    def encode_images(self, crops: list[np.ndarray]) -> np.ndarray:
        batch = []
        for c in crops:
            img = cv2.cvtColor(c, cv2.COLOR_BGR2RGB)
            img = cv2.resize(img, (self.w, self.h), interpolation=cv2.INTER_CUBIC)
            img = img.astype(np.float32) / 255.0
            batch.append(((img - _MEAN) / _STD).transpose(2, 0, 1))
        tensor = self.torch.from_numpy(np.ascontiguousarray(np.stack(batch))).float().to(self.device)
        with self.torch.no_grad():
            image_embed, _ = self.model.get_vision_embeds(tensor)
            feat = self.model.vision_proj(image_embed[:, 0, :])
        return _l2_rows(feat.cpu().numpy())

    def encode_text(self, query: str) -> np.ndarray:
        enc = self.tokenizer([query], padding="max_length", truncation=True,
                             max_length=self.max_tokens, return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            text_embed = self.model.get_text_embeds(enc.input_ids, enc.attention_mask)
            feat = self.model.text_proj(text_embed[:, 0, :])
        return _l2_rows(feat.cpu().numpy())[0]
