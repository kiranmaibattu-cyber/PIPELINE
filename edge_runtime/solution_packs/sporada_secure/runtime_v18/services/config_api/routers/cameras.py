"""Cameras + processor-config endpoints.

`GET /api/cameras/processor-config` returns the schema the worker already
consumes (see services/worker/pipeline/processor_config.py:210). Mounting
this on the worker URL means we don't touch worker code.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse

from ..models import Camera, CameraCollection
from ..storage import STORE as store

logger = logging.getLogger(__name__)

router = APIRouter()

_LIVE_REDIS = None


def _live_redis():
    """Binary redis client for the on-demand MJPEG live view (shared with worker)."""
    global _LIVE_REDIS
    if _LIVE_REDIS is None:
        import redis
        _LIVE_REDIS = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
        )
    return _LIVE_REDIS

SNAPSHOT_TIMEOUT_S = float(os.getenv("SNAPSHOT_TIMEOUT_S", "10"))
# Source videos dir; snapshots come straight from the camera source (file/RTSP).
VIDEO_DIR = os.getenv("VIDEO_DIR", "")
STREAM_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")


def _source_input(camera: Dict[str, Any]) -> Optional[str]:
    """Resolve the camera's own source to an ffmpeg input: RTSP/HTTP streams as-is,
    relative file uris against VIDEO_DIR. None if it can't be resolved. This lets
    ZoneStudio snapshot the real camera without the inference worker running."""
    uri = (camera.get("source") or {}).get("uri")
    if not uri:
        return None
    if uri.startswith(STREAM_SCHEMES):
        return uri
    if not os.path.isabs(uri) and VIDEO_DIR:
        uri = os.path.join(VIDEO_DIR, uri)
    return uri if os.path.exists(uri) else None


def _grab_jpeg(stream_url: str) -> Optional[bytes]:
    """Pull a single JPEG frame via ffmpeg from an RTSP/HTTP stream or a local
    file. Returns None on any failure so the caller can fall back gracefully.
    For files we seek ~2s in to skip black intro frames."""
    rtsp = stream_url.startswith(("rtsp://", "rtsps://"))
    is_file = not stream_url.startswith(STREAM_SCHEMES)
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        *(["-rtsp_transport", "tcp"] if rtsp else []),
        *(["-ss", "2"] if is_file else []),
        "-i", stream_url,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=SNAPSHOT_TIMEOUT_S,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg not found on PATH; snapshot unavailable")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("snapshot ffmpeg timed out for %s", stream_url)
        return None
    if result.returncode != 0 or not result.stdout:
        logger.warning(
            "snapshot ffmpeg failed (rc=%s) for %s: %s",
            result.returncode,
            stream_url,
            (result.stderr or b"").decode("utf-8", "replace")[:300],
        )
        return None
    return result.stdout


@router.get("/api/cameras/processor-config")
def processor_config() -> Dict[str, Any]:
    return store.read()


@router.get("/api/cameras")
def list_cameras() -> Dict[str, Any]:
    return store.read()


@router.put("/api/cameras")
def replace_cameras(body: CameraCollection) -> Dict[str, Any]:
    return store.replace_cameras(body.model_dump(mode="json", exclude_none=True))


@router.post("/api/cameras")
def add_camera(body: Camera) -> Dict[str, Any]:
    data = store.read()
    cameras = data.setdefault("cameras", [])
    if any(camera.get("camera_id") == body.camera_id for camera in cameras):
        raise HTTPException(409, f"camera {body.camera_id} already exists")
    cameras.append(body.model_dump(mode="json", exclude_none=True))
    store.replace_cameras(data)
    return body.model_dump(mode="json", exclude_none=True)


@router.patch("/api/cameras/{camera_id}")
def patch_camera(camera_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return store.update_camera(camera_id, patch)
    except KeyError:
        raise HTTPException(404, f"camera {camera_id} not found")


@router.get("/api/cameras/{camera_id}/snapshot")
def camera_snapshot(camera_id: str) -> Response:
    """Return one JPEG still grabbed straight from the camera source so the
    ZoneStudio editor can draw on the real scene instead of a blank canvas.

    Graceful by design: if ffmpeg or the source is unavailable we return 503
    (the UI just shows a blank editor in that case). The processing
    width/height are reported via headers so ZoneEditor can scale drawn points
    to the worker's frame coordinates.
    """
    data = store.read()
    camera = next(
        (cam for cam in data.get("cameras", []) if cam.get("camera_id") == camera_id),
        None,
    )
    if camera is None:
        raise HTTPException(404, f"camera {camera_id} not found")

    # Grab one frame straight from the camera source (file or RTSP) — works
    # without the worker running, so ZoneStudio always has a frame to draw on.
    source = _source_input(camera)
    jpeg = _grab_jpeg(source) if source is not None else None
    if jpeg is None:
        raise HTTPException(503, f"snapshot unavailable for camera {camera_id}")

    processing = camera.get("processing") or {}
    source = camera.get("source") or {}
    headers: Dict[str, str] = {"Cache-Control": "no-store"}
    width = processing.get("width") or source.get("width")
    height = processing.get("height") or source.get("height")
    if width:
        headers["X-Frame-Width"] = str(int(width))
    if height:
        headers["X-Frame-Height"] = str(int(height))

    return Response(content=jpeg, media_type="image/jpeg", headers=headers)


@router.get("/api/cameras/{camera_id}/live")
def camera_live(camera_id: str) -> StreamingResponse:
    """On-demand annotated MJPEG. Opening this sets live:want:<cam> so the worker
    starts drawing detections on that camera and pushing JPEGs to redis; we poll
    them out fast and stream as soon as a new frame lands (low latency). On
    disconnect the flag is cleared so the worker stops."""
    data = store.read()
    camera = next(
        (cam for cam in data.get("cameras", []) if cam.get("camera_id") == camera_id),
        None,
    )
    if camera is None:
        raise HTTPException(404, f"camera {camera_id} not found")
    name = camera.get("name") or camera_id
    r = _live_redis()

    async def gen():
        last = None
        ticks = 0
        try:
            while True:
                try:
                    if ticks % 20 == 0:                  # refresh the want ~1/s
                        r.setex(f"live:want:{name}", 10, b"1")
                    jpeg = r.get(f"live:frame:{name}")
                except Exception:  # noqa: BLE001
                    jpeg = None
                if jpeg and jpeg != last:
                    last = jpeg
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                ticks += 1
                await asyncio.sleep(0.05)                 # ~20 Hz poll -> push new frames promptly
        finally:
            try:
                r.delete(f"live:want:{name}")
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")
