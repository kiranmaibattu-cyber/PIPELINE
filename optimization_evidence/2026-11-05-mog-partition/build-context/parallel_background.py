"""Bounded CPU execution for unchanged, independent MOG2 pixel updates.

The camera still performs one ordered update per eligible frame. Row bands
have the same parameters, learning-rate and frame count as a whole-frame
model; morphology remains on the complete foreground image. No frames or
pixels are omitted and no observation/emission policy is owned here.
"""
from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
import threading

import cv2
import numpy as np


_lock = threading.Lock()
_pools = {}


def _after_fork():
    global _lock, _pools
    _lock, _pools = threading.Lock(), {}


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def _cpu_capacity():
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    try:
        from .resource_metrics import CgroupSampler
        root = CgroupSampler._process_cgroup(Path(os.getenv('SENTINEL_CGROUP_ROOT', '/sys/fs/cgroup')))
        quota, period = (root / 'cpu.max').read_text().split()
        if quota != 'max':
            count = min(count, max(1, math.floor(int(quota) / int(period))))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return max(1, count)


class _BoundedPool:
    def __init__(self, workers):
        self.workers = workers
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='gait-mog')
        self.slots = threading.BoundedSemaphore(2 * workers)

    def submit(self, fn, *args):
        self.slots.acquire()
        try:
            future = self.executor.submit(fn, *args)
        except BaseException:
            self.slots.release()
            raise
        future.add_done_callback(lambda unused: self.slots.release())
        return future

    def close(self):
        self.executor.shutdown(wait=True)


def _acquire_pool(workers):
    key = (os.getpid(), workers)
    with _lock:
        if key not in _pools:
            _pools[key] = [_BoundedPool(workers), 0]
        _pools[key][1] += 1
        return key, _pools[key][0]


def _release_pool(key):
    pool = None
    with _lock:
        entry = _pools.get(key)
        if entry is not None:
            entry[1] -= 1
            if entry[1] == 0:
                pool = _pools.pop(key)[0]
    if pool is not None:
        pool.close()


class RowPartitionedMog2:
    def __init__(self, *, history=300, var_threshold=25, detect_shadows=False,
                 partitions=4, workers=None, minimum_pixels=1_500_000):
        capacity = _cpu_capacity()
        self.workers = min(capacity, 8 if workers is None else max(1, int(workers)))
        self.partitions = min(self.workers, max(1, int(partitions)))
        self.minimum_pixels = int(minimum_pixels)
        self.parameters = dict(history=history, varThreshold=var_threshold,
                               detectShadows=detect_shadows)
        self._shape = None
        self._models = []
        self._edges = []
        self._pool_key = self._pool = None
        self._state_lock = threading.RLock()
        self._closed = self._failed = False
        self.last_partitions = 0

    def apply(self, frame, fgmask=None, learningRate=-1):
        with self._state_lock:
            if self._closed or self._failed:
                raise RuntimeError('gait MOG2 background is closed or failed')
            if not isinstance(frame, np.ndarray) or frame.ndim not in (2, 3) or not frame.size:
                raise ValueError('MOG2 requires a nonempty source image')
            shape = (frame.shape, frame.dtype.str)
            if shape != self._shape:
                count = self.partitions if frame.shape[0] * frame.shape[1] >= self.minimum_pixels else 1
                count = min(count, frame.shape[0])
                self._edges = np.linspace(0, frame.shape[0], count + 1).astype(int)
                self._models = [cv2.createBackgroundSubtractorMOG2(**self.parameters)
                                for _ in range(count)]
                self._shape = shape
            self.last_partitions = len(self._models)
            if len(self._models) == 1:
                return self._models[0].apply(frame, fgmask, learningRate)
            if fgmask is not None and (fgmask.shape != frame.shape[:2] or fgmask.dtype != np.uint8):
                raise ValueError('MOG2 output buffer has wrong dimensions or dtype')
            result = np.empty(frame.shape[:2], np.uint8) if fgmask is None else fgmask
            if self._pool_key is not None and self._pool_key[0] != os.getpid():
                self._pool_key = self._pool = None
            if self._pool is None:
                self._pool_key, self._pool = _acquire_pool(self.workers)
            jobs = []
            try:
                for model, start, end in zip(self._models, self._edges, self._edges[1:]):
                    jobs.append(self._pool.submit(model.apply, frame[start:end], result[start:end], learningRate))
                for job in jobs:
                    job.result()
            except BaseException:
                # All writers finish before the retained source/output can be
                # released. A partially advanced state is never reused.
                self._failed = True
                for job in jobs:
                    try:
                        job.result()
                    except BaseException:
                        pass
                raise
            return result

    def getBackgroundImage(self):
        with self._state_lock:
            if not self._models:
                return None
            images = [model.getBackgroundImage() for model in self._models]
            if any(image is None for image in images):
                return None
            return images[0] if len(images) == 1 else np.concatenate(images)

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._models = []
            key, self._pool_key = self._pool_key, None
            self._pool = None
        if key is not None:
            _release_pool(key)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def create_gait_background():
    """Internal execution switch; never a desired-state or quality setting."""
    count = max(1, int(os.getenv('SENTINEL_GAIT_MOG_PARTITIONS', '1')))
    if count == 1 or _cpu_capacity() == 1 or cv2.getNumThreads() != 1:
        return cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=25, detectShadows=False)
    return RowPartitionedMog2(partitions=count)
