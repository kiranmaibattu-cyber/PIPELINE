"""Query the local FAISS gallery vector-DB exported by multicam_pipeline.

Demonstrates the production storage/matching pattern the reference questions ask
about: every stored embedding is indexed with its global_id; a query retrieves
top-k nearest embeddings (cosine via inner product), which are grouped by
global_id and reduced by MIN distance — exactly how the online gallery matches.

Usage:
    python -m MTMC.gallery_query --gallery NEW/gallery --stats
    python -m MTMC.gallery_query --gallery NEW/gallery --crop path/to/crop.jpg
    python -m MTMC.gallery_query --gallery NEW/gallery --gid 12    # neighbors of ID 12
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent


def load(gallery_dir: Path):
    import faiss
    index = faiss.read_index(str(gallery_dir / "gallery.faiss"))
    meta = json.loads((gallery_dir / "gallery_meta.json").read_text(encoding="utf-8"))
    row_gid = np.load(gallery_dir / "row_global_ids.npy")
    return index, meta, row_gid


def stats(gallery_dir: Path) -> dict:
    index, meta, row_gid = load(gallery_dir)
    per_id = defaultdict(int)
    for g in row_gid:
        per_id[int(g)] += 1
    counts = np.array(list(per_id.values()))
    xcam = sum(1 for m in meta.values() if len(m["cameras"]) > 1)
    out = {
        "vectors_in_index": int(index.ntotal),
        "unique_global_ids": len(meta),
        "embeddings_per_id_min": int(counts.min()),
        "embeddings_per_id_max": int(counts.max()),
        "embeddings_per_id_avg": round(float(counts.mean()), 2),
        "cross_camera_ids": xcam,
        "index_type": type(index).__name__,
    }
    print(json.dumps(out, indent=2))
    return out


def search_vector(gallery_dir: Path, q: np.ndarray, top_k=5) -> list:
    import faiss
    index, meta, _ = load(gallery_dir)
    q = q.astype(np.float32).reshape(1, -1)
    faiss.normalize_L2(q)
    # over-fetch rows then group by global_id, reduce by MIN distance (= max IP)
    sims, ids = index.search(q, min(50, index.ntotal))
    best = {}
    for s, gid in zip(sims[0], ids[0]):
        gid = int(gid)
        if gid not in best or s > best[gid]:
            best[gid] = float(s)
    ranked = sorted(best.items(), key=lambda kv: -kv[1])[:top_k]
    return [{"global_id": g, "cosine_sim": round(s, 4),
             "cameras": meta.get(str(g), {}).get("cameras", []),
             "thumbnail": meta.get(str(g), {}).get("thumbnail", "")} for g, s in ranked]


def search_gid(gallery_dir: Path, gid: int, top_k=6) -> list:
    """Use one of a global ID's own stored vectors as the query — shows which
    other identities are nearest (confusability check)."""
    index, meta, row_gid = load(gallery_dir)
    rows = np.where(row_gid == gid)[0]
    if len(rows) == 0:
        print(f"no vectors for gid {gid}"); return []
    q = index.reconstruct(int(rows[0]))
    res = search_vector(gallery_dir, np.asarray(q), top_k)
    print(f"nearest identities to G{gid} (self should rank #1):")
    for r in res:
        print(f"  G{r['global_id']:<4} sim={r['cosine_sim']:.3f} cams={r['cameras']}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gallery", default="NEW/gallery")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--gid", type=int, default=None)
    ap.add_argument("--crop", default="")
    ap.add_argument("--embedder", default="transreid_ssl")
    args = ap.parse_args()
    gdir = _ROOT / args.gallery

    if args.stats:
        stats(gdir)
    if args.gid is not None:
        search_gid(gdir, args.gid)
    if args.crop:
        import cv2
        from MTMC.adapters import load_embedder
        emb, _ = load_embedder(args.embedder, tta_flip=True)
        v = emb.embed([cv2.imread(args.crop)])[0]
        for r in search_vector(gdir, np.asarray(v)):
            print(f"  G{r['global_id']:<4} sim={r['cosine_sim']:.3f} cams={r['cameras']} {r['thumbnail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
