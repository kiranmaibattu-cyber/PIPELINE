"""Query a person index with free text; rank global IDs.

Usage:
    python -m MTMC.text_search.search --index <path.npz> "woman in red saree"
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def search(index_path: Path, query: str, top_k: int = 5, searcher=None) -> list[dict]:
    from MTMC.text_search.models import load_searcher

    data = np.load(index_path, allow_pickle=True)
    embs = data["embeddings"]
    meta = json.loads(str(data["meta"]))
    model_key = str(data["model"])
    if searcher is None:
        searcher = load_searcher(model_key)

    q = searcher.encode_text(query)
    sims = embs @ q

    # group crops by global_id, keep each identity's best crop score
    by_gid: dict[int, dict] = defaultdict(lambda: {"score": -1.0})
    for sim, m in zip(sims, meta):
        gid = m["global_id"]
        if sim > by_gid[gid]["score"]:
            by_gid[gid] = {"score": float(sim), **m}

    ranked = sorted(by_gid.values(), key=lambda r: -r["score"])[:top_k]
    return ranked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("query")
    args = ap.parse_args()
    for r in search(Path(args.index), args.query, args.top_k):
        print(f"  {r['score']:.3f}  gid={r['global_id']:<4} cam={r['camera']} frame={r['frame']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
