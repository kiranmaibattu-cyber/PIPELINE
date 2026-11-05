"""Reuse historical behavioral scenarios against V8 with a capture API boundary.

Old SQL uploader tests are deliberately excluded. Observations/models are injected
for policy testing; these are not live model-accuracy results.
"""
import ast
from pathlib import Path
src=Path('edge_runtime/solution_packs/sporada_secure/runtime_v7/tests/test_v7_runtime.py').read_text();tree=ast.parse(src)
helpers={'Scene','Body','packet','add_quality_plate','binding','config'}
selected=['count_only_binding','quiet_frame_preserves','presence_count_scene_and_anpr','loss_is_unknown','zone_boundary_jitter','candidate_confirmation','frame_receive_time','frame_observed_time','same_track_recovery','motion_quiet_frames','tracker_retained_prediction','small_subject','stream_disconnect_has_unknown','binding_revision_keeps_session','geometry_revision_preserves','shared_polygon','dwell_episode','face_and_body_are_independent','gait_sample_emits','face_window','face_improvement','disconnect_flushes','line_crossing_requires','fire_episode_has_clear','low_score_hazard','plate_attempt_has_one','anpr_accepts_single','anpr_rejects_below','anpr_improved_confidence','anpr_suppresses_noisy']
header='''from pathlib import Path
from types import SimpleNamespace
import os,sys,json,time
import numpy as np
import pytest
sys.path.insert(0,'/opt/traffic-pilot')
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from pipeline import v8_events as events_module
from pipeline.v8_events import V8EventPipeline as ActualPipeline
from pipeline.ocr_stabilizer import OcrStabilizer
CAPTURES={}
class CaptureStore:
    def __init__(self):
        self.root=Path(os.environ['APEXFABRIC_STATE_ROOT']);self.rows=CAPTURES.setdefault(str(self.root),[])
    def persist(self,event,evidence,embeddings):
        rows=[]
        for metadata,blob in evidence:
            p=self.root/(metadata['evidence_id']+'.jpg');p.write_bytes(blob)
            rows.append({'metadata':metadata,'path':str(p)})
        self.rows.append({'observation':event,'evidence':rows,'embeddings':embeddings})
        return True
    def metrics_summary(self):return {'outbox_bytes':0}
    def close(self):pass

def create_pipeline(*args,**kwargs):
    kwargs['handoff_client']=CaptureStore()
    return ActualPipeline(*args,**kwargs)
def events(tmp_path):return [r['observation'] for r in CAPTURES.get(str(tmp_path),[])]
def package_record(tmp_path,observation_id):return next(r for r in CAPTURES[str(tmp_path)] if r['observation']['observation_id']==observation_id)
class Gait:
    def __init__(self):self.buffers={};self.buffer_times={}
    def collect(self,frame,people,camera_id,keys,captured_at=None,**kwargs):
        out={}
        for person in people:
            track=int(person.metadata['track_id']);key=keys[track]
            self.buffers[key]=[np.ones((64,44),np.uint8) for _ in range(24)]
            self.buffer_times[key]=[captured_at-1,captured_at]
            accept=kwargs.get('accept_sequence')
            if accept is None or accept(track,24):
                vector=np.zeros(4096,np.float32);vector[0]=1;out[track]=vector
        return out
'''
chunks=[header];names=[]
for node in tree.body:
 if isinstance(node,(ast.FunctionDef,ast.ClassDef)) and (node.name in helpers or any(node.name.startswith('test_'+s) for s in selected)):
  text=ast.get_source_segment(src,node).replace('V7EventPipeline','create_pipeline').replace('v7_events','events_module').replace('"events_module"','"v8_events"').replace('["v7"]','["v8"]').replace('"v7": {','"v8": {')
  chunks.append(text)
  if node.name.startswith('test_'):names.append(node.name)
out=Path('scripts/isolated_validation/test_policy_scenarios.py');out.write_text('\n\n'.join(chunks)+'\n')
print(len(names),'ported V8 behavioral scenarios')
