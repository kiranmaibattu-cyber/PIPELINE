from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .config import ensure_dirs, load_config


ANNOTATION_COLUMNS = ["scenario", "model", "camera", "global_id", "person_id", "notes"]


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    pd.DataFrame(columns=ANNOTATION_COLUMNS).to_csv(path, index=False)


def score(df: pd.DataFrame) -> dict:
    required = set(ANNOTATION_COLUMNS[:-1])
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Annotation file missing columns: {sorted(missing)}")
    df = df.dropna(subset=["scenario", "model", "global_id", "person_id"]).copy()
    if df.empty:
        return {"status": "empty", "message": "No annotation rows found."}
    df["global_id"] = df["global_id"].astype(str)
    df["person_id"] = df["person_id"].astype(str)

    rows = []
    for (scenario, model), group in df.groupby(["scenario", "model"]):
        total = len(group)
        person_majority = group.groupby("person_id")["global_id"].agg(lambda s: s.value_counts().iloc[0]).sum()
        global_majority = group.groupby("global_id")["person_id"].agg(lambda s: s.value_counts().iloc[0]).sum()
        unique_people = group["person_id"].nunique()
        unique_global_ids = group["global_id"].nunique()
        rows.append(
            {
                "scenario": scenario,
                "model": model,
                "annotations": int(total),
                "unique_people": int(unique_people),
                "unique_global_ids": int(unique_global_ids),
                "person_consistency_accuracy": round(float(person_majority) / total, 4),
                "global_id_purity": round(float(global_majority) / total, 4),
                "split_error_count": int(max(0, unique_global_ids - unique_people)),
            }
        )
    return {"status": "scored", "runs": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/benchmark.yaml")
    parser.add_argument("--annotations", default="annotations/annotations.csv")
    args = parser.parse_args()

    config = load_config(args.config)
    ensure_dirs(config)
    annotation_path = Path(args.annotations)
    write_template(annotation_path)

    result = score(pd.read_csv(annotation_path))
    output = Path(config["paths"]["reports_dir"]) / "annotation_accuracy.json"
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
