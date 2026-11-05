"""Frame decode for the OpenVINO worker.

This standalone decoder drives the Intel media engine via ffmpeg `-hwaccel vaapi`
and yields BGR frames to Python.

Key recipe (validated on this box): decode on the media engine, download **NV12**
(half the bytes of BGR) and convert to BGR with cv2 on our side. That beats both
software decode (~60 fps/stream) and the naive VA-API->bgr24 path (~35 fps, which
pays a full-size GPU->CPU readback + swscale): ~66 fps/stream. HW decode only wins
if you don't pay a full BGR readback.

`-re` makes a looped file behave like a real-time camera. V7.0 requires
hardware decode; OV_DECODE_HW=0 is rejected instead of silently using libav.
"""
from __future__ import annotations

from collections import deque
import ctypes
import json
import logging
import os
import signal
import subprocess
import threading
import time

import cv2
import numpy as np

from pipeline.runtime_status import DecoderRuntimeStatus
from pipeline.structured_logging import log_event, sanitize_text, set_stream_session_id

logger = logging.getLogger(__name__)


def _die_with_parent():
    """preexec_fn: make this child receive SIGKILL when its parent dies, so an
    ffmpeg decoder never orphans (even if the worker is SIGKILLed). Linux-only."""
    try:
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass

STREAM_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://", "tcp://")
_HW = os.getenv("OV_DECODE_HW", "1") != "0"
_VAAPI_DEVICE = os.getenv("VAAPI_DEVICE", "/dev/dri/renderD128")
_FFMPEG_THREADS = max(1, int(os.getenv("SENTINEL_FFMPEG_THREADS", "1")))
_OPENCV_THREADS = max(1, int(os.getenv("SENTINEL_OPENCV_THREADS", "1")))

# Every camera has its own process. Letting FFmpeg and OpenCV each create a
# host-sized CPU pool per camera exhausts the pod PID budget long before CPU,
# GPU, NPU or memory capacity is reached. VAAPI performs the actual decode, so
# one bounded CPU orchestration/filter thread is sufficient for this path.
cv2.setNumThreads(_OPENCV_THREADS)


def _network_input_options(uri: str, *, probe: bool) -> list[str]:
    lowered = uri.lower()
    if lowered.startswith("tcp://"):
        return ["-rw_timeout", "30000000"]
    if lowered.startswith(("rtsp://", "rtsps://")):
        timeout_option = "-rw_timeout" if probe else "-timeout"
        return ["-rtsp_transport", "tcp", timeout_option, "30000000"]
    if lowered.startswith(("http://", "https://")):
        return [
            "-rw_timeout", "30000000",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1",
            "-reconnect_on_http_error", "408,429,500,502,503,504",
            "-reconnect_delay_max", "10",
        ]
    if lowered.startswith(("rtmp://", "rtmps://", "rtmpt://", "rtmpts://")):
        return ["-rw_timeout", "30000000"]
    return []


def _probe_resolution(uri: str, *, camera_id: str = "unknown") -> tuple[int, int]:
    args = ["ffprobe", "-v", "error"]
    args += _network_input_options(uri, probe=True)
    args += ["-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "json", uri]
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    check=True, timeout=35)
            return _parse_probe_resolution(result.stdout)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as exc:
            stderr = sanitize_text(getattr(exc, "stderr", "") or "", limit=2048)
            if attempt == attempts:
                log_event(
                    logger, logging.ERROR, "decoder_probe_exhausted",
                    "resolution probe failed after all attempts",
                    camera_id=camera_id, attempt=attempt,
                    maximum_attempts=attempts,
                    retry_reason=type(exc).__name__,
                    sanitized_stderr=stderr or None,
                )
                raise RuntimeError(f"could not probe stream after {attempts} attempts") from exc
            delay = min(2 ** attempt, 10)
            log_event(
                logger, logging.WARNING, "decoder_probe_retry",
                "resolution probe failed; retry scheduled",
                camera_id=camera_id, attempt=attempt, maximum_attempts=attempts,
                retry_reason=type(exc).__name__, retry_backoff_seconds=delay,
                sanitized_stderr=stderr or None,
            )
            time.sleep(delay)


def _parse_probe_resolution(payload: str) -> tuple[int, int]:
    document = json.loads(payload)
    for stream in document.get("streams") or []:
        try:
            width = int(stream["width"])
            height = int(stream["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            return width, height
    raise ValueError("ffprobe returned no video dimensions")


def _ffmpeg_cmd(uri: str, fps: int, is_stream: bool) -> list[str]:
    if not _HW:
        raise RuntimeError("V7.0 requires VA-API hardware decode (OV_DECODE_HW must not be 0)")
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-threads", str(_FFMPEG_THREADS),
        "-filter_threads", str(_FFMPEG_THREADS),
        "-filter_complex_threads", str(_FFMPEG_THREADS),
    ]
    if _HW:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", _VAAPI_DEVICE,
                "-hwaccel_output_format", "vaapi"]
    if is_stream:
        cmd += _network_input_options(uri, probe=False)
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

    def __init__(self, uri: str, fps: int = 12, name: str = "?", status_callback=None,
                 stream_session_id: str | None = None):
        if not _HW:
            raise RuntimeError("V7.0 requires VA-API hardware decode (OV_DECODE_HW must not be 0)")
        self.uri = uri
        self.fps = int(fps)
        self.name = name
        self.is_stream = uri.startswith(STREAM_SCHEMES)
        self.status_callback = status_callback
        self.stream_session_id = str(stream_session_id or "unknown")
        self.runtime_status = DecoderRuntimeStatus(self.name, self.stream_session_id)
        self._probe_failed = False
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None
        backoff = 1.0
        while True:
            try:
                self.runtime_status.update(state="probing", reason="resolution_probe")
                self.w, self.h = _probe_resolution(uri, camera_id=self.name)
                break
            except Exception as exc:
                if not self.is_stream:
                    raise
                self._probe_failed = True
                self._status("unavailable", "stream_disconnected")
                self.runtime_status.increment(
                    "decoder_errors", state="unavailable", reason="probe_failed",
                    retry_reason=type(exc).__name__, retry_backoff_seconds=backoff,
                )
                log_event(
                    logger, logging.WARNING, "decoder_probe_failed",
                    "decoder probe failed; retry scheduled",
                    retry_reason=type(exc).__name__, retry_backoff_seconds=backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
        self._nv12 = _HW
        # NV12 is 1.5 bytes/px; BGR is 3 bytes/px
        self._fsize = self.w * self.h * 3 // 2 if self._nv12 else self.w * self.h * 3
        self._proc: subprocess.Popen | None = None

    def _spawn(self) -> None:
        self._stderr_lines.clear()
        self._proc = subprocess.Popen(
            _ffmpeg_cmd(self.uri, self.fps, self.is_stream),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=_die_with_parent,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name=f"ffmpeg-stderr-{self.name}", daemon=True
        )
        self._stderr_thread.start()
        self.runtime_status.increment(
            "decoder_starts", state="connecting", reason="ffmpeg_started",
            decoder_pid=self._proc.pid, last_ffmpeg_exit_code=None,
            retry_reason=None, retry_backoff_seconds=0.0, sanitized_stderr=None,
        )
        log_event(
            logger, logging.INFO, "decoder_started", "FFmpeg decoder started",
            decoder_pid=self._proc.pid, width=self.w, height=self.h,
            target_fps=self.fps, hardware_decode=self._nv12,
            ffmpeg_threads=_FFMPEG_THREADS, opencv_threads=_OPENCV_THREADS,
        )

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = proc.stderr.readline()
                if not line:
                    return
                cleaned = sanitize_text(line.decode("utf-8", errors="replace"), limit=1024).strip()
                if cleaned:
                    self._stderr_lines.append(cleaned)
        except (OSError, ValueError):
            return

    def _stderr_tail(self) -> str | None:
        value = " | ".join(self._stderr_lines)
        return sanitize_text(value, limit=4096) if value else None

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
        unavailable = self._probe_failed
        while True:
            if self._proc is None:
                self._spawn()
            frame = self._read_one()
            if frame is None:
                exit_code, stderr = self._finish_process()
                if not self.is_stream:
                    # file pipe ended unexpectedly (stream_loop should prevent this) — respawn
                    self.runtime_status.increment(
                        "decoder_errors", state="unavailable", reason="file_decoder_ended",
                        last_ffmpeg_exit_code=exit_code, sanitized_stderr=stderr,
                    )
                    continue
                self._status("unavailable", "stream_disconnected")
                unavailable = True
                self.runtime_status.increment(
                    "decoder_errors", state="unavailable", reason="stream_disconnected",
                    last_ffmpeg_exit_code=exit_code, retry_reason="stream_disconnected",
                    retry_backoff_seconds=backoff, sanitized_stderr=stderr,
                )
                self.runtime_status.increment("reconnects")
                log_event(
                    logger, logging.WARNING, "decoder_reconnect_scheduled",
                    "FFmpeg decoder ended; reconnect scheduled",
                    ffmpeg_exit_code=exit_code, sanitized_stderr=stderr,
                    retry_reason="stream_disconnected", retry_backoff_seconds=backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            backoff = 1.0
            if unavailable:
                self._status("healthy", "none")
                unavailable = False
            self.runtime_status.frame()
            yield frame

    def _status(self, state: str, reason: str) -> None:
        if self.status_callback is not None:
            updated_session = self.status_callback(state, reason)
            if updated_session and str(updated_session) != self.stream_session_id:
                previous_session = self.stream_session_id
                self.stream_session_id = str(updated_session)
                set_stream_session_id(self.stream_session_id)
                self.runtime_status.set_session(self.stream_session_id)
                log_event(
                    logger, logging.INFO, "decoder_recovered",
                    "decoder recovered with a new stream session",
                    previous_stream_session_id=previous_session,
                )

    def stop(self) -> None:
        self._finish_process()

    def _finish_process(self) -> tuple[int | None, str | None]:
        proc = self._proc
        if proc is None:
            return None, self._stderr_tail()
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            exit_code = proc.returncode
        except (OSError, ProcessLookupError):
            exit_code = proc.poll()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
        stderr = self._stderr_tail()
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._proc = None
        self._stderr_thread = None
        return exit_code, stderr
