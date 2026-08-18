"""Guided face enrollment -- the reference FACE/live.py capture loop, headless.

The Windows tool was an OpenCV window: it watched the largest face, measured its
yaw, and filled a named person's frontal/left/right pose cells while the operator
turned their head, showing what was still missing. This is the same loop driving
the dashboard instead of a window, so the storage decisions stay in
PLATF/face_enroll_gallery.py's Gallery.consider() exactly as they were.

Why its own capture instead of the engine's observations: enrollment needs the
MEASURED yaw of a close-up subject, and the engine publishes only a face embedding
(pose is never computed in the hot path -- landmark_3d_68 is not loaded there). It
also needs clean pixels; the engine's /dev/shm frame has boxes and labels drawn on
it (DRAW_OVERLAY=1), which would be baked into a stored chip.

Enrollment only ever considers the LARGEST face in view. A bystander walking behind
the subject would otherwise be enrolled under the subject's name, which is the one
error a gallery cannot recover from on its own.
"""
from __future__ import annotations

import os
import threading
import time

import cv2

# Gate before a sample is even offered to the gallery, from the reference tool.
MIN_QUALITY = float(os.environ.get("ENROLL_MIN_Q", "0.25"))
# Consecutive video frames are the same instant; a burst would fill one mode with
# one pose. The reference rate-limited auto-capture to this.
CAPTURE_GAP_S = float(os.environ.get("ENROLL_CAPTURE_GAP_S", "0.7"))
DETECT_EVERY = int(os.environ.get("ENROLL_DETECT_EVERY", "2"))


class Stream:
    """Always hand back the newest frame.

    cv2 buffers RTSP internally, so a slow consumer drifts seconds behind and what
    the operator sees stops matching the room. A reader thread that keeps only the
    latest frame makes the session honest about what the camera sees now.
    """

    def __init__(self, src: str) -> None:
        self.cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG if "://" in src else cv2.CAP_ANY)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open {src}")
        self.live = "://" in src
        self.frame, self.ok, self.stop = None, True, False
        if self.live:
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while not self.stop:
            ok, f = self.cap.read()
            if not ok:
                self.ok = False
                time.sleep(0.2)
                continue
            self.frame = f

    def read(self):
        if not self.live:
            return self.cap.read()
        for _ in range(200):
            if self.stop:
                return False, None
            if self.frame is not None:
                return True, self.frame.copy()
            time.sleep(0.02)
        return self.ok, None

    def release(self) -> None:
        self.stop = True
        time.sleep(0.05)
        try:
            self.cap.release()
        except Exception:
            pass


class EnrollmentSession:
    """One person's enrollment run against one camera.

    Vectors are persisted AS THEY ARE ACCEPTED (the reference saved on every add):
    a crash or a closed browser at minute three must not throw away minutes one and
    two. `cancel()` therefore has to roll back what this session wrote, rather than
    simply dropping an unsaved buffer.
    """

    def __init__(self, adapter, name: str, camera: str, source: str,
                 core_factory=None, stream_factory=None) -> None:
        self.adapter = adapter
        self.name = name
        self.camera = camera
        self.source = source
        # Both injectable so the loop's gating, rollback and coverage can be tested
        # without loading models or opening a camera.
        self._core_factory = core_factory or self._default_core
        self._stream_factory = stream_factory or (lambda: Stream(self.source))
        self.state = "loading"          # loading | capturing | done | error
        self.error = None
        self.started = time.time()
        self.log: list = []             # newest last: {t, kind, text}
        self.live: dict = {}            # what the operator needs to see RIGHT NOW
        self.added: list = []           # chip_paths written by THIS session
        self.last_chip = None           # relative chip path, for the preview
        self.frames = 0
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @staticmethod
    def _default_core():
        from PLATF.face_core import FaceCore
        return FaceCore()

    def _say(self, kind: str, text: str) -> None:
        with self._lock:
            self.log.append({"t": round(time.time(), 1), "kind": kind, "text": text})
            del self.log[:-12]

    # -- the loop --------------------------------------------------------

    def _run(self) -> None:
        stream = None
        try:
            core = self._core_factory()
            self._say("info", "models ready")
            stream = self._stream_factory()
            with self._lock:
                self.state = "capturing"
        except Exception as exc:
            with self._lock:
                self.state, self.error = "error", f"{type(exc).__name__}: {exc}"
            self._say("bad", str(exc)[:80])
            if stream is not None:
                stream.release()
            return

        from PLATF.face_enroll_gallery import quality_score

        last_capture = 0.0
        obs: list = []
        n_frame = 0
        try:
            while not self._stop.is_set():
                ok, frame = stream.read()
                if not ok or frame is None:
                    self._say("bad", "stream ended")
                    break
                n_frame += 1
                # Only ever store a freshly analysed observation. A stale one is the
                # same instant re-offered, and would fill a mode with one pose.
                fresh = (n_frame % max(1, DETECT_EVERY) == 0) or not obs
                if fresh:
                    obs = core.analyse(frame)
                with self._lock:
                    self.frames = n_frame

                target = max(obs, key=lambda o: o.face_px, default=None)
                if target is None:
                    with self._lock:
                        self.live = {"face": False}
                    continue

                q = quality_score(target.sharp, target.face_px, target.det_score)
                with self._lock:
                    self.live = {
                        "face": True, "pose": target.pose, "yaw": round(target.yaw, 1),
                        "pitch": round(target.pitch, 1), "quality": round(float(q), 3),
                        "px": round(target.face_px, 1), "det": round(target.det_score, 3),
                        "ok": bool(target.usable and q >= MIN_QUALITY)}

                if not fresh:
                    continue
                if (time.time() - last_capture) <= CAPTURE_GAP_S:
                    continue
                if not target.usable:
                    continue
                if q < MIN_QUALITY:
                    continue

                last_capture = time.time()
                # A bug inside the gallery must not cost the operator a whole
                # session; report it and carry on.
                try:
                    r = self.adapter.consider(self.name, target)
                except Exception as exc:
                    self._say("bad", f"{type(exc).__name__}: {exc}"[:80])
                    continue
                if r.get("action") in ("add", "replace"):
                    with self._lock:
                        self.added.append(r["chip_path"])
                        self.last_chip = r["chip_path"]
                    self._say("good", f"{r['action']} {r.get('cell')}/m{r.get('mode')} "
                                      f"q{r.get('quality', 0):.2f}")
                else:
                    self._say("skip", str(r.get("reason", "skipped"))[:60])
        except Exception as exc:
            with self._lock:
                self.state, self.error = "error", f"{type(exc).__name__}: {exc}"
            self._say("bad", str(exc)[:80])
        finally:
            stream.release()
            with self._lock:
                if self.state == "capturing":
                    self.state = "done"

    # -- control ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
        with self._lock:
            if self.state in ("capturing", "loading"):
                self.state = "done"

    def rollback(self) -> int:
        """Remove everything THIS session stored. Used by cancel and retake."""
        with self._lock:
            paths = list(self.added)
            self.added.clear()
            self.last_chip = None
        n = self.adapter.drop_chips(paths) if paths else 0
        if n:
            self._say("info", f"rolled back {n} vector(s)")
        return n

    # -- what the UI reads ------------------------------------------------

    def status(self) -> dict:
        cov = self.adapter.coverage(self.name)
        missing = [c for c, v in cov.items() if not v["vectors"]]
        with self._lock:
            return {
                "state": self.state, "name": self.name, "camera": self.camera,
                "error": self.error, "coverage": cov, "missing": missing,
                "tip": ("all bins filled - keep going for extra modes" if not missing
                        else "TURN: " + ", ".join(missing)),
                "captured": len(self.added), "frames": self.frames,
                "live": dict(self.live), "log": list(self.log),
                "has_preview": bool(self.last_chip),
                "elapsed_s": round(time.time() - self.started, 1),
            }
