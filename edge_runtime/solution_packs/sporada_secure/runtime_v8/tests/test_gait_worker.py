from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import sys, threading
import numpy as np
import cv2
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/worker'))
from pipeline.person_reid import OpenVINOGaitExtractor
from pipeline.v8_events import V8EventPipeline


def setup(worker):
    p = V8EventPipeline.__new__(V8EventPipeline)
    p.camera_id, p.session_id = 'cam', 'session'
    p.config = {'deployment_id': 'test', 'config_revision': 1}
    p.bindings = [{'app': 'person_identity', 'use_case_id': 'identity', 'scope': 'full_frame',
                   'geometry_ids': [], 'object_types': ['person'],
                   'embedding_profile_ids': ['gaitbase-v18.1']}]
    p.policy = SimpleNamespace(section=lambda name: {'gait_min_silhouettes': 20})
    p.telemetry = None
    p.feature_cooldown = 5
    p._gait_worker = ThreadPoolExecutor(max_workers=1) if worker else None
    p._pending_gait = None
    g = OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
    g.backgrounds, g.centers, g.cache = {}, {}, {}
    g.buffers, g.buffer_times = defaultdict(list), defaultdict(list)
    g.minimum_motion_pixels = 2
    g.min_sequence, g.max_sequence, g.sequence_length = 20, 60, 30
    g.dimension, g.gait_output = 4096, 0
    g.gait = lambda inputs: [np.arange(4096, dtype=np.float32) + inputs[0].sum()]
    p.gait = g
    packages = []
    p._persist = lambda event, evidence, embeddings: packages.append((event, evidence, embeddings))
    return p, packages


def run(worker):
    p, packages = setup(worker)
    states = [{'id': 'p'+str(i), 'track_id': i+1, 'last_feature': {}} for i in range(2)]
    try:
        for index in range(100):
            frame = np.zeros((180, 420, 3), np.uint8)
            for i, state in enumerate(states):
                x = 10 + (index * 4) % 240
                bbox = [x, 10 + i*70, x+55, 75 + i*70]
                cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (180,180,180), -1)
            packet = SimpleNamespace(frame=frame, index=index*3)
            for i, state in enumerate(states):
                x = 10 + (index * 4) % 240
                item = SimpleNamespace(bbox=[x,10+i*70,x+55,75+i*70],metadata={'track_id':i+1})
                p._features(packet,item,state,[],None,None,index/3,'frame:'+str(index))
            p._finish_gait()
        return p, packages, states
    finally:
        if p._gait_worker: p._gait_worker.shutdown()


def test_worker_preserves_background_history_vectors_timestamps_and_evidence():
    old, a, states_a = run(False)
    new, b, states_b = run(True)
    assert a and len(a) == len(b)
    assert states_a == states_b
    np.testing.assert_array_equal(old.gait.backgrounds['cam'].getBackgroundImage(),new.gait.backgrounds['cam'].getBackgroundImage())
    assert old.gait.buffer_times == new.gait.buffer_times
    for key in old.gait.buffers:
        np.testing.assert_array_equal(old.gait.buffers[key],new.gait.buffers[key])
        np.testing.assert_array_equal(old.gait.cache[key],new.gait.cache[key])
    for (ea,va,xa),(eb,vb,xb) in zip(a,b):
        for key in ['observed_at','frame_id','presence_id','track_id','kind','quality','sample_reason']:
            assert ea.get(key)==eb.get(key)
        assert va[0][1] == vb[0][1]  # exact encoded source crop
        # Embedding metadata differs only in generated IDs; vector bytes match.
        for key in xa[0]:
            if key not in {'embedding_id','evidence_id','event_id','observation_id'}:
                assert xa[0][key] == xb[0][key]


def test_row_parallel_background_preserves_emitted_gait_packages(monkeypatch):
    from pipeline import person_reid, parallel_background
    cv2.setNumThreads(1)
    monkeypatch.setattr(parallel_background, '_cpu_capacity', lambda: 4)
    monkeypatch.setattr(person_reid, 'create_gait_background', lambda:
        cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False))
    old, first, states_a = run(False)
    monkeypatch.setattr(person_reid, 'create_gait_background', lambda:
        parallel_background.RowPartitionedMog2(workers=4, minimum_pixels=0))
    new, second, states_b = run(False)
    try:
        assert first and len(first) == len(second)
        assert states_a == states_b
        assert new.gait.backgrounds['cam'].last_partitions == 4
        assert old.gait.buffer_times == new.gait.buffer_times
        for (ea, va, xa), (eb, vb, xb) in zip(first, second):
            for key in ['observed_at', 'source_frame_id', 'presence_id', 'track_id',
                        'kind', 'quality', 'sample_reason', 'profile_id', 'use_case_id']:
                assert ea.get(key) == eb.get(key)
            assert va[0][1] == vb[0][1]  # Exact JPEG bytes of original-frame crop.
            for key in va[0][0]:
                if key not in {'evidence_id', 'observation_id'}:
                    assert va[0][0][key] == vb[0][0][key]
            for key in xa[0]:
                if key not in {'embedding_id', 'evidence_id', 'event_id', 'observation_id'}:
                    assert xa[0][key] == xb[0][key]
    finally:
        old.gait.close()
        new.gait.close()


def test_worker_overlaps_and_rejects_retired_session():
    p,_ = setup(True)
    started,release = threading.Event(),threading.Event()
    emitted=[]
    def collect(*args):
        started.set(); assert release.wait(3); return ['candidate']
    p._collect_gait=collect
    p._emit_feature_candidates=lambda *args: emitted.extend(args[3])
    state={'id':'p','track_id':1,'last_feature':{}}
    packet=SimpleNamespace(frame=np.zeros((20,20,3),np.uint8),index=3)
    item=SimpleNamespace(bbox=[0,0,10,20],metadata={'track_id':1})
    try:
        p._features(packet,item,state,[],None,None,1,'f')
        assert started.wait(1)
        assert p._pending_gait is not None  # caller returned while computation is blocked
        p.session_id='new-session'
        release.set();p._finish_gait()
        assert emitted==[]
    finally:
        release.set();p._gait_worker.shutdown()


def test_failed_frame_waits_for_worker_and_discards_its_emission():
    p,_=setup(True)
    state={'id':'p','track_id':1,'last_feature':{}}
    packet=SimpleNamespace(frame=np.zeros((20,20,3),np.uint8),index=3)
    item=SimpleNamespace(bbox=[0,0,10,20],metadata={'track_id':1})
    p._collect_gait=lambda *args: ['candidate']
    emitted=[]
    p._emit_feature_candidates=lambda *args: emitted.extend(args[3])
    def broken(packet,faces):
        p._features(packet,item,state,[],None,None,1,'f')
        raise ValueError('frame failure')
    p._process_frame=broken
    try:
        with pytest.raises(ValueError,match='frame failure'):p.process(packet)
        assert p._pending_gait is None and emitted==[]
    finally:p._gait_worker.shutdown()


def test_next_job_waits_for_prior_emission_and_uses_updated_gate_snapshot():
    p,_=setup(True)
    state={'id':'p','track_id':1,'last_feature':{}}
    packet=SimpleNamespace(frame=np.zeros((20,20,3),np.uint8),index=3)
    item=SimpleNamespace(bbox=[0,0,10,20],metadata={'track_id':1})
    snapshots=[];threads=[];main_thread=threading.get_ident()
    def collect(packet,item,snapshot,policy,now):
        snapshots.append(dict(snapshot['last_feature']))
        return ['candidate'] if p._feature_due(snapshot,'gait',1,now) else []
    def emit(packet,actual,binding,candidates,now,frame_id):
        if candidates:
            threads.append(threading.get_ident())
            actual['last_feature']['gait']=(1,now)
    p._collect_gait=collect;p._emit_feature_candidates=emit
    try:
        p._features(packet,item,state,[],None,None,1,'f')
        p._features(packet,item,state,[],None,None,1,'f')
        p._finish_gait()
        assert snapshots==[{}, {'gait':(1,1)}]
        assert threads==[main_thread]
    finally:p._gait_worker.shutdown()
