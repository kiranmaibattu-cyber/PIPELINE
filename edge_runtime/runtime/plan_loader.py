"""Runtime-plan loading helpers for solution-pack containers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RuntimePlanLoader:
    def load(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"runtime plan not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
