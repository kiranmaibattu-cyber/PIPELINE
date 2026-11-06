from pathlib import Path
import multiprocessing as mp
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services' / 'worker'))
from pipeline.dlstreamer_bridge import DlsMotionPolicyGate, AdmittedFrame
from pipeline.multi_camera_gst import MultiCameraGstPipeline, CameraBranch
from pipeline.native_watchdog import create_watchdog_channel, watchdog_failure
from pipeline.rtsp_source import RtspVideoLink


def gate():
    return DlsMotionPolicyGate(None, SimpleNamespace(set_gauge=lambda *a: None),
                              object(), 8, 10**9, maximum_pending=1)


def record(n):
    return AdmittedFrame(n, n*100, float(n), float(n), 'TRACKING', 'active')


def test_suspend_unblocks_producer_without_erasing_unfinished_work():
    g=gate(); assert g._reserve(record(1))
    entered=threading.Event(); result=[]
    def reserve():
        entered.set(); result.append(g._reserve(record(2)))
    t=threading.Thread(target=reserve);t.start()
    try:
        assert entered.wait(1)
        g.suspend();t.join(1)
        assert not t.is_alive() and result==[False]
        assert g.pending_admissions==1
        with pytest.raises(RuntimeError,match='unfinished'):
            g.resume(object())
        assert g.take(100)==record(1)
        g.resume(object())
        assert g._reserve(record(3))
        assert g.take(300)==record(3)
    finally:g.close();t.join(1)


def test_closed_gate_cannot_be_accidentally_reopened():
    g=gate();g.close()
    with pytest.raises(RuntimeError,match='closed'):g.resume(object())


def coordinator_with_source(source):
    c=MultiCameraGstPipeline.__new__(MultiCameraGstPipeline)
    c.Gst=SimpleNamespace(State=SimpleNamespace(NULL='NULL'),StateChangeReturn=SimpleNamespace(FAILURE='FAILURE'))
    c._condition=threading.Condition(threading.RLock());c._closed=False
    c._recoveries={};c._traces={};c._rebuild_request=None;c._messages={'cam':queue.Queue()}
    class Bin:
        def get_by_name(self,name):return source if name=='camerasource' else None
        def set_state(self,*a):raise AssertionError('must never cycle VA/inference bin state')
    branch=CameraBranch(c,'cam',Bin(),True);c._branches={'cam':branch}
    return c,branch


def test_reconnect_only_restarts_source_and_requires_fresh_frame_confirmation():
    calls=[]
    source=SimpleNamespace(set_state=lambda state:calls.append(state),sync_state_with_parent=lambda:True)
    c,b=coordinator_with_source(source)
    assert b.restart() is True
    assert calls==['NULL']
    assert b.synced_with_parent
    assert c._recoveries['cam']['verified'] is False
    c.recovery_completed('cam',123,1000.0)
    assert c._recoveries['cam']['verified'] is True
    assert c.watchdog_snapshot()['recovering'] is False


def test_source_failure_is_not_reported_as_verified_recovery():
    source=SimpleNamespace(set_state=lambda state:'FAILURE',sync_state_with_parent=lambda:True)
    c,b=coordinator_with_source(source)
    assert b.restart() is False
    assert not c._recoveries['cam']['verified']


def test_recovery_native_call_deadline_requests_rebuild():
    source=SimpleNamespace(set_state=lambda state:None,sync_state_with_parent=lambda:True)
    c,b=coordinator_with_source(source)
    c._recoveries['cam']={'phase':'source_stopping','started':time.monotonic()-50,'attempt':2,'verified':False}
    assert c.watchdog_snapshot()['rebuild_request']['reason']=='native_source_restart_deadline'


def test_ordinary_source_outage_does_not_rebuild_healthy_peers():
    c,b=coordinator_with_source(None)
    c._recoveries['cam']={'phase':'awaiting_fresh_frame','started':time.monotonic()-500,'attempt':20,'verified':False}
    c._traces['cam']=SimpleNamespace(snapshot=lambda:{'camera_phase':'await_primary_output','phase_age_seconds':500,'stages':{'source.video':{'count':0,'age_seconds':None}}})
    assert c.watchdog_snapshot()['rebuild_request'] is None
    assert c.watchdog_snapshot()['recovering'] is True


def test_live_input_without_output_has_a_bounded_recovery():
    c,b=coordinator_with_source(None)
    c._recoveries['cam']={'phase':'awaiting_fresh_frame','started':time.monotonic()-60,'attempt':2,'verified':False}
    c._traces['cam']=SimpleNamespace(snapshot=lambda:{'camera_phase':'await_primary_output','phase_age_seconds':60,'stages':{'source.video':{'count':50,'age_seconds':.1}}})
    assert c.watchdog_snapshot()['rebuild_request']['reason']=='live_input_without_recovered_native_output'


def test_parent_watchdog_is_independent_of_camera_thread_and_generation():
    ctx=mp.get_context('spawn');old=create_watchdog_channel(ctx)
    old['heartbeat'].value=time.monotonic()-70
    assert watchdog_failure(old)=='native_worker_watchdog_unresponsive'
    fresh=create_watchdog_channel(ctx)
    assert watchdog_failure(fresh) is None
    fresh['restart_requested'].value=1
    assert watchdog_failure(fresh)=='native_graph_rebuild_requested'


class Pad:
    def __init__(self,media='video',encoding='H264'):
        self.media=media;self.encoding=encoding;self.peer=None;self.callbacks=[]
    def get_current_caps(self):
        info=SimpleNamespace(get_name=lambda:'application/x-rtp',get_string=lambda k:{'media':self.media,'encoding-name':self.encoding}.get(k))
        return SimpleNamespace(is_empty=lambda:False,get_size=lambda:1,get_structure=lambda n:info)
    def get_peer(self):return self.peer
    def get_name(self):return 'video-pad'
    def link(self,sink):self.peer=sink;sink.peer=self;return 0
    def add_probe(self,kind,callback):self.callbacks.append(callback)


def install_link(monkeypatch):
    gst=SimpleNamespace(PadLinkReturn=SimpleNamespace(OK=0),PadProbeType=SimpleNamespace(BUFFER=1),PadProbeReturn=SimpleNamespace(OK=0))
    monkeypatch.setitem(sys.modules,'gi',SimpleNamespace(repository=SimpleNamespace(Gst=gst)))
    monkeypatch.setitem(sys.modules,'gi.repository',SimpleNamespace(Gst=gst))
    handlers={};source=SimpleNamespace(connect=lambda name,cb:handlers.setdefault(name,cb))
    sink=Pad();depay=SimpleNamespace(get_static_pad=lambda name:sink)
    link=RtspVideoLink(source,depay,'cam','session','h264')
    return link,handlers,sink


def test_persistent_video_link_connects_recreated_pad_and_ignores_audio(monkeypatch):
    link,handlers,sink=install_link(monkeypatch)
    audio=Pad(media='audio');handlers['pad-added'](None,audio)
    assert sink.peer is None
    first=Pad();handlers['pad-added'](None,first)
    assert sink.peer is first and link.generation==1
    sink.peer=None
    second=Pad();handlers['pad-added'](None,second)
    assert sink.peer is second and link.generation==2
    assert len(second.callbacks)==1


def test_second_video_or_wrong_codec_cannot_steal_pad(monkeypatch):
    link,handlers,sink=install_link(monkeypatch)
    handlers['pad-added'](None,Pad(encoding='H265'))
    assert sink.peer is None
    first=Pad();handlers['pad-added'](None,first)
    handlers['pad-added'](None,Pad())
    assert sink.peer is first


def _gil_blocked_native_worker(channel, entered):
    import ctypes
    import threading
    import time
    def heartbeat():
        while True:
            channel['heartbeat'].value=time.monotonic()
            time.sleep(.01)
    threading.Thread(target=heartbeat,daemon=True).start()
    time.sleep(.05)
    entered.set()
    # PyDLL intentionally retains the GIL during this harmless libc call.
    # This reproduces the boundary an in-worker Python watchdog cannot rescue.
    ctypes.PyDLL(None).sleep(20)


def test_parent_can_fence_worker_even_when_native_call_holds_gil():
    ctx=mp.get_context('spawn');channel=create_watchdog_channel(ctx);entered=ctx.Event()
    p=ctx.Process(target=_gil_blocked_native_worker,args=(channel,entered));p.start()
    try:
        assert entered.wait(5)
        deadline=time.monotonic()+2
        reason=None
        while time.monotonic()<deadline:
            reason=watchdog_failure(channel,timeout_seconds=.15)
            if reason:break
            time.sleep(.02)
        assert reason=='native_worker_watchdog_unresponsive'
        assert p.is_alive()
        p.kill();p.join(2)
        assert not p.is_alive()
    finally:
        if p.is_alive():p.kill();p.join(2)
