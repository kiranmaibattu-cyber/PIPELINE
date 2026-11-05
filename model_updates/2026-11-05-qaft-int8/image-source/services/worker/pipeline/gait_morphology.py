"""Exact local morphology; leaves the full-frame MOG update unchanged."""
import cv2


def clean_foreground(mask):
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel, iterations=2)


def clean_person_regions(foreground, boxes):
    height, width = foreground.shape[:2]
    regions = []
    for box in boxes:
        x1, y1, x2, y2 = map(int, box)
        # Match the existing NumPy crop semantics, including clipped bounds.
        y1, y2, _ = slice(max(0, y1), min(height, y2)).indices(height)
        x1, x2, _ = slice(max(0, x1), min(width, x2)).indices(width)
        if x2 <= x1 or y2 <= y1:
            regions.append(None)
            continue
        # Opening: radius 2; closing with two iterations: radius 4.
        # Six source pixels preserve every dependency of the requested crop.
        left, top = max(0, x1 - 6), max(0, y1 - 6)
        right, bottom = min(width, x2 + 6), min(height, y2 + 6)
        cleaned = clean_foreground(foreground[top:bottom, left:right])
        regions.append(cleaned[y1-top:y2-top, x1-left:x2-left])
    return regions
