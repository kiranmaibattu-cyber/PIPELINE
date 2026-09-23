"""Zone/line/mask CRUD for the UI ZoneStudio page."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import ZoneUpsert
from ..storage import STORE as store

router = APIRouter()


@router.post("/api/cameras/{camera_id}/use-cases/{use_case}/geometry")
def upsert_geometry(camera_id: str, use_case: str, body: ZoneUpsert):
    try:
        return store.upsert_geometry(
            camera_id=camera_id,
            use_case=use_case,
            kind=body.kind,
            geometry=body.geometry.model_dump(mode="json", exclude_none=True),
        )
    except KeyError:
        raise HTTPException(404, f"camera {camera_id} not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/cameras/{camera_id}/use-cases/{use_case}/geometry/{kind}/{geometry_id}")
def delete_geometry(camera_id: str, use_case: str, kind: str, geometry_id: str):
    try:
        store.delete_geometry(camera_id, use_case, kind, geometry_id)
    except KeyError:
        raise HTTPException(404, f"camera {camera_id} not found")
    return {"deleted": geometry_id}
