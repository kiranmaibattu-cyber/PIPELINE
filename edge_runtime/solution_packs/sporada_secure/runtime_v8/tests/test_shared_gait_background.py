from pathlib import Path
from types import SimpleNamespace
import sys
import cv2
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/worker'))
from pipeline.person_reid import OpenVINOGaitExtractor,opengait_pretreat
from pipeline.gait_morphology import clean_person_regions

class CountingBackground:
    def __init__(self):
        self.model=cv2.createBackgroundSubtractorMOG2(history=300,varThreshold=25,detectShadows=False)
        self.calls=0
    def apply(self,frame):
        self.calls+=1
        return self.model.apply(frame)


def extractor():
    obj=OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
    obj.backgrounds={'cam':CountingBackground()}
    return obj


def test_shared_masks_match_one_update_per_frame_reference(monkeypatch):
    monkeypatch.setenv('SENTINEL_GAIT_SHARED_BACKGROUND','1')
    obj=extractor();reference=CountingBackground()
    boxes=[[0,0,65,115],[30,4,120,120],[95,2,160,118]]
    for i in range(40):
        f=np.zeros((120,160,3),np.uint8)
        cv2.rectangle(f,(i*3%130,10),(i*3%130+25,105),(190,190,190),-1)
        expected=[opengait_pretreat(m) for m in clean_person_regions(reference.apply(f),boxes)]
        for j,box in enumerate(boxes):
            actual=obj._silhouettes(f,[SimpleNamespace(bbox=box)],'cam',frame_key=('s',i))[0]
            assert (actual is None and expected[j] is None) or np.array_equal(actual,expected[j])
            assert ('background_update' in obj.last_silhouette_timings)==(j==0)
            assert ('background_reuse' in obj.last_silhouette_timings)==(j>0)
        assert obj.backgrounds['cam'].calls==i+1
        np.testing.assert_array_equal(obj.backgrounds['cam'].model.getBackgroundImage(),reference.model.getBackgroundImage())
        obj.release_frame('cam',('s',i))
        assert not obj._foreground_cache


def test_camera_session_shape_and_model_are_separate(monkeypatch):
    monkeypatch.setenv('SENTINEL_GAIT_SHARED_BACKGROUND','1')
    obj=extractor();obj.backgrounds['other']=CountingBackground()
    f=np.zeros((80,100,3),np.uint8);p=[SimpleNamespace(bbox=[0,0,50,70])]
    for cam,key,frame in [('cam',('s',1),f),('cam',('s',1),f),('other',('s',1),f),('cam',('new',1),f),('cam',('new',1),f[:60])]:
        obj._silhouettes(frame,p,cam,frame_key=key)
    assert obj.backgrounds['cam'].calls==3 and obj.backgrounds['other'].calls==1
    obj.backgrounds['cam']=CountingBackground()
    obj._silhouettes(f[:60],p,'cam',frame_key=('new',1))
    assert obj.backgrounds['cam'].calls==1
    obj.release_frame('cam',('older',1))
    assert 'cam' in obj._foreground_cache  # stale completion cannot evict a newer frame


def test_rollback_and_missing_key_keep_legacy_updates(monkeypatch):
    obj=extractor();f=np.zeros((80,100,3),np.uint8);p=[SimpleNamespace(bbox=[0,0,50,70])]
    monkeypatch.setenv('SENTINEL_GAIT_SHARED_BACKGROUND','0')
    for _ in range(3):obj._silhouettes(f,p,'cam',frame_key=('s',1))
    monkeypatch.setenv('SENTINEL_GAIT_SHARED_BACKGROUND','1')
    for _ in range(3):obj._silhouettes(f,p,'cam')
    assert obj.backgrounds['cam'].calls==6 and not obj._foreground_cache


def test_frame_error_releases_shared_mask():
    import pytest
    from pipeline.v8_events import V8EventPipeline
    p=V8EventPipeline.__new__(V8EventPipeline)
    p.camera_id,p.session_id='cam','s'
    p.gait=extractor();p._pending_gait=None
    packet=SimpleNamespace(index=3,frame=np.zeros((80,100,3),np.uint8))
    def broken(packet,faces):
        p.gait._silhouettes(packet.frame,[SimpleNamespace(bbox=[0,0,50,70])],p.camera_id,frame_key=('s',3))
        assert p.gait._foreground_cache
        raise ValueError('frame failure')
    p._process_frame=broken
    with pytest.raises(ValueError,match='frame failure'):p.process(packet)
    assert not p.gait._foreground_cache
