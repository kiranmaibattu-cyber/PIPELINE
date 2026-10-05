"""Config API for the ALPR worker + UI.

Responsibilities:
  - GET /api/cameras/processor-config  (worker pulls; schema matches today's
    response so worker code is unchanged)
  - GET/PUT/POST/PATCH /api/cameras    (UI authors)
  - POST /api/cameras/.../geometry     (UI ZoneStudio writes lines/zones/masks)
  - WS  /api/events/ws                 (Redis → browser bridge)

Persistence is a single JSON file (services/config_api/storage.py), atomically
replaced on every write. The worker watches that file's mtime (and the backend
mode) every ~5 s and re-execs itself on a change, so UI edits to cameras, zones,
use cases, or backend mode land in the running pipeline within ~5 s.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import analytics, cameras, health, system, zones

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="ALPR Config API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cameras.router)
app.include_router(zones.router)
app.include_router(analytics.router)
app.include_router(health.router)
app.include_router(system.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
