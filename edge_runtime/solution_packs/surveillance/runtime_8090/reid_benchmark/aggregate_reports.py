from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .config import load_config
from .registry import MODEL_REGISTRY


SUMMARY_FIELDS = [
    "model",
    "model_name",
    "scenario",
    "run_status",
    "backend",
    "frames",
    "elapsed_seconds",
    "avg_live_fps",
    "avg_detector_ms",
    "avg_reid_ms",
    "avg_total_ms",
    "output_video",
    "timing_csv",
    "track_events_csv",
    "accuracy_note",
]


def load_status(models_dir: Path, key: str) -> dict:
    status_path = models_dir / key / "status.json"
    if not status_path.exists():
        return {}
    return json.loads(status_path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/benchmark.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    reports_dir = Path(config["paths"]["reports_dir"])
    models_dir = Path(config["paths"]["models_dir"])

    rows = []
    for key in config["models"]:
        spec = MODEL_REGISTRY.get(key)
        status = load_status(models_dir, key)
        model_dir = reports_dir / key
        summaries = sorted(model_dir.glob("*_summary.json"))
        if not summaries:
            rows.append(
                {
                    "model": key,
                    "model_name": spec.name if spec else key,
                    "scenario": "",
                    "run_status": "not_tested",
                    "backend": status.get("download_status", ""),
                    "frames": "",
                    "elapsed_seconds": "",
                    "avg_live_fps": "",
                    "avg_detector_ms": "",
                    "avg_reid_ms": "",
                    "avg_total_ms": "",
                    "output_video": "",
                    "timing_csv": "",
                    "track_events_csv": "",
                    "accuracy_note": status.get("notes", ""),
                }
            )
            continue
        for summary_path in summaries:
            item = json.loads(summary_path.read_text(encoding="utf-8"))
            rows.append({field: item.get(field, "") for field in SUMMARY_FIELDS})

    json_path = reports_dir / "all_model_summary.json"
    csv_path = reports_dir / "all_model_summary.csv"
    json_path.write_text(json.dumps({"runs": rows}, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"json": str(json_path), "csv": str(csv_path), "rows": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
