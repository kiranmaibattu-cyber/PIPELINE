"""Download weights for the new Stage-1 embedders.

Direct-download (fastreid GitHub release): agw, mgn — tries MSMT17 first,
falls back to Market1501 naming if the MSMT release asset doesn't exist.
GDrive-hosted (manual or gdown): transreid_ssl, solider — attempted via gdown
if installed, otherwise prints instructions.

Usage:  python -m MTMC.download_new_models
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
_MODELS = _ROOT / "models"
RELEASE = "https://github.com/JDAI-CV/fast-reid/releases/download/v0.1.1"

FASTREID_CANDIDATES = {
    "agw": ["msmt_agw_R50.pth", "market_agw_R50.pth", "duke_agw_R50.pth"],
    "mgn": ["msmt_mgn_R50-ibn.pth", "market_mgn_R50-ibn.pth", "duke_mgn_R50-ibn.pth"],
}

GDRIVE_NOTES = {
    "transreid_ssl": (
        _MODELS / "transreid_ssl" / "vit_small_ics_msmt17.pth",
        "https://github.com/damo-cv/TransReID-SSL — download 'ViT-S/16+ICS MSMT17' from the README GDrive table",
    ),
    "solider": (
        _MODELS / "solider" / "swin_small_msmt17.pth",
        "https://github.com/tinyvision/SOLIDER-REID — download 'Swin-S MSMT17' from the README GDrive table",
    ),
}


def _download(url: str, dest: Path) -> bool:
    print(f"  trying {url} ...", flush=True)
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            if r.status_code != 200:
                print(f"    HTTP {r.status_code}")
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            n = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    n += len(chunk)
            tmp.rename(dest)
            print(f"    OK — {n / 1e6:.1f} MB -> {dest}")
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"    failed: {exc}")
        return False


def main() -> int:
    from MTMC.new_models import NEW_MODEL_WEIGHTS

    any_fail = False

    for key, candidates in FASTREID_CANDIDATES.items():
        dest = NEW_MODEL_WEIGHTS[key]["path"]
        if dest.exists():
            print(f"[{key}] already present: {dest}")
            continue
        print(f"[{key}]")
        got = None
        for name in candidates:
            if _download(f"{RELEASE}/{name}", dest.parent / name):
                got = dest.parent / name
                break
        if got is None:
            print(f"  !! no release asset found for {key}")
            any_fail = True
        elif got != dest:
            # update expected path to whichever dataset variant we found
            got.replace(dest.parent / dest.name) if got.name == dest.name else None
            if got.name != dest.name:
                print(f"  note: saved as {got.name}; update NEW_MODEL_WEIGHTS[{key}] path/config to match")

    for key, (dest, note) in GDRIVE_NOTES.items():
        if dest.exists():
            print(f"[{key}] already present: {dest}")
            continue
        print(f"[{key}] manual download needed:\n  {note}\n  save to: {dest}")
        any_fail = True

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
