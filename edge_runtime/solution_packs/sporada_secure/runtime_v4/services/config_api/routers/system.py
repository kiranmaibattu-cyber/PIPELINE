"""System settings: the global inference backend mode.

GET  /api/system          -> current mode + per-stage backend map + options
PUT  /api/system/backend  -> set the mode (UI). Applying needs a worker restart;
                             the worker detects the change on its config poll and
                             re-execs onto the new engines.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..storage import MODE_BACKENDS, MODE_LABELS, SYSTEM_STORE, VALID_MODES

router = APIRouter(prefix="/api/system", tags=["system"])


class BackendModeRequest(BaseModel):
    backend_mode: str


def _payload(mode: str) -> dict:
    return {
        "backend_mode": mode,
        "backends": MODE_BACKENDS[mode],
        "available_modes": [
            {"value": m, "label": MODE_LABELS[m], "backends": MODE_BACKENDS[m]}
            for m in VALID_MODES
        ],
    }


@router.get("")
def get_system() -> dict:
    return _payload(SYSTEM_STORE.read()["backend_mode"])


@router.put("/backend")
def set_backend(req: BackendModeRequest) -> dict:
    if req.backend_mode not in VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown backend_mode {req.backend_mode!r}; valid: {list(VALID_MODES)}",
        )
    SYSTEM_STORE.set_mode(req.backend_mode)
    result = _payload(req.backend_mode)
    result["restart_required"] = True
    return result
