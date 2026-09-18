"""Frame decode for the all-OpenVINO worker (no Axelera SDK / GStreamer).

The hybrid worker gets decode for free from the Voyager SDK's GStreamer pipeline.
This standalone decoder drives the Intel media engine via ffmpeg `-hwaccel vaapi`
and yields BGR frames to Python.

Key recipe (validated on this box): decode on the media engine, download **NV12**
(half the bytes of BGR) and convert to BGR with cv2 on our side. That beats both
software decode (~60 fps/stream) and the naive VA-API->bgr24 path (~35 fps, which
pays a full-size GPU->CPU readback + swscale): ~66 fps/stream. HW decode only wins
if you don't pay a full BGR readback.

`-re` makes a looped file behave like a real-time camera (so a 12fps cap means a
real 12 cam-frames/s), which is what the capacity probe needs. RTSP is already
real-time. Set OV_DECODE_HW=0 to fall back to software decode.
"""
from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def _die_with_parent():
    """preexec_fn: make this child receive SIGKILL when its parent dies, so an
    ffmpeg decoder never orphans (even if the worker is SIGKILLed). Linux-only."""
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass

STREAM_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")
_HW = os.getenv("OV_DECODE_HW", "1") != "0"
_VAAPI_DEVICE = os.getenv("VAAPI_DEVICE", "/dev/dri/renderD128")


def _probe_resolution(uri: str) -> tuple[int, int]:
    args = ["ffprobe", "-v", "error"]
    if uri.startswith(("rtsp://", "rtsps://")):
        args += ["-rtsp_transport", "tcp", "-rw_timeout", "30000000"]
    args += ["-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", uri]
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    check=True, timeout=35)
            out = result.stdout.strip()
            w, h = out.split("x")[:2]
            return int(w), int(h)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError) as exc:
            if attempt == attempts:
                raise RuntimeError(f"could not probe stream after {attempts} attempts") from exc
            delay = min(2 ** attempt, 10)
            logger.warning("resolution probe failed for %s (attempt %d/%d); retrying in %ss",
                           uri, attempt, attempts, delay)
            time.sleep(delay)


def _ffmpeg_cmd(uri: str, fps: int, is_stream: bool) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if _HW:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", _VAAPI_DEVICE,
                "-hwaccel_output_format", "vaapi"]
    if is_stream:
        # Ubuntu's FFmpeg accepts the RTSP demuxer option as `timeout`; its
        # FFprobe accepts `rw_timeout`. Keep the two commands intentionally
        # different so the decoder does not exit immediately with option errors.
        cmd += ["-rtsp_transport", "tcp", "-timeout", "30000000"]
    else:
        cmd += ["-re", "-stream_loop", "-1"]   # pace a file like a live camera, loop forever
    cmd += ["-i", uri]
    # cap to target fps, then (if HW) download the surface as NV12
    vf = f"fps={fps}"
    if _HW:
        vf += ",hwdownload,format=nv12"
        pix = "nv12"
    else:
        pix = "bgr24"
    cmd += ["-vf", vf, "-pix_fmt", pix, "-f", "rawvideo", "-"]
    return cmd


class FfmpegDecoder:
    """Iterate BGR frames from one source at `fps`. HW (VA-API/NV12) by default;
    reconnects RTSP and loops files. Iterating yields numpy BGR (H,W,3)."""

    def __init__(self, uri: str, fps: int = 12, name: str = "?"):
        self.uri = uri
        self.fps = int(fps)
        self.name = name
        self.is_stream = uri.startswith(STREAM_SCHEMES)
        self.w, self.h = _probe_resolution(uri)
        self._nv12 = _HW
        # NV12 is 1.5 bytes/px; BGR is 3 bytes/px
        self._fsize = self.w * self.h * 3 // 2 if self._nv12 else self.w * self.h * 3
        self._proc: subprocess.Popen | None = None

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            _ffmpeg_cmd(self.uri, self.fps, self.is_stream),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )
        logger.info("decode[%s] %dx%d @%dfps hw=%s", self.name, self.w, self.h, self.fps, self._nv12)

    def _read_one(self):
        assert self._proc and self._proc.stdout
        buf = self._proc.stdout.read(self._fsize)
        if len(buf) < self._fsize:
            return None
        if self._nv12:
            yuv = np.frombuffer(buf, np.uint8).reshape(self.h * 3 // 2, self.w)
            return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
        return np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3).copy()

    def frames(self):
        """Yield BGR frames forever (files loop; RTSP reconnects with backoff)."""
        backoff = 1.0
        while True:
            if self._proc is None:
                self._spawn()
            frame = self._read_one()
            if frame is None:
                self.stop()
                if not self.is_stream:
                    # file pipe ended unexpectedly (stream_loop should prevent this) — respawn
                    continue
                logger.warning("decode[%s] stream ended; reconnecting in %.0fs", self.name, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            backoff = 1.0
            yield frame

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
