from pathlib import Path
import sys
import hashlib
import cv2
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/worker'))
from pipeline.frame_work_cache import FrameWorkCache


def test_jpeg_bytes_match_original_and_mutation_is_not_reused():
    image = np.random.default_rng(7).integers(0, 256, (240, 320, 3), dtype=np.uint8)
    cache = FrameWorkCache()
    (body, digest), hit = cache.jpeg('frame1', image)
    assert not hit
    assert body == cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])[1].tobytes()
    assert digest == 'sha256:' + hashlib.sha256(body).hexdigest()
    assert cache.jpeg('frame1', image.copy()) == ((body, digest), True)
    image[:] = 0
    (changed, _), hit = cache.jpeg('frame1', image)
    assert not hit and changed != body
    assert not cache.jpeg('frame2', image)[1]


def test_noncontiguous_crop_and_camera_cache_are_independent():
    image = np.arange(100 * 120 * 3, dtype=np.uint8).reshape(100, 120, 3)
    crop = image[1:70, 3:65]
    a, b = FrameWorkCache(), FrameWorkCache()
    first = a.jpeg('f', crop)
    assert a.jpeg('f', crop.copy())[1]
    assert not b.jpeg('f', crop)[1]
    assert first[0][0] == cv2.imencode('.jpg', crop, [int(cv2.IMWRITE_JPEG_QUALITY), 88])[1].tobytes()


def test_bounded_eviction_and_oversized_result():
    cache = FrameWorkCache(max_entries=2, max_bytes=10)
    cache.put('a', b'1234', 4)
    cache.put('b', b'1234', 4)
    assert cache.get('a') == b'1234'
    cache.put('c', b'1234', 4)
    assert cache.get('b') is None
    assert cache.retained_bytes == 8
    cache.put('big', b'x' * 11, 11)
    assert cache.get('big') is None and cache.retained_bytes == 8
