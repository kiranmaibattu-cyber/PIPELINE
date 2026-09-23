"""Thread-safe, track-keyed OCR stabilizer for the async pipeline.

Async OCR results arrive out of order tagged only with (camera, track_id). This
stabilizer turns that noisy per-frame stream into ONE confirmed plate per track:

  1. GATE each read (Step 2): drop low-confidence, wrong-length, non-Indian-format,
     and (when size is known) too-small-plate reads before they can vote. Our study
     showed <100px plates and <0.4-conf reads are mostly garbage.
  2. VOTE + CONFIRM-AND-HOLD (Step 1): accumulate gated reads weighted by
     confidence x plate-area (best-shot bias), vote per character position on the
     modal length, and once a strong consensus forms LOCK it — the confirmed plate
     never flips afterward. `confirmed_text()` returns only locked plates, so the
     analytics layer fires exactly one plate_read event per track instead of one per
     noisy frame.

`text_for` still returns confirmed-or-provisional for live-view display; the EVENT
path uses `confirmed_text`. `should_ocr` stops re-OCRing confirmed/exhausted tracks.
"""
from __future__ import annotations

import re
import threading
from collections import Counter
from typing import Dict, List, Optional, Tuple

Key = Tuple[str, int]

# Indian plate formats (wide — covers all current variants on the road):
#   standard / HSRP / Delhi : SS D(D) L(LL) NNNN  e.g. TS09EA1234, KA01AB1234,
#                             DL11CAA1111 (Delhi has an extra category letter -> up to 3)
#   Bharat series (2021+)   : YY BH NNNN L(L)      e.g. 22BH1234AB
# (Pre-1989 3-letter-code plates are effectively gone post-HSRP-mandate, so omitted.)
PLATE_RE = re.compile(r"^(?:[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4}|\d{2}BH\d{4}[A-Z]{1,2})$")

def normalize_plate(text: str) -> str:
    """Uppercase and strip spaces/hyphens/dots so 'GJ 03 AY 1097' -> 'GJ03AY1097'."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


class OcrStabilizer:
    def __init__(
        self,
        min_confidence: float = 0.4,
        min_plate_width: int = 0,
        confirm_min_reads: int = 4,
        positional_min_character_ratio: float = 0.6,
        positional_min_length_ratio: float = 0.5,
        prune_after_frames: int = 150,
        max_ocr_attempts: int = 12,
        require_format: bool = True,
        **_legacy,  # tolerate old kwargs (min_length, exact_min_votes, max_history, ...)
    ):
        self.min_confidence = min_confidence
        self.min_plate_width = min_plate_width
        self.confirm_min_reads = confirm_min_reads
        self.pos_char_ratio = positional_min_character_ratio
        self.pos_len_ratio = positional_min_length_ratio
        self.prune_after_frames = prune_after_frames
        self.max_ocr_attempts = max_ocr_attempts
        self.require_format = require_format
        self._lock = threading.Lock()
        self._reads: Dict[Key, List[Tuple[str, float]]] = {}   # (text, weight) gated reads
        self._confirmed: Dict[Key, str] = {}                   # locked plate
        self._provisional: Dict[Key, str] = {}                 # latest raw (display only)
        self._last_frame: Dict[Key, int] = {}
        self._attempts: Dict[Key, int] = {}

    def observe(self, camera: str, track_id: int, text: str, confidence: float,
                frame_idx: int, plate_width: float = 0.0) -> None:
        text = normalize_plate(text)
        key = (camera, int(track_id))
        with self._lock:
            self._last_frame[key] = frame_idx
            if text:
                self._provisional[key] = text
            if key in self._confirmed:
                return
            # --- Step 2: gate ---
            if confidence < self.min_confidence:
                return
            if self.min_plate_width and plate_width and plate_width < self.min_plate_width:
                return
            if self.require_format and not PLATE_RE.match(text):
                return
            # --- Step 1: weighted accumulate + vote ---
            weight = max(0.05, float(confidence)) * max(1.0, float(plate_width))
            self._reads.setdefault(key, []).append((text, weight))
            voted = self._vote(self._reads[key])
            if voted:
                self._confirmed[key] = voted

    def _vote(self, reads: List[Tuple[str, float]]) -> str:
        if len(reads) < self.confirm_min_reads:
            return ""
        total_w = sum(w for _, w in reads)
        # modal length (weighted) must dominate
        lenw: Counter = Counter()
        for t, w in reads:
            lenw[len(t)] += w
        best_len, best_len_w = lenw.most_common(1)[0]
        if best_len_w < self.pos_len_ratio * total_w:
            return ""
        same = [(t, w) for t, w in reads if len(t) == best_len]
        same_w = sum(w for _, w in same)
        out = []
        for i in range(best_len):
            cw: Counter = Counter()
            for t, w in same:
                cw[t[i]] += w
            ch, chw = cw.most_common(1)[0]
            if chw < self.pos_char_ratio * same_w:
                return ""   # this position not yet agreed
            out.append(ch)
        cand = "".join(out)
        return cand if (not self.require_format or PLATE_RE.match(cand)) else ""

    def confirmed_text(self, camera: str, track_id: int) -> Optional[str]:
        with self._lock:
            return self._confirmed.get((camera, int(track_id)))

    def text_for(self, camera: str, track_id: int) -> Optional[str]:
        key = (camera, int(track_id))
        with self._lock:
            return self._confirmed.get(key) or self._provisional.get(key)

    def is_done(self, camera: str, track_id: int) -> bool:
        with self._lock:
            return (camera, int(track_id)) in self._confirmed

    def should_ocr(self, camera: str, track_id: int) -> bool:
        """Skip re-OCR once a track is confirmed OR has burned through the attempt
        budget without converging (distant/blurry plates that never will)."""
        key = (camera, int(track_id))
        with self._lock:
            if key in self._confirmed:
                return False
            return self._attempts.get(key, 0) < self.max_ocr_attempts

    def note_ocr_submit(self, camera: str, track_id: int) -> None:
        key = (camera, int(track_id))
        with self._lock:
            self._attempts[key] = self._attempts.get(key, 0) + 1

    def prune(self, frame_idx: int) -> None:
        with self._lock:
            stale = [k for k, f in self._last_frame.items()
                     if frame_idx - f > self.prune_after_frames]
            for k in stale:
                self._reads.pop(k, None)
                self._confirmed.pop(k, None)
                self._provisional.pop(k, None)
                self._last_frame.pop(k, None)
                self._attempts.pop(k, None)
