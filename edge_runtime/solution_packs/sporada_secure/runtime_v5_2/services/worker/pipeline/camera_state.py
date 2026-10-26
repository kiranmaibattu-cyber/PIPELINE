"""Atomic camera readiness and applied-revision acknowledgements."""
from __future__ import annotations
import json
import os
from pathlib import Path


def publish_camera_state(name: str, camera_config: dict, path_kind: str) -> None:
    directory = os.getenv('APEXFABRIC_CAMERA_READY_DIR')
    if not directory:
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    revision = int((((camera_config.get('analytics') or {}).get('v5_events') or {}).get('v5') or {}).get('config_revision', 0))
    payload = {'revision': revision, 'processing_path': path_kind}
    temporary = root / f'.{name}.revision.tmp'
    temporary.write_text(json.dumps(payload), encoding='utf-8')
    temporary.replace(root / f'{name}.revision')
    temporary = root / f'.{name}.ready.tmp'
    temporary.write_text('ready\n', encoding='utf-8')
    temporary.replace(root / f'{name}.ready')
