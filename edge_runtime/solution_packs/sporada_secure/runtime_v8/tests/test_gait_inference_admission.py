from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
import sys
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/worker'))
from pipeline.person_reid import OpenVINOGaitExtractor
from pipeline.v8_events import V8EventPipeline


def run(optimized, observed=None):
    gait = OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
    gait.buffers = defaultdict(list)
    gait.buffer_times = defaultdict(list)
    gait.centers, gait.cache = {}, {}
    gait.minimum_motion_pixels = 2
    gait.min_sequence, gait.max_sequence, gait.sequence_length = 20, 60, 30
    gait.dimension, gait.gait_output = 4096, 0
    gait.actual_device = "NPU/DLStreamer"
    calls = []
    def model(inputs):
        sequence = inputs[0].copy()
        calls.append(sequence)
        return [np.arange(4096, dtype=np.float32).reshape(1,256,16) + sequence.sum()]
    gait.gait = model
    policy = V8EventPipeline.__new__(V8EventPipeline)
    policy.feature_cooldown = 5.0
    state = {'last_feature': {}}
    outputs = []
    for i in range(80):
        now = i / 3
        mask = np.full((64,44), i, np.uint8)
        gait._silhouettes = lambda frame, people, camera_id: [mask]
        person = SimpleNamespace(metadata={'track_id': 7},bbox=[i*3,0,i*3+30,80])
        accept = (lambda track, count: policy._feature_due(state,'gait',min(1.,count/30),now)) if optimized else None
        result = gait.collect(np.zeros((1,1,3),np.uint8),[person],'cam',{7:'presence'},captured_at=now,accept_sequence=accept,observe=observed)
        if 7 in result:
            quality = min(1., len(gait.buffers['presence'])/30)
            if policy._feature_due(state,'gait',quality,now):
                outputs.append((now, result[7].copy(), list(gait.buffer_times['presence'])))
                state['last_feature']['gait']=(quality, now)
    return outputs, calls, gait


def test_admission_preserves_emitted_vectors_and_all_sequence_history():
    old, old_calls, old_gait = run(False)
    new, new_calls, new_gait = run(True)
    assert len(old) == len(new) == 2
    for a,b in zip(old,new):
        assert a[0] == b[0] and a[2] == b[2]
        np.testing.assert_array_equal(a[1],b[1])
    assert len(new_calls) == 2 < len(old_calls)
    assert old_gait.buffer_times == new_gait.buffer_times
    np.testing.assert_array_equal(old_gait.buffers['presence'],new_gait.buffers['presence'])
    assert old_gait.centers.keys() == new_gait.centers.keys()
    for key in old_gait.centers:np.testing.assert_array_equal(old_gait.centers[key],new_gait.centers[key])


def test_identity_event_reason_survives_early_gate_refactor():
    pipeline = V8EventPipeline.__new__(V8EventPipeline)
    pipeline.camera_id, pipeline.session_id = 'cam', 'session'
    pipeline.config = {'deployment_id':'test','config_revision':1}
    pipeline.bindings = [{'app':'person_identity','use_case_id':'identity',
                         'scope':'full_frame','geometry_ids':[],
                         'object_types':['person'],
                         'embedding_profile_ids':['transreid-ssl-v18.1']}]
    pipeline.feature_cooldown = 5
    pipeline.policy = SimpleNamespace(section=lambda name: {})
    pipeline.gait = None
    pipeline.telemetry = None
    pipeline._body_quality = lambda crop: .8
    packages = []
    pipeline._persist = lambda event,evidence,embeddings: packages.append((event,evidence,embeddings))
    frame = np.ones((100,100,3),np.uint8)
    packet = SimpleNamespace(frame=frame,index=1)
    person = SimpleNamespace(bbox=[10,10,90,90])
    state = {'id':'p','track_id':7,'last_feature':{}}
    vector = np.ones(384,np.float32)/np.sqrt(384)
    pipeline._features(packet,person,state,[],None,vector,10,'f1')
    assert packages[-1][0]['sample_reason'] == 'first_qualified'
    assert packages[-1][2][0]['evidence_id'] == packages[-1][1][0][0]['evidence_id']
    pipeline._body_quality = lambda crop: .99
    pipeline._features(packet,person,state,[],None,vector,16,'f2')
    assert packages[-1][0]['sample_reason'] == 'quality_improved'
    assert len(packages) == 2


def test_gait_collection_is_not_misreported_as_accelerator_inference():
    observed=[]
    run(True,lambda stage,seconds,**kwargs: observed.append((stage,seconds,kwargs)))
    preprocessing=[x for x in observed if x[0].endswith('gait_silhouette_preprocessing')]
    models=[x for x in observed if x[0].endswith('gait_model_wall_time')]
    assert len(preprocessing)==80 and len(models)==2
    assert all(not x[2].get('device') for x in preprocessing)
    assert all(x[2]['device']=='NPU/DLStreamer' for x in models)
    assert all(x[1]>=0 for x in observed)
