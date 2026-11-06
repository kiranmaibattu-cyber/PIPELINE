"""One native GStreamer pipeline containing independent camera branches.

DL Streamer model sharing and cross-stream scheduling are process-local.  The
production fleet therefore places the complete per-camera source graphs in one
parent :class:`Gst.Pipeline` while retaining a separate ``Gst.Bin`` (and hence
separate decoder, motion state, tracker and sinks) for every camera.

The wrapper exposes the small ``Gst.Pipeline`` surface used by the camera
worker. RTSP reconnects replace the network session only: VA decoders and
shared inference stay PLAYING. A failed native graph is fenced and rebuilt
by the separate process supervisor rather than recycled through READY.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from pipeline.structured_logging import log_event, sanitize_text


logger = logging.getLogger(__name__)


class _BranchBus:
    """Camera-scoped view of the shared parent bus.

    A camera timeout must not consume an error belonging to another branch.
    The coordinator dispatches ERROR/EOS messages into these branch queues.
    """

    def __init__(self, coordinator: "MultiCameraGstPipeline", camera_id: str):
        self._coordinator = coordinator
        self._camera_id = camera_id

    def pop_filtered(self, message_types):
        return self._coordinator.pop_message(self._camera_id, message_types)


@dataclass
class CameraBranch:
    """Pipeline-compatible handle for one camera bin."""

    coordinator: "MultiCameraGstPipeline"
    camera_id: str
    bin: object
    synced_with_parent: bool = False

    def get_by_name(self, name: str):
        return self.bin.get_by_name(name)

    def get_bus(self):
        return _BranchBus(self.coordinator, self.camera_id)

    def restart(self) -> bool:
        return self.coordinator.restart_branch(self)

    def set_state(self, state):
        Gst = self.coordinator.Gst
        if state == Gst.State.PLAYING:
            return self.coordinator.play_branch(self)
        if state == Gst.State.NULL:
            self.coordinator.remove_branch(self)
            return Gst.StateChangeReturn.SUCCESS
        return self.bin.set_state(state)


class MultiCameraGstPipeline:
    """Own one parent GstPipeline and N independently replaceable camera bins."""

    def __init__(
        self,
        camera_ids: list[str],
        *,
        startup_timeout_seconds: float = 90.0,
        startup_camera_count: int | None = None,
    ) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init([])
        self.Gst = Gst
        self.camera_ids = tuple(str(value) for value in camera_ids)
        self.expected = len(self.camera_ids)
        self.startup_camera_count = min(
            self.expected,
            max(1, int(startup_camera_count or self.expected)),
        )
        self.startup_timeout_seconds = max(5.0, float(startup_timeout_seconds))
        self.pipeline = Gst.Pipeline.new("sentinel-multi-camera")
        if self.pipeline is None:
            raise RuntimeError("could not create shared multi-camera GstPipeline")

        self._condition = threading.Condition(threading.RLock())
        self._branches: dict[str, CameraBranch] = {}
        self._generations = {camera_id: 0 for camera_id in self.camera_ids}
        self._messages = {camera_id: queue.Queue() for camera_id in self.camera_ids}
        self._started = False
        self._starting = False
        self._startup_owner: str | None = None
        self._closed = False
        self._recoveries: dict[str, dict] = {}
        self._traces: dict[str, object] = {}
        self._rebuild_request: dict | None = None
        self._created_at = time.monotonic()
        self._bus_thread = threading.Thread(
            target=self._dispatch_bus,
            name="sentinel-gst-bus",
            daemon=True,
        )
        self._bus_thread.start()

    def create_branch(self, camera_id: str, description: str, *, configure=None) -> CameraBranch:
        Gst = self.Gst
        camera_id = str(camera_id)
        with self._condition:
            # Start exactly one branch first.  Once that branch and its shared
            # GVA inference instances are PLAYING, add/synchronise every peer
            # serially.  Starting all shared GVA elements in one parent state
            # transition can deadlock Intel's InferenceImpl output mutex while
            # multiple appsinks are still prerolling.
            while not self._closed:
                if self._starting:
                    self._condition.wait(timeout=0.25)
                    continue
                if not self._started:
                    if self._startup_owner is None:
                        self._startup_owner = camera_id
                    elif self._startup_owner != camera_id:
                        self._condition.wait(timeout=0.25)
                        continue
                break
            if self._closed:
                raise RuntimeError("multi-camera GstPipeline is closed")
            while self._starting and not self._closed:
                self._condition.wait(timeout=0.25)
            if self._closed:
                raise RuntimeError("multi-camera GstPipeline is closed")
            # GStreamer parsing creates DL Streamer elements and can touch the
            # shared Intel model registry. Keep construction and parent
            # attachment under one process-local lock; camera decode remains
            # parallel after the graph is PLAYING.
            try:
                branch_bin = Gst.parse_bin_from_description(description, False)
                if branch_bin is None:
                    raise RuntimeError(
                        f"could not parse GStreamer branch for {camera_id}"
                    )
                # A running parent can start a newly attached source at once.
                # Install TLS trust/callbacks before attachment or state sync.
                if configure is not None:
                    configure(branch_bin)
            except Exception:
                if not self._started and self._startup_owner == camera_id:
                    self._startup_owner = None
                self._condition.notify_all()
                raise
            previous = self._branches.get(camera_id)
            if previous is not None:
                self._remove_locked(previous)
            # A reconnect can occur while native objects from the old graph
            # are still being released.  Use a generation-qualified bin name
            # while still requiring the old bin to be fully detached below.
            generation = self._generations.get(camera_id, 0) + 1
            self._generations[camera_id] = generation
            branch_bin.set_name(f"camera-bin-{camera_id}-{generation}")
            branch = CameraBranch(self, camera_id, branch_bin)
            if branch_bin.get_parent() is not None:
                raise RuntimeError(
                    f"new camera branch {camera_id} already has a parent"
                )
            # Intel's packaged GI bindings return ``None`` on success while
            # Ubuntu's override returns ``True``.  The parent relationship is
            # the portable success check.
            self.pipeline.add(branch_bin)
            if branch_bin.get_parent() != self.pipeline:
                raise RuntimeError(f"could not add camera branch {camera_id}")
            self._branches[camera_id] = branch
            logger.info(
                "registered camera branch %s (%d/%d)",
                camera_id, len(self._branches), self.startup_camera_count,
            )
            self._condition.notify_all()
            already_started = self._started
            if already_started:
                self._starting = True

        if already_started:
            # A running parent makes this a bounded one-branch transition,
            # avoiding the shared-inference/preroll cycle seen when all camera
            # bins change state together.
            synced = bool(branch.bin.sync_state_with_parent())
            with self._condition:
                self._starting = False
                branch.synced_with_parent = synced
                if not synced:
                    self._remove_locked(branch)
                self._condition.notify_all()
            if not synced:
                raise RuntimeError(
                    f"could not synchronise camera branch {camera_id}"
                )
        return branch

    def play_branch(self, branch: CameraBranch):
        Gst = self.Gst
        deadline = self._created_at + self.startup_timeout_seconds
        while True:
            with self._condition:
                if self._closed:
                    return Gst.StateChangeReturn.FAILURE
                if branch.synced_with_parent:
                    return Gst.StateChangeReturn.SUCCESS
                if self._started:
                    # create_branch synchronises branches added after startup
                    # before returning them to the camera worker.
                    return Gst.StateChangeReturn.FAILURE
                if self._starting:
                    self._condition.wait(timeout=0.25)
                    continue

                # The first registered camera is intentionally the startup
                # leader.  Peers are held out of the parent until this one
                # branch is fully PLAYING, then attached one at a time.
                all_registered = self._startup_owner == branch.camera_id
                timed_out = time.monotonic() >= deadline
                if not (all_registered or timed_out):
                    self._condition.wait(
                        timeout=min(
                            0.5,
                            max(0.0, deadline - time.monotonic()),
                        )
                    )
                    continue

                # Only one camera thread performs the parent transition.
                # Crucially, do not hold _condition while calling into
                # GStreamer: state-change callbacks and the bus dispatcher
                # need to inspect the camera registry and would otherwise
                # deadlock against this lock.
                self._starting = True
                branch_count = len(self._branches)

            result = self.pipeline.set_state(Gst.State.PLAYING)

            with self._condition:
                self._starting = False
                if result != Gst.StateChangeReturn.FAILURE:
                    self._started = True
                    branch.synced_with_parent = True
                    self._startup_owner = None
                    logger.info(
                        "shared GStreamer parent entered PLAYING with %d branches",
                        branch_count,
                    )
                else:
                    self._startup_owner = None
                    logger.error(
                        "shared GStreamer parent failed to enter PLAYING with %d branches",
                        branch_count,
                    )
                self._condition.notify_all()
                return result

    def remove_branch(self, branch: CameraBranch) -> None:
        with self._condition:
            self._remove_locked(branch)
            self._condition.notify_all()

    def restart_branch(self, branch: CameraBranch) -> bool:
        """Request a fresh RTSP session without stopping native media elements.

        Return means only that the source transition was accepted. The camera
        consumer must call recovery_completed after receiving a fresh frame.
        Native calls can block; an independent parent watchdog bounds this phase.
        """
        Gst = self.Gst
        with self._condition:
            if self._closed or self._branches.get(branch.camera_id) is not branch:
                return False
            previous = self._recoveries.get(branch.camera_id) or {}
            attempt = int(previous.get("attempt", 0)) + 1
            now = time.monotonic()
            self._recoveries[branch.camera_id] = {
                "attempt": attempt,
                "started": (previous["started"] if previous and not previous.get("verified") else now),
                "call_started": now,
                "phase": "source_stopping", "verified": False,
            }
        source = branch.get_by_name("camerasource")
        if source is None:
            self.request_rebuild(branch.camera_id, "native_branch_has_no_restartable_source")
            return False
        log_event(logger, logging.WARNING, "camera_source_restart_begin",
                  "restarting RTSP session while retaining native media graph",
                  camera_id=branch.camera_id, attempt=attempt,
                  recovery_phase="source_stopping")
        try:
            result = source.set_state(Gst.State.NULL)
            if result == Gst.StateChangeReturn.FAILURE:
                self.request_rebuild(branch.camera_id, "rtsp_source_stop_rejected")
                return False
            # Clear stop/PAUSE errors only after logging them on the bus. They
            # must not masquerade as failures of the newly opened session.
            self._clear_messages(branch.camera_id)
            with self._condition:
                self._recoveries[branch.camera_id]["phase"] = "source_starting"
            accepted = bool(source.sync_state_with_parent())
            with self._condition:
                self._recoveries[branch.camera_id]["phase"] = (
                    "awaiting_fresh_frame" if accepted else "source_transition_rejected"
                )
            log_event(logger, logging.INFO if accepted else logging.WARNING,
                      "camera_source_restart_requested",
                      "RTSP source state transition requested; fresh frame not yet verified",
                      camera_id=branch.camera_id, attempt=attempt,
                      transition_accepted=accepted, recovery_verified=False)
            return accepted
        except BaseException:
            # Do not use the fleet-wide _starting latch for network recovery.
            # One slow/disconnected source must not serialize peer restarts.
            with self._condition:
                self._recoveries[branch.camera_id]["phase"] = "source_restart_exception"
            raise

    def attach_trace(self, camera_id, trace) -> None:
        with self._condition:
            self._traces[camera_id] = trace

    def recovery_completed(self, camera_id, pts, received_at) -> None:
        with self._condition:
            state = self._recoveries.get(camera_id)
            if not state or state.get("verified"):
                return
            state.update(phase="recovered", verified=True)
            attempt = state["attempt"]
            elapsed = time.monotonic() - state["started"]
        log_event(logger, logging.INFO, "camera_recovery_verified",
                  "fresh admitted frame received after RTSP recovery",
                  camera_id=camera_id, attempt=attempt, recovery_verified=True,
                  recovery_seconds=round(elapsed, 3), frame_pts_ns=int(pts),
                  frame_received_at=received_at)

    def request_rebuild(self, camera_id, reason, **evidence) -> None:
        with self._condition:
            if self._rebuild_request is None:
                self._rebuild_request = {"camera_id": camera_id, "reason": reason,
                                         "stream_session_id": getattr(self._traces.get(camera_id), "session_id", None),
                                         **evidence}
        log_event(logger, logging.ERROR, "native_graph_rebuild_requested",
                  "native graph requires supervised process replacement",
                  camera_id=camera_id, retry_reason=reason, **evidence)

    def watchdog_snapshot(self, recovery_timeout=45.0, processing_timeout=120.0):
        # Only Python state is queried. Never call a native get_state here:
        # even its nominal timeout cannot bound a blocked native state mutex.
        now = time.monotonic()
        with self._condition:
            request = self._rebuild_request
            pending = {key: dict(value) for key, value in self._recoveries.items()
                       if not value.get("verified")}
            traces = dict(self._traces)
        for camera_id, recovery in pending.items():
            if (recovery["phase"] in {"source_stopping", "source_starting"}
                    and now - recovery.get("call_started", recovery["started"]) > recovery_timeout):
                request = {"camera_id": camera_id, "reason": "native_source_restart_deadline",
                           "recovery_phase": recovery["phase"],
                           "attempt": recovery["attempt"]}
            if (recovery["phase"] == "awaiting_fresh_frame"
                    and now - recovery["started"] > recovery_timeout):
                trace = traces.get(camera_id)
                progress = trace.snapshot() if trace is not None else {}
                stages = progress.get("stages", {})
                input_age = stages.get("source.video", {}).get("age_seconds")
                live_input = input_age is not None and input_age < 10
                if live_input:
                    request = {"camera_id": camera_id,
                               "reason": "live_input_without_recovered_native_output",
                               "native_progress": progress, "attempt": recovery["attempt"]}
        for camera_id, trace in traces.items():
            snap = trace.snapshot()
            if (snap["camera_phase"] in {"frame_processing", "plate_detection", "event_policy"}
                    and snap["phase_age_seconds"] > processing_timeout):
                request = {"camera_id": camera_id, "reason": "camera_processing_deadline",
                           "native_progress": snap}
        return {"recovering": bool(pending), "rebuild_request": request}

    def _clear_messages(self, camera_id: str) -> None:
        messages = self._messages.get(str(camera_id))
        if messages is None:
            return
        while True:
            try:
                messages.get_nowait()
            except queue.Empty:
                return

    def _remove_locked(self, branch: CameraBranch) -> None:
        current = self._branches.get(branch.camera_id)
        if current is not branch:
            return
        branch.bin.set_state(self.Gst.State.NULL)
        # Finish resource teardown before a replacement opens the same source
        # and shared model instances.  This path runs only on reconnect/close.
        branch.bin.get_state(int(2 * self.Gst.SECOND))
        # As with ``add``, the Intel GI binding may return ``None`` even when
        # removal succeeds; verify the resulting ownership instead.
        self.pipeline.remove(branch.bin)
        if branch.bin.get_parent() is not None:
            raise RuntimeError(
                f"could not detach camera branch {branch.camera_id}"
            )
        self._branches.pop(branch.camera_id, None)
        self._clear_messages(branch.camera_id)

    def _camera_for_source(self, source) -> str | None:
        with self._condition:
            for camera_id, branch in self._branches.items():
                try:
                    if source == branch.bin or source.has_as_ancestor(branch.bin):
                        return camera_id
                except (AttributeError, TypeError):
                    continue
        return None

    def _dispatch_bus(self) -> None:
        Gst = self.Gst
        bus = self.pipeline.get_bus()
        mask = Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS
        while True:
            with self._condition:
                if self._closed:
                    return
            message = bus.timed_pop_filtered(int(0.25 * Gst.SECOND), mask)
            if message is None:
                continue
            camera_id = self._camera_for_source(message.src)
            if message.type in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
                error, debug = (message.parse_error() if message.type == Gst.MessageType.ERROR
                                else message.parse_warning())
                trace = self._traces.get(camera_id)
                log_event(logger, logging.ERROR if message.type == Gst.MessageType.ERROR else logging.WARNING,
                          "native_pipeline_error" if message.type == Gst.MessageType.ERROR else "native_pipeline_warning",
                          "GStreamer native diagnostic", camera_id=camera_id,
                          stream_session_id=getattr(trace, "session_id", None),
                          native_element=message.src.get_name() if message.src else None,
                          native_error=sanitize_text(error, limit=2048),
                          native_debug=sanitize_text(debug or "", limit=4096),
                          recovery_phase=(self._recoveries.get(camera_id) or {}).get("phase"),
                          native_progress=trace.snapshot() if trace is not None else None)
                if message.type == Gst.MessageType.WARNING:
                    continue
            if camera_id is not None:
                self._messages.setdefault(camera_id, queue.Queue()).put(message)
            elif message.type == Gst.MessageType.ERROR:
                # A parent/device-level error is relevant to every branch.
                for target in tuple(self._messages.values()):
                    target.put(message)

    def pop_message(self, camera_id: str, message_types):
        messages = self._messages.setdefault(str(camera_id), queue.Queue())
        retained = []
        selected = None
        while True:
            try:
                message = messages.get_nowait()
            except queue.Empty:
                break
            if selected is None and bool(message.type & message_types):
                selected = message
            else:
                retained.append(message)
        for message in retained:
            messages.put(message)
        return selected

    def snapshot(self) -> dict:
        with self._condition:
            return {
                "architecture": "single-gst-pipeline-multi-camera-branches",
                "expected_cameras": self.expected,
                "startup_camera_count": self.startup_camera_count,
                "registered_cameras": len(self._branches),
                "camera_ids": sorted(self._branches),
                "playing": self._started,
            }

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            branches = list(self._branches.values())
            for branch in branches:
                self._remove_locked(branch)
            self.pipeline.set_state(self.Gst.State.NULL)
            self._condition.notify_all()
        self._bus_thread.join(timeout=2.0)

