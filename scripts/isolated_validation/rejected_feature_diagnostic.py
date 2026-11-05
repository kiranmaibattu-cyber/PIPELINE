"""Observe feature selection state after explicit CV handoff admission rejection.

No detector/model/accuracy test. Calls the actual baked candidate emission
method with a qualified synthetic vector and original-frame-sized pixels.
"""
import copy, json, sys, uuid
from pathlib import Path
from types import SimpleNamespace
import numpy as np
sys.path.insert(0, '/opt/traffic-pilot/services/worker')
from pipeline.v8_events import V8EventPipeline

class Store:
    def __init__(self): self.allow = False; self.attempts = []; self.accepted = []
    def persist(self, event, evidence, embeddings):
        self.attempts.append(event['observation_id'])
        if self.allow: self.accepted.append(event['observation_id'])
        return self.allow
    def close(self): pass

results=[]
for kind, profile, dimension in [('face','adaface-ir101-v18.1',512),('body','transreid-ssl-v18.1',384),('gait','gaitbase-v18.1',4096)]:
    store=Store();binding={'use_case_id':'identity','app':'person_identity','scope':'full_frame','geometry_ids':[],
                          'object_types':['person'],'embedding_profile_ids':[profile]}
    config={'analytics':{'v8_events':{'v8':{'deployment_id':'test','config_revision':1,'bindings':[binding],
                                          'geometry':{'zones':[],'counting_lines':[]}}}}}
    pipeline=V8EventPipeline('cam',config,handoff_client=store)
    state={'id':str(uuid.uuid4()),'track_id':1,'last_feature':{}}
    pipeline.presences[state['id']]=state
    packet=SimpleNamespace(index=1,frame=np.full((200,100,3),128,np.uint8))
    vector=np.zeros(dimension,np.float32);vector[0]=1
    sequence={'sequence_started_at':'1970-01-01T00:01:10Z','sequence_ended_at':'1970-01-01T00:01:40Z','usable_silhouette_count':30} if kind=='gait' else None
    candidate=(kind,vector,.5,(10,10,90,190),profile,sequence)
    pipeline._emit_feature_candidates(packet,state,binding,[candidate],100,'cam:1')
    if kind=='face':pipeline._flush_face_candidates(102)
    after_reject=copy.deepcopy(state['last_feature']);attempts_before=len(store.attempts)
    store.allow=True
    pipeline._emit_feature_candidates(packet,state,binding,[candidate],106,'cam:2')
    if kind=='face':pipeline._flush_face_candidates(108)
    results.append({'kind':kind,'first_admission_accepted':False,'selection_state_after_rejection':after_reject,
                    'attempts_after_rejection':attempts_before,'attempts_after_restoration':len(store.attempts),
                    'accepted_after_restoration':len(store.accepted),
                    'same_qualified_sample_retried':len(store.attempts)>attempts_before})
print(json.dumps({'scope':'Actual baked producer admission handling, synthetic qualified candidates. Does not simulate post-admission ACK exhaustion.',
                  'results':results,'rejection_suppression_reproduced':all(not r['same_qualified_sample_retried'] and r['selection_state_after_rejection'] for r in results)},indent=2))
