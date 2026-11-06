"""Persistent RTSP video-pad wiring across source-only reconnections."""
from __future__ import annotations

import logging
import threading

from pipeline.structured_logging import log_event

logger = logging.getLogger(__name__)


class RtspVideoLink:
    """Keep dynamic RTP pads connected without restarting VA/model elements.

    GStreamer's ordinary parse-time delayed link is consumed after its first
    success. A reused rtspsrc needs a persistent handler for subsequent pads.
    This handler deliberately ignores audio and incompatible video codecs.
    """

    def __init__(self, source, depay, camera_id, session_id, codec, trace=None):
        self.source = source
        self.depay = depay
        self.camera_id = camera_id
        self.session_id = session_id
        self.codec = {"h264": "H264", "h265": "H265"}[codec]
        self.trace = trace or (lambda: None)
        self._lock = threading.Lock()
        self.generation = 0
        source.connect("pad-added", self._added)
        source.connect("pad-removed", self._removed)

    def _added(self, _source, pad):
        from gi.repository import Gst

        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps is None or caps.is_empty() or caps.get_size() < 1:
            return
        info = caps.get_structure(0)
        if (info.get_name() != "application/x-rtp"
                or info.get_string("media") != "video"
                or (info.get_string("encoding-name") or "").upper() != self.codec):
            return
        sink = self.depay.get_static_pad("sink")
        peer = sink.get_peer()
        if peer is not None and peer != pad:
            log_event(logger, logging.WARNING, "rtsp_video_pad_conflict",
                      "another video pad already owns the depayloader",
                      camera_id=self.camera_id, stream_session_id=self.session_id,
                      pad_name=pad.get_name())
            return
        result = Gst.PadLinkReturn.OK if peer == pad else pad.link(sink)
        with self._lock:
            self.generation += 1
            generation = self.generation
        linked = result == Gst.PadLinkReturn.OK
        log_event(logger, logging.INFO if linked else logging.ERROR,
                  "rtsp_video_pad_linked" if linked else "rtsp_video_pad_link_failed",
                  "RTSP video pad connected" if linked else "RTSP video pad connection failed",
                  camera_id=self.camera_id, stream_session_id=self.session_id,
                  source_generation=generation, pad_name=pad.get_name(),
                  codec=self.codec, link_result=str(result), linked=linked)
        if linked:
            def observe(_pad, probe_info):
                frame = probe_info.get_buffer()
                trace = self.trace()
                if trace is not None and frame is not None:
                    trace.mark("source.video", int(frame.pts))
                return Gst.PadProbeReturn.OK
            pad.add_probe(Gst.PadProbeType.BUFFER, observe)

    def _removed(self, _source, pad):
        log_event(logger, logging.INFO, "rtsp_source_pad_removed",
                  "RTSP source pad removed", camera_id=self.camera_id,
                  stream_session_id=self.session_id, source_generation=self.generation,
                  pad_name=pad.get_name())
