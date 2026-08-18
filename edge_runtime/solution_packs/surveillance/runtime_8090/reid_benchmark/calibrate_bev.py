"""BEV (Bird's Eye View) camera calibration tool.

What this does
--------------
Computes a homography matrix H that maps pixel coordinates from camera A (ch9)
to the same physical ground point in camera B (ch10).  Both cameras observe the
same flat floor plane (your OPD waiting room).

Because a homography preserves the mapping of any FLAT PLANE between two cameras,
projecting a person's foot-point (bottom-centre of bounding box) through H gives
you where that person's feet *should* appear in ch10 — purely from geometry,
with zero appearance information.

How to use
----------
1.  Run:  python -m reid_benchmark.calibrate_bev
2.  A window opens showing ch9 (left) and ch10 (right) side by side.
3.  Click a FIXED FLOOR POINT in the ch9 half (left).
4.  Click the SAME PHYSICAL POINT in the ch10 half (right).
5.  Repeat for at least 4 more point pairs (6–8 is ideal).
    Good landmarks: chair legs, floor tile corners, door frame base, bin base.
6.  Press  C  to compute the homography.
7.  Press  T  to test — hover over ch9 and see the projected point in ch10.
8.  Press  S  to save calibration to  configs/bev_calibration.json
9.  Press  Q  to quit.

After calibration, run the BEV cross-camera scenario:
    python -m reid_benchmark.runner --models bev_osnet_ain --scenario cross_camera --no-display
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

from .config import load_config


# ---------------------------------------------------------------------------
# State shared with mouse callback
# ---------------------------------------------------------------------------
class CalibState:
    def __init__(self, w_left: int) -> None:
        self.w_left = w_left          # width of left (ch9) panel in the side-by-side image
        self.pts_a: list[list[float]] = []   # ch9 points
        self.pts_b: list[list[float]] = []   # ch10 points
        self.pending_a: list[float] | None = None  # waiting for the ch10 pair
        self.H: np.ndarray | None = None
        self.mouse_pos: tuple[int, int] = (0, 0)
        self.mode = "collect"   # "collect" | "test"

    def add_click(self, x: int, y: int) -> None:
        if x < self.w_left:
            # Left panel → ch9 point
            if self.pending_a is not None:
                print("  [!] Click the matching ch10 point first before clicking a new ch9 point.")
                return
            self.pending_a = [float(x), float(y)]
            print(f"  ch9 point {len(self.pts_a)+1}: ({x}, {y}) — now click the SAME spot in ch10 (right half)")
        else:
            # Right panel → ch10 point
            if self.pending_a is None:
                print("  [!] Click a ch9 point first (left half).")
                return
            bx = float(x - self.w_left)
            by = float(y)
            self.pts_a.append(self.pending_a)
            self.pts_b.append([bx, by])
            print(f"  ch10 point {len(self.pts_b)}: ({bx:.0f}, {by:.0f}) — pair {len(self.pts_b)} recorded")
            self.pending_a = None
            if len(self.pts_a) >= 4:
                print(f"  {len(self.pts_a)} pairs — press C to compute homography")

    def compute(self) -> bool:
        if len(self.pts_a) < 4:
            print(f"  Need at least 4 point pairs (have {len(self.pts_a)})")
            return False
        src = np.array(self.pts_a, dtype=np.float32)
        dst = np.array(self.pts_b, dtype=np.float32)
        self.H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        if self.H is None:
            print("  Homography computation FAILED — points may be collinear or degenerate.")
            return False
        inliers = int(mask.sum()) if mask is not None else 0
        print(f"  Homography computed! Inliers: {inliers}/{len(self.pts_a)}")
        # Reprojection error
        src_h = np.hstack([src, np.ones((len(src), 1), dtype=np.float32)])
        proj_h = (self.H @ src_h.T).T
        proj = proj_h[:, :2] / proj_h[:, 2:3]
        errs = np.linalg.norm(proj - dst, axis=1)
        print(f"  Reprojection errors: mean={errs.mean():.1f}px  max={errs.max():.1f}px")
        return True


# ---------------------------------------------------------------------------
# Mouse callback
# ---------------------------------------------------------------------------
_state: CalibState | None = None

def _mouse_cb(event: int, x: int, y: int, flags: int, param: object) -> None:
    global _state
    if _state is None:
        return
    _state.mouse_pos = (x, y)
    if event == cv2.EVENT_LBUTTONDOWN:
        _state.add_click(x, y)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
COLORS = [
    (0, 255, 0), (0, 140, 255), (255, 0, 128), (0, 220, 220),
    (255, 200, 0), (180, 0, 255), (0, 255, 180), (255, 80, 80),
]


def _draw_overlay(canvas: np.ndarray, state: CalibState, w_left: int) -> np.ndarray:
    out = canvas.copy()
    # Draw divider
    cv2.line(out, (w_left, 0), (w_left, out.shape[0]), (200, 200, 200), 2)

    # Draw paired points + connecting lines
    for i, (pa, pb) in enumerate(zip(state.pts_a, state.pts_b)):
        c = COLORS[i % len(COLORS)]
        ax, ay = int(pa[0]), int(pa[1])
        bx, by = int(pb[0]) + w_left, int(pb[1])
        cv2.circle(out, (ax, ay), 8, c, -1)
        cv2.circle(out, (bx, by), 8, c, -1)
        cv2.putText(out, str(i + 1), (ax + 10, ay + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, c, 2)
        cv2.putText(out, str(i + 1), (bx + 10, by + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, c, 2)
        cv2.line(out, (ax, ay), (bx, by), c, 1, cv2.LINE_AA)

    # Pending ch9 point
    if state.pending_a:
        px, py = int(state.pending_a[0]), int(state.pending_a[1])
        cv2.circle(out, (px, py), 8, (255, 255, 255), 2)
        cv2.putText(out, "?", (px + 10, py + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

    # Test mode: project mouse position from ch9 to ch10
    if state.mode == "test" and state.H is not None:
        mx, my = state.mouse_pos
        if mx < w_left:
            pt_h = np.array([[mx, my, 1.0]], dtype=np.float64)
            proj_h = (state.H @ pt_h.T).T[0]
            px = int(proj_h[0] / proj_h[2]) + w_left
            py = int(proj_h[1] / proj_h[2])
            cv2.drawMarker(out, (mx, my), (0, 255, 255), cv2.MARKER_CROSS, 20, 2)
            if 0 <= px < out.shape[1] and 0 <= py < out.shape[0]:
                cv2.drawMarker(out, (px, py), (0, 255, 255), cv2.MARKER_CROSS, 20, 2)
                cv2.line(out, (mx, my), (px, py), (0, 255, 255), 1, cv2.LINE_AA)

    # Status bar
    n = len(state.pts_a)
    status = f"Pairs: {n}  |  "
    if n < 4:
        status += f"Need {4-n} more  |  "
    if state.H is not None:
        status += "H: OK  |  "
    status += "C=compute  T=test  S=save  Q=quit"
    cv2.rectangle(out, (0, out.shape[0] - 28), (out.shape[1], out.shape[0]), (30, 30, 30), -1)
    cv2.putText(out, status, (8, out.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (220, 220, 220), 1)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _state

    import argparse
    parser = argparse.ArgumentParser(description="BEV calibration tool")
    parser.add_argument("--config", default="configs/benchmark.yaml")
    parser.add_argument("--frame", type=int, default=300, help="Frame index to display for calibration")
    parser.add_argument("--threshold", type=float, default=100.0,
                        help="Foot-point match threshold in ch10 pixels (default 100)")
    args = parser.parse_args()

    config = load_config(args.config)
    videos = config["videos"]
    bench = config["benchmark"]
    process_every = int(bench.get("process_every_n_frames", 10))

    raw_frame = args.frame * process_every

    def read_frame(path: str, frame_n: int) -> np.ndarray | None:
        cap = cv2.VideoCapture(path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_n)
        ok, frame = cap.read()
        cap.release()
        return frame if ok else None

    print(f"Reading frame {args.frame} (raw #{raw_frame}) from both cameras...")
    frame_a = read_frame(videos["ch9_5min"], raw_frame)
    frame_b = read_frame(videos["ch10_5min"], raw_frame)
    if frame_a is None or frame_b is None:
        print("ERROR: Could not read frames from video files.")
        return 1

    # Resize for display (keep aspect ratio, max 540px height per panel)
    def fit(img: np.ndarray, max_h: int = 540) -> np.ndarray:
        if img.shape[0] > max_h:
            s = max_h / img.shape[0]
            return cv2.resize(img, (int(img.shape[1] * s), max_h))
        return img

    fa = fit(frame_a)
    fb = fit(frame_b)
    # Make same height
    h = min(fa.shape[0], fb.shape[0])
    fa = cv2.resize(fa, (int(fa.shape[1] * h / fa.shape[0]), h))
    fb = cv2.resize(fb, (int(fb.shape[1] * h / fb.shape[0]), h))

    # Store scale factors (clicked pixels are in display space; need to convert to original)
    scale_a_x = frame_a.shape[1] / fa.shape[1]
    scale_a_y = frame_a.shape[0] / fa.shape[0]
    scale_b_x = frame_b.shape[1] / fb.shape[1]
    scale_b_y = frame_b.shape[0] / fb.shape[0]

    canvas_base = np.hstack([fa, fb])
    w_left = fa.shape[1]

    _state = CalibState(w_left)

    print("\n=== BEV Calibration Tool ===")
    print("LEFT half = ch9, RIGHT half = ch10")
    print("Click a FLOOR point in ch9, then the SAME point in ch10.")
    print("Repeat 4–8 times. Good landmarks: chair legs, tile corners, door frame base.")
    print("Keys: C=compute homography  T=toggle test mode  S=save  Q=quit\n")

    win = "BEV Calibration (ch9 left | ch10 right)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, canvas_base.shape[1], canvas_base.shape[0])
    cv2.setMouseCallback(win, _mouse_cb)

    while True:
        overlay = _draw_overlay(canvas_base, _state, w_left)
        cv2.imshow(win, overlay)
        key = cv2.waitKey(50) & 0xFF

        if key == ord("q"):
            break
        elif key == ord("c"):
            _state.compute()
        elif key == ord("t"):
            if _state.H is not None:
                _state.mode = "test" if _state.mode != "test" else "collect"
                print(f"  Test mode: {'ON' if _state.mode=='test' else 'OFF'} — hover over ch9 to see projected point in ch10")
            else:
                print("  No homography yet — press C first")
        elif key == ord("s"):
            if _state.H is None:
                print("  No homography — press C first")
                continue
            # Convert display-space points back to full-resolution space
            pts_a_full = [[p[0] * scale_a_x, p[1] * scale_a_y] for p in _state.pts_a]
            pts_b_full = [[p[0] * scale_b_x, p[1] * scale_b_y] for p in _state.pts_b]
            # Recompute H in full-resolution space
            src_full = np.array(pts_a_full, dtype=np.float32)
            dst_full = np.array(pts_b_full, dtype=np.float32)
            H_full, _ = cv2.findHomography(src_full, dst_full, cv2.RANSAC, 5.0)
            if H_full is None:
                print("  Full-res homography failed — save aborted")
                continue

            out_path = Path("configs/bev_calibration.json")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "H_ch9_to_ch10": H_full.tolist(),
                "ch9_points_fullres": pts_a_full,
                "ch10_points_fullres": pts_b_full,
                "foot_threshold_px": args.threshold,
                "ch9_frame_size": [frame_a.shape[1], frame_a.shape[0]],
                "ch10_frame_size": [frame_b.shape[1], frame_b.shape[0]],
                "calibration_frame": args.frame,
                "note": f"BEV calibration for ch9->ch10 homography. foot_threshold_px={args.threshold}"
            }
            out_path.write_text(json.dumps(data, indent=2))
            print(f"  Saved to {out_path}")

    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
