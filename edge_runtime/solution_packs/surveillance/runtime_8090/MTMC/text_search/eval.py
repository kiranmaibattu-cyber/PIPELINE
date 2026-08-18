"""Evaluate text-search models: Rank-1/5 retrieval accuracy against GT labels.

A query hits at rank r if the r-th ranked global_id maps (via propagated
annotations for the indexed run) to the query's person_id.

Usage:
    python -m MTMC.text_search.eval --run <run_id>       # both models
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from MTMC.text_search.index_builder import build_index  # noqa: E402
from MTMC.text_search.models import load_searcher  # noqa: E402
from MTMC.text_search.search import search  # noqa: E402

_REPORTS = _ROOT / "MTMC" / "reports"
_QUERIES = Path(__file__).resolve().parent / "queries.json"


def evaluate(run_id: str, model_key: str) -> dict:
    index_path = _REPORTS / "text_search" / f"index__{model_key}__{run_id}.npz"
    if not index_path.exists():
        build_index(run_id, model_key)

    annots = pd.read_csv(_REPORTS / "annotations_mtmc.csv")
    rows = annots[annots["model"] == run_id]
    gid_pid = {(r.camera, int(r.global_id)): r.person_id for r in rows.itertuples()}

    queries = json.loads(_QUERIES.read_text(encoding="utf-8"))["queries"]
    searcher = load_searcher(model_key)

    r1 = r5 = 0
    details = []
    for q in queries:
        ranked = search(index_path, q["text"], top_k=5, searcher=searcher)
        pids = [gid_pid.get((r["camera"], r["global_id"])) for r in ranked]
        hit1 = pids[:1] == [q["person_id"]]
        hit5 = q["person_id"] in pids
        r1 += hit1
        r5 += hit5
        details.append({"query": q["text"], "expected": q["person_id"],
                        "top5_pids": pids, "rank1": hit1, "rank5": hit5})

    result = {
        "model": model_key, "run_id": run_id, "n_queries": len(queries),
        "rank1": round(r1 / len(queries), 4), "rank5": round(r5 / len(queries), 4),
        "details": details,
    }
    out = _REPORTS / "text_search" / f"eval__{model_key}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"{model_key}: Rank-1 {result['rank1']:.1%}  Rank-5 {result['rank5']:.1%}  ({len(queries)} queries)")
    return result


def _summarize(models: list[str]) -> None:
    """Collect eval__*.json into a ranked text-search comparison table."""
    rows = []
    for m in models:
        p = _REPORTS / "text_search" / f"eval__{m}.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            rows.append({"model": m, "rank1": d["rank1"], "rank5": d["rank5"],
                         "n_queries": d["n_queries"]})
    rows.sort(key=lambda r: (-r["rank1"], -r["rank5"]))
    lines = ["# Text-search comparison (text-to-person retrieval)", "",
             "| model | Rank-1 | Rank-5 | queries |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['model']} | {r['rank1']:.1%} | {r['rank5']:.1%} | {r['n_queries']} |")
    out = _REPORTS / "text_search" / "text_search_comparison.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--models", nargs="*",
                    default=["clip_zeroshot", "irra", "rde", "aptm"])
    ap.add_argument("--isolate", action="store_true",
                    help="run each model in its own subprocess (avoids repo module collisions)")
    ap.add_argument("--summary", action="store_true", help="only rebuild the comparison table")
    args = ap.parse_args()

    if args.summary:
        _summarize(args.models)
        return 0

    if args.isolate:
        import subprocess
        for m in args.models:
            print(f"=== {m} (subprocess) ===", flush=True)
            subprocess.run([sys.executable, "-m", "MTMC.text_search.eval",
                            "--run", args.run, "--models", m], cwd=str(_ROOT))
        _summarize(args.models)
        return 0

    for m in args.models:
        try:
            evaluate(args.run, m)
        except Exception as exc:  # noqa: BLE001
            print(f"{m}: FAILED - {exc}")
    _summarize(args.models)
    return 0


if __name__ == "__main__":
    sys.exit(main())
