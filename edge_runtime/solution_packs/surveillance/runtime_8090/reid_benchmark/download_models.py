from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import ensure_dirs, load_config
from .registry import model_dir, selected_models


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900, env: dict[str, str] | None = None) -> tuple[bool, str]:
    try:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode == 0, proc.stdout[-8000:]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def clone_repo(repo_url: str, dest: Path) -> tuple[str, str]:
    if dest.exists():
        return "already_present", f"Repo exists at {dest}"
    ok, output = run(["git", "clone", "--depth", "1", repo_url, str(dest)])
    return ("downloaded" if ok else "failed", output)


def probe_torchreid(spec_key: str, dest: Path, torch_home: Path) -> tuple[str, str]:
    model_map = {
        "osnet_ain": "osnet_ain_x1_0",
        "osnet_x1_0": "osnet_x1_0",
        "osnet_x0_75": "osnet_x0_75",
        "osnet_x0_5": "osnet_x0_5",
    }
    model_name = model_map[spec_key]
    code = f"""
from pathlib import Path
import torchreid
Path({str(dest)!r}).mkdir(parents=True, exist_ok=True)
model = torchreid.models.build_model(name={model_name!r}, num_classes=1000, pretrained=True)
print(type(model).__name__)
"""
    ok, output = run([sys.executable, "-c", code], timeout=1200, env={"TORCH_HOME": str(torch_home)})
    return ("downloaded" if ok else "failed", output)


def probe_openvino(dest: Path) -> tuple[str, str]:
    downloader = shutil_which("omz_downloader")
    if not downloader:
        return "requires_tooling", "omz_downloader is not installed. Install openvino-dev to download Open Model Zoo models."
    ok, output = run(
        [
            downloader,
            "--name",
            "person-reidentification-retail-0288",
            "--output_dir",
            str(dest),
        ],
        timeout=1200,
    )
    return ("downloaded" if ok else "failed", output)


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def download_one(spec, config: dict) -> dict:
    started = time.time()
    models_root = Path(config["paths"]["models_dir"])
    repos_root = Path(config["paths"]["repos_dir"])
    dest = model_dir(models_root, spec.key)
    dest.mkdir(parents=True, exist_ok=True)

    status = "unavailable"
    detail = ""
    if spec.loader == "torchreid":
        status, detail = probe_torchreid(spec.key, dest, models_root / "torch_cache")
    elif spec.loader == "openvino":
        status, detail = probe_openvino(dest)
    elif spec.repo_url:
        status, detail = clone_repo(spec.repo_url, repos_root / spec.key)
        if status in {"downloaded", "already_present"}:
            status = "repo_only"
            detail += "\nRepo cloned/present; pretrained checkpoint must be verified manually or by a model-specific adapter."
    else:
        status = "requires_credentials" if "NVIDIA" in spec.name else "no_verified_weights"
        detail = spec.notes or "No direct public checkpoint URL is configured."

    payload = {
        "key": spec.key,
        "name": spec.name,
        "priority": spec.priority,
        "target": spec.target,
        "loader": spec.loader,
        "download_status": status,
        "repo_url": spec.repo_url,
        "weights_url": spec.weights_url,
        "notes": spec.notes,
        "elapsed_seconds": round(time.time() - started, 3),
        "detail": detail,
    }
    write_status(dest / "status.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/benchmark.yaml")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    config = load_config(args.config)
    ensure_dirs(config)
    specs = selected_models(config["models"])
    statuses = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(download_one, spec, config): spec for spec in specs}
        for future in as_completed(futures):
            statuses.append(future.result())
    statuses = sorted(statuses, key=lambda item: item["priority"])
    reports_dir = Path(config["paths"]["reports_dir"])
    write_status(reports_dir / "model_download_status.json", {"models": statuses})
    print(json.dumps({"models": statuses}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
