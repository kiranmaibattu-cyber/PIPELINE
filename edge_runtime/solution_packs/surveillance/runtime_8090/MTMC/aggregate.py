"""Aggregate MTMC run summaries into the tournament comparison table.

Collects MTMC/reports/*/<scenario>_summary.json, joins accuracy metrics from
MTMC.metrics when annotations exist, and writes
MTMC/reports/stage1_comparison.{json,md} ranked by (idf1 desc, gid_inflation
asc, fps desc). Without annotations the table still ranks by proxy columns.

Usage:  python -m MTMC.aggregate --scenario cross_camera
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_REPORTS = _ROOT / "MTMC" / "reports"
_ANNOTATIONS = _ROOT / "MTMC" / "reports" / "annotations_mtmc.csv"


def collect(scenario: str) -> list[dict]:
    rows: list[dict] = []
    for run_dir in sorted(_REPORTS.iterdir()):
        if not run_dir.is_dir():
            continue
        summary_path = run_dir / f"{scenario}_summary.json"
        if not summary_path.exists():
            continue
        s = json.loads(summary_path.read_text(encoding="utf-8"))
        row = {
            "run_id": s.get("run_id", run_dir.name),
            "embedder": s.get("embedder"),
            "status": s.get("run_status"),
            "threshold": s.get("threshold_used"),
            "fps": s.get("avg_live_fps"),
            "reid_ms": s.get("avg_reid_ms"),
            "unique_gids": s.get("unique_gids_final"),
            "cross_camera_ids": (s.get("gallery_stats") or {}).get("cross_camera_ids"),
        }
        # join accuracy metrics if annotations exist
        events_path = run_dir / f"{scenario}_track_events.csv"
        if _ANNOTATIONS.exists() and _ANNOTATIONS.stat().st_size > 60 and events_path.exists():
            from MTMC.metrics import score_run
            m = score_run(events_path, _ANNOTATIONS, row["run_id"], scenario)
            if m.get("status") == "scored":
                row.update({
                    "idf1": m["idf1"], "purity": m["purity"],
                    "id_switches": m["id_switches"], "gid_inflation": m["gid_inflation"],
                    "xcam_precision": m["cross_camera_precision"],
                    "xcam_recall": m["cross_camera_recall"],
                })
        rows.append(row)
    rows.sort(key=lambda r: (-(r.get("idf1") or 0), r.get("gid_inflation") or 999,
                             -(r.get("fps") or 0)))
    return rows


def to_markdown(rows: list[dict], scenario: str) -> str:
    cols = ["run_id", "status", "threshold", "fps", "unique_gids", "cross_camera_ids",
            "idf1", "purity", "id_switches", "gid_inflation", "xcam_precision", "xcam_recall"]
    present = [c for c in cols if any(r.get(c) is not None for r in rows)]
    lines = [f"# Stage-1 comparison — {scenario}", "",
             "| " + " | ".join(present) + " |",
             "|" + "|".join("---" for _ in present) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "")) if r.get(c) is not None else "—"
                                        for c in present) + " |")
    if not any(r.get("idf1") is not None for r in rows):
        lines += ["", "_Accuracy columns pending GATE #1 annotations "
                      "(annotations/annotations.csv is empty)._"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="cross_camera")
    args = ap.parse_args()

    rows = collect(args.scenario)
    (_REPORTS / "stage1_comparison.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8")
    md = to_markdown(rows, args.scenario)
    (_REPORTS / "stage1_comparison.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
