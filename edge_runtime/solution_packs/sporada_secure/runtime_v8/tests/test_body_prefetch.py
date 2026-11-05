from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import sys, threading
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/worker'))
from pipeline.dls_secondary import DlsBodyExtractor, _DlsTicket, _SharedDlsInferencerHandle, _InferenceJob


class Engine:
    def __init__(self):
        self.pool=ThreadPoolExecutor(max_workers=1)
        self.outstanding=0
        self.peak=0
        self.cancelled=0
        self.queue_wait_seconds=0.001
        self.device_inference_seconds=0.01
    def infer(self,image):
        return np.arange(384,dtype=np.float32)+float(image.mean())
    def submit(self,image):
        assert self.outstanding==0
        self.outstanding+=1;self.peak=max(self.peak,self.outstanding)
        future=self.pool.submit(self.infer,image)
        def result():
            try:return future.result(timeout=2)
            finally:self.outstanding-=1
        def cancel():
            self.cancelled+=1;future.cancel();self.outstanding=0
        return SimpleNamespace(result=result,cancel=cancel)
    def close(self):self.pool.shutdown(wait=True)


def extractor(engine):
    x=DlsBodyExtractor.__new__(DlsBodyExtractor);x.engine=engine
    return x


def test_fifo_vectors_and_single_outstanding_job(monkeypatch):
    engine=Engine();x=extractor(engine)
    crops=[np.full((160+i,70+i,3),i*30,np.uint8) for i in range(6)]
    try:
        monkeypatch.setenv('SENTINEL_DLS_BODY_PREFETCH','0');expected=x.embed(crops)
        monkeypatch.setenv('SENTINEL_DLS_BODY_PREFETCH','1');actual=x.embed(crops)
        np.testing.assert_array_equal(actual,expected)
        assert engine.peak==1 and engine.outstanding==0
        assert len(x.last_timings)==6
    finally:engine.close()


def test_second_crop_prepares_while_first_model_is_pending(monkeypatch):
    engine=Engine();x=extractor(engine);started=threading.Event();release=threading.Event()
    original_infer=engine.infer
    def infer(image):
        started.set();assert release.wait(2)
        return original_infer(image)
    engine.infer=infer
    prepare=x._prepare;calls=[]
    def preparing(crop):
        calls.append(1)
        if len(calls)==2:
            assert started.wait(2) and engine.outstanding==1
            release.set()
        return prepare(crop)
    x._prepare=preparing
    monkeypatch.setenv('SENTINEL_DLS_BODY_PREFETCH','1')
    try:assert x.embed([np.ones((80,50,3),np.uint8)]*2).shape==(2,384)
    finally:release.set();engine.close()


def test_preparation_failure_cancels_pending_job(monkeypatch):
    engine=Engine();x=extractor(engine);prepare=x._prepare;calls=[]
    def preparing(crop):
        calls.append(1)
        if len(calls)==2:raise ValueError('bad crop')
        return prepare(crop)
    x._prepare=preparing;monkeypatch.setenv('SENTINEL_DLS_BODY_PREFETCH','1')
    try:
        with pytest.raises(ValueError,match='bad crop'):x.embed([np.ones((80,50,3),np.uint8)]*2)
        assert engine.cancelled==1 and engine.outstanding==0
    finally:engine.close()


def test_old_camera_handle_rejects_async_result():
    engine=SimpleNamespace()
    handle=_SharedDlsInferencerHandle(('test',),engine,'camera-session')
    job=_InferenceJob('camera-session',np.zeros((1,1,3),np.uint8))
    def wait(job):
        handle._closed=True
        return np.ones(384)
    engine.wait=wait
    with pytest.raises(RuntimeError,match='closed before completion'):_DlsTicket(handle,job).result()
    assert job.cancelled.is_set()


def test_async_submission_keeps_queue_bound_and_cancels_timeout():
    import time
    from collections import deque
    from pipeline.dls_secondary import DlsRawImageInferencer
    engine=DlsRawImageInferencer.__new__(DlsRawImageInferencer)
    engine.width=engine.height=1
    engine._condition=threading.Condition()
    engine._closed=False;engine._owner_queues={};engine._owner_order=deque()
    engine._maximum_pending_per_owner=1;engine.timeout_seconds=0.001
    first=engine.submit(np.zeros((1,1,3),np.uint8),owner_id='cam')
    with pytest.raises(RuntimeError,match='queue is full'):
        engine.submit(np.zeros((1,1,3),np.uint8),owner_id='cam')
    first.submitted_at=time.perf_counter()-1
    with pytest.raises(TimeoutError):engine.wait(first)
    assert first.cancelled.is_set()


def test_async_completion_metrics_belong_to_ticket_not_latest_owner_job():
    engine=SimpleNamespace(wait=lambda job:np.ones(384))
    handle=_SharedDlsInferencerHandle(('test',),engine,'camera-session')
    job=_InferenceJob('camera-session',np.zeros((1,1,3),np.uint8))
    job.queue_wait_seconds=.02;job.device_inference_seconds=.03
    _DlsTicket(handle,job).result()
    assert handle.queue_wait_seconds==.02 and handle.device_inference_seconds==.03
