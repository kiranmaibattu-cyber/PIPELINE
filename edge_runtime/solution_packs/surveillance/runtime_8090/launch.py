"""Launch the copied 8090 runtime from a compiled surveillance graph plan."""
from __future__ import annotations

import argparse
import os
import runpy
import shutil
import sys
from pathlib import Path

from edge_runtime.runtime.plan_loader import RuntimePlanLoader
from edge_runtime.runtime.source_resolver import CameraSourceResolver
from edge_runtime.solution_packs.surveillance.runtime.config_adapter import (
    SurveillanceConfigAdapter,
)


RUNTIME_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare and launch 8090 surveillance runtime")
    parser.add_argument("--plan", default="/plans/surveillance.runtime_plan.json")
    parser.add_argument("--generated-dir", default="/generated/surveillance")
    parser.add_argument("--state-dir", default="/state/surveillance")
    parser.add_argument("--models-dir", default="/models/surveillance")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PLATF_PORT", "8090")))
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_dir = Path(args.generated_dir)
    state_dir = Path(args.state_dir)
    models_dir = Path(args.models_dir)

    plan = CameraSourceResolver().resolve_plan(RuntimePlanLoader().load(Path(args.plan)))
    SurveillanceConfigAdapter().write(plan, generated_dir)
    _install_runtime_config(generated_dir)
    _prepare_model_mount(models_dir)
    _configure_environment(plan, state_dir, models_dir, args.port)

    streams_path = generated_dir / "streams.generated.yaml"
    print(f"surveillance 8090 config prepared in {generated_dir.resolve()}", flush=True)
    if args.prepare_only:
        return 0

    sys.path.insert(0, str(RUNTIME_ROOT))
    os.chdir(RUNTIME_ROOT)
    sys.argv = [
        "PLATF.app",
        "--streams",
        str(streams_path),
        "--crops",
        str(state_dir / "crops"),
        "--port",
        str(args.port),
    ]
    runpy.run_module("PLATF.app", run_name="__main__")
    return 0


def _install_runtime_config(generated_dir: Path) -> None:
    target_dir = RUNTIME_ROOT / "PLATF" / "config"
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        generated_dir / "runtime_usecases.generated.json",
        target_dir / "runtime_usecases.json",
    )


def _prepare_model_mount(models_dir: Path) -> None:
    runtime_models = RUNTIME_ROOT / "models"
    if runtime_models.exists():
        return
    runtime_models.symlink_to(models_dir, target_is_directory=True)


def _configure_environment(plan, state_dir: Path, models_dir: Path, port: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PORT", str(port))
    os.environ.setdefault("PLATF_PORT", str(port))
    os.environ.setdefault("APP_TAG", "surveillance")
    os.environ.setdefault("PLATF_CROPS", str(state_dir / "crops"))
    if _uses_live_rtsp(plan):
        os.environ.setdefault("VIDEO_CLOCK", "0")

    os.environ.setdefault("DET_DEV", "GPU")
    os.environ.setdefault("EMB_DEV", "NPU")
    os.environ.setdefault("FACE_DEV", "GPU")
    os.environ.setdefault("GAIT_DEV", "NPU")
    os.environ.setdefault("SEG_DEV", "GPU")

    os.environ.setdefault("DET_MODEL", str(models_dir / "yolo11s_int8.xml"))
    os.environ.setdefault("EMB_MODEL", str(models_dir / "transreid_ssl_int8.xml"))
    os.environ.setdefault("FACE_MODEL", str(models_dir / "adaface_ir101_int8.xml"))
    os.environ.setdefault("GAIT_MODEL", str(models_dir / "gaitbase_int8.xml"))
    os.environ.setdefault("SEG_MODEL", str(models_dir / "yolov8n_seg_int8.xml"))

    os.environ.setdefault("FACE_GALLERY_DIR", str(state_dir / "face_gallery"))
    os.environ.setdefault("REID_GALLERY_DIR", str(state_dir / "reid_gallery"))
    os.environ.setdefault("HISTORY_DIR", str(state_dir / "history"))
    os.environ.setdefault("PLATF_HISTORY_DIR", str(state_dir / "history"))
    os.environ.setdefault("MANAGEMENT_EVENTS_PATH", str(state_dir / "events.jsonl"))
    os.environ.setdefault("MANAGEMENT_SNAPSHOT_DIR", str(state_dir / "snapshots"))

    # The copied 8090 runtime was tuned for a larger long-running host and defaults
    # to four model-server replicas for detector/embed/face. On this edge image the
    # graph compiler already shares model services across cameras, so start with
    # one compiled model per role.
    os.environ.setdefault("POOL_N_DET", "1")
    os.environ.setdefault("POOL_N_EMBED", "1")
    os.environ.setdefault("POOL_N_FACE", "1")
    os.environ.setdefault("POOL_N_GAIT", "1")
    os.environ.setdefault("POOL_DET_BATCH", "0")
    os.environ.setdefault("POOL_FACE_BATCH", "0")
    os.environ.setdefault("POOL_BATCH_MAX", "8")
    os.environ.setdefault("FRAME_SHM_BYTES", str(3840 * 2160 * 3))


def _uses_live_rtsp(plan) -> bool:
    return any(
        str(camera.get("source", "")).lower().startswith("rtsp://")
        for camera in plan.get("cameras", [])
    )


if __name__ == "__main__":
    raise SystemExit(main())
