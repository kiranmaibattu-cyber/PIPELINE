"""Bounded camera-local reuse of identical, same-frame image work.

Never caches observations or their IDs. Content hashing deliberately protects
against mutable arrays, reused storage and different crops of the same frame.
Owned by the serial camera policy worker; no raw frame/surface is retained.
"""
from collections import OrderedDict
import hashlib

import cv2
import numpy as np


class FrameWorkCache:
    def __init__(self, max_entries=8, max_bytes=8 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.retained_bytes = 0

    @staticmethod
    def key(frame_id, image):
        array = np.ascontiguousarray(image)
        return (frame_id, array.shape, array.dtype.str,
                hashlib.sha256(memoryview(array).cast("B")).digest())

    def get(self, key):
        item = self.entries.get(key)
        if item is None:
            return None
        self.entries.move_to_end(key)
        return item[0]

    def put(self, key, value, size):
        if size > self.max_bytes or self.max_entries <= 0:
            return
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.retained_bytes -= previous[1]
        while self.entries and (len(self.entries) >= self.max_entries or
                                self.retained_bytes + size > self.max_bytes):
            _, (_, removed) = self.entries.popitem(last=False)
            self.retained_bytes -= removed
        self.entries[key] = (value, size)
        self.retained_bytes += size

    def jpeg(self, frame_id, image, *, content_key=None):
        key = ("jpeg", 88, content_key or self.key(frame_id, image))
        cached = self.get(key)
        if cached is not None:
            return cached, True
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise RuntimeError("JPEG evidence encoding failed")
        body = encoded.tobytes()
        value = (body, "sha256:" + hashlib.sha256(body).hexdigest())
        self.put(key, value, len(body))
        return value, False
