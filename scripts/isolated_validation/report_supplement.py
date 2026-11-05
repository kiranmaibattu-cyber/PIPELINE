"""Offline report facts; no inference, threshold changes or automatic readiness claim."""
from pathlib import Path
import collections
import datetime
import gzip
import json
import math
import statistics
import sys

root = Path(sys.argv[1])


def describe(values):
    values = sorted(x for x in values if isinstance(x, (int, float)) and math.isfinite(x))
    if not values:
        return {"samples": 0}
    return {"samples": len(values), "mean": statistics.mean(values),
            **{name: values[round((len(values) - 1) * q)] for name, q in
               [("min", 0), ("p50", .5), ("p95", .95), ("p99", .99), ("max", 1)]}}


def slope(points):
    if len(points) < 2:
        return None
    xs = [(x - points[0][0]) / 60 for x, _ in points]
    ys = [y for _, y in points]
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((x - xm) ** 2 for x in xs)
    return sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / denominator if denominator else None


out = {"scope": "Offline reporting only. Memory slopes include allocator/state effects; "
       "stage distributions describe cached window means, not individual model-call percentiles.",
       "hours": [], "reconfiguration": [], "comparisons": []}
ledger = json.loads((root / "ledger.json").read_text())
for row in ledger:
    p = root / "artifacts" / row["case"]
    if any(x in row["case"] for x in ["compare-shared-background", "frozen-reference"]):
        hp = p / "healthz-final.json"
        if hp.exists():
            h = json.loads(hp.read_text())
            cameras = []
            for c in h.get("telemetry", {}).get("cameras", []):
                total = c.get("processing", {}).get("totals", {}).get("inference_frames", 0)
                stage_facts = {}
                for name, v in c.get("use_case_processing", {}).items():
                    if any(x in name for x in ["gait", "frame_conversion", "body", "scene"]):
                        calls, mean = v.get("calls_total", 0), v.get("lifetime_avg_ms")
                        stage_facts[name] = {"calls": calls, "lifetime_mean_ms_per_call": mean,
                            "attributed_wall_ms_per_inference_frame": calls * mean / total if total and mean is not None else None}
                cameras.append({"camera_id": c["camera_id"], "inference_frames": total, "stages": stage_facts})
            out["comparisons"].append({"case": row["case"], "cameras": cameras,
                "note": "Individual stage work estimates overlap and must not be summed; background rollback changes learning cadence and mask content."})
    if any(x in row["case"] for x in ["secret-url-rotation", "camera-add-remove"]):
        counts, dims = collections.Counter(), collections.Counter()
        capture = p / "packages.jsonl.gz"
        if capture.exists():
            for line in gzip.open(capture, "rt"):
                d = json.loads(line)
                counts[(d.get("camera_id"), d.get("config_revision"))] += 1
        for manifest in (p / "gallery").glob("*/manifest.json"):
            d = json.loads(manifest.read_text())
            if d["observation"]["event_type"] == "scene_sample":
                for e in d["evidence"]:
                    m = e["metadata"]
                    dims[(m["width"], m["height"])] += 1
        out["reconfiguration"].append({"case": row["case"],
            "accepted_camera_revision_counts": [{"camera_id": k[0], "revision": k[1], "count": v} for k, v in counts.items()],
            "scene_dimensions_counts": [{"width": k[0], "height": k[1], "count": v} for k, v in dims.items()]})
    if row["config"].get("steady_seconds", 0) < 3600 or "yuv420" not in row["case"]:
        continue
    report = json.loads((p / "report.json").read_text())
    samples = [s for s in report["samples"] if s.get("sample_phase") == "steady"]
    start, end = samples[0]["sample_monotonic_seconds"], samples[-1]["sample_monotonic_seconds"]
    memory = [(s["sample_monotonic_seconds"], s["container_memory_mb"]) for s in samples if s.get("container_memory_mb") is not None]
    hour = {"case": row["case"], "steady_samples": len(samples), "sample_span_seconds": end - start,
            "memory_MiB": describe([y for _, y in memory]), "first_memory_MiB": memory[0][1],
            "last_memory_MiB": memory[-1][1], "memory_slope_MiB_per_minute": slope(memory),
            "last_10min_memory_slope_MiB_per_minute": slope([(x, y) for x, y in memory if x >= end - 600]),
            "last_30min_memory_slope_MiB_per_minute": slope([(x, y) for x, y in memory if x >= end - 1800]),
            "checkpoints": [], "steady_resources": {}, "stage_window_mean_distributions_ms": {}}
    for minute in [0, 10, 20, 30, 40, 50, 60]:
        s = min(samples, key=lambda s: abs(s["sample_monotonic_seconds"] - start - minute * 60))
        hour["checkpoints"].append({"requested_minute": minute,
            "actual_minute": (s["sample_monotonic_seconds"] - start) / 60,
            "memory_MiB": s.get("container_memory_mb"), "cpu_cores": s.get("container_cpu_cores"),
            "cameras": s.get("per_camera"), "handoff": s.get("handoff")})
    resources, stages, seen = collections.defaultdict(list), collections.defaultdict(list), set()
    with gzip.open(p / "monitor.jsonl.gz", "rt") as f:
        for line in f:
            m = json.loads(line)
            if not start <= m["monotonic"] <= end:
                continue
            for t in m.get("temperatures", {}).get("coretemp", []):
                if t["label"] == "Package id 0":
                    resources["CPU_package_C"].append(t["current"])
            for v in m.get("power", {}).values():
                if v.get("name") == "package-0":
                    resources["package_W"].append(v["watts"])
            for device in ["GPU", "NPU"]:
                resources[device + "_node_activity_pct"].append(m.get("accelerators", {}).get(device, {}).get("hardware_utilization_pct"))
            for c in m.get("health", {}).get("telemetry", {}).get("cameras", []):
                key = (c["camera_id"], c.get("started_at"), c.get("observed_at"))
                if key in seen:
                    continue
                seen.add(key)
                for name, v in c.get("use_case_processing", {}).items():
                    if v.get("calls_in_window", 0) and v.get("avg_ms") is not None:
                        stages[(c["camera_id"], name)].append(v["avg_ms"])
    hour["steady_resources"] = {k: describe(v) for k, v in resources.items()}
    hour["stage_window_mean_distributions_ms"] = {cam + "/" + name: describe(v) for (cam, name), v in stages.items()}
    out["hours"].append(hour)
(root / "report-supplement.json").write_text(json.dumps(out, indent=2))
print(json.dumps({"hours": len(out["hours"]), "reconfiguration_cases": len(out["reconfiguration"])}))
