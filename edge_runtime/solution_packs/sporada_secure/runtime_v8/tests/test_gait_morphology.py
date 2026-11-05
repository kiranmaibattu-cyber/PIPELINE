from pathlib import Path
from types import SimpleNamespace
import sys
import cv2
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/worker'))
from pipeline.gait_morphology import clean_foreground,clean_person_regions
from pipeline.person_reid import OpenVINOGaitExtractor,opengait_pretreat


def test_region_morphology_matches_full_frame_at_edges_and_small_crops():
    rng=np.random.default_rng(21)
    for _ in range(20):
        mask=rng.integers(0,2,(121,183),dtype=np.uint8)*255
        boxes=[[0,0,183,121],[0,0,1,1],[181,119,183,121],[3,7,55,89],[-10,-10,220,160],[-10,0,-1,80],[50,30,40,20]]
        reference=clean_foreground(mask)
        for bbox,actual in zip(boxes,clean_person_regions(mask,boxes)):
            expected=OpenVINOGaitExtractor._crop(reference,bbox)
            if expected.size:
                np.testing.assert_array_equal(actual,expected)
            else:assert actual is None


def test_stateful_background_updates_and_normalized_sequences_stay_identical():
    current=OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
    current.backgrounds={}
    old=cv2.createBackgroundSubtractorMOG2(history=300,varThreshold=25,detectShadows=False)
    people=[SimpleNamespace(bbox=[0,0,60,110]),SimpleNamespace(bbox=[42,3,130,120])]
    for frame_index in range(20):
        frame=np.zeros((120,160,3),np.uint8)
        cv2.rectangle(frame,(frame_index*3,10),(frame_index*3+30,100),(200,200,200),-1)
        for person in people:
            expected=opengait_pretreat(OpenVINOGaitExtractor._crop(clean_foreground(old.apply(frame)),person.bbox))
            actual=current._silhouettes(frame,[person],'cam')[0]
            assert (actual is None and expected is None) or np.array_equal(actual,expected)
        np.testing.assert_array_equal(current.backgrounds['cam'].getBackgroundImage(),old.getBackgroundImage())
        assert set(current.last_silhouette_timings) == {
            'background_update', 'region_morphology', 'silhouette_normalization'}
        assert all(value >= 0 for value in current.last_silhouette_timings.values())


def test_preprocessing_substages_are_reported_without_device_attribution():
    from collections import defaultdict
    gait = OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
    gait.backgrounds, gait.centers, gait.cache = {}, {}, {}
    gait.buffers, gait.buffer_times = defaultdict(list), defaultdict(list)
    gait.minimum_motion_pixels = 2
    gait.min_sequence, gait.max_sequence = 20, 60
    observed = []
    person = SimpleNamespace(bbox=[5, 5, 40, 80], metadata={"track_id": 1})
    for camera in ['a', 'b']:
        gait.collect(np.zeros((100, 100, 3), np.uint8), [person], camera,
                     {1: camera + '-presence'}, captured_at=1.0,
                     observe=lambda stage, seconds, **kwargs: observed.append((stage, seconds, kwargs)))
    assert gait.backgrounds['a'] is not gait.backgrounds['b']
    for stage in ['background_update', 'region_morphology', 'silhouette_normalization']:
        samples = [row for row in observed if row[0] == 'person_identity.gait_' + stage]
        assert len(samples) == 2
        assert all(seconds >= 0 and not kwargs for _, seconds, kwargs in samples)
