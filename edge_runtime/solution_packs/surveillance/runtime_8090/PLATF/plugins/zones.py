"""Zone / line geometry + config -- the spatial context the analytics plugins need.

Zones and lines are authored in NORMALISED coordinates (0..1) so one config fits any
resolution; they are scaled to pixels at load using the configured frame size. Point
tests run on a track's foot-point (bottom-centre of the bbox = where the person stands).
"""
from __future__ import annotations

import os
from pathlib import Path


def point_in_poly(pt, poly) -> bool:
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def line_side(pt, a, b) -> float:
    """Signed side of point pt relative to directed line a->b (>0 left, <0 right)."""
    return (b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0])


def observation_point_norm(obs, cfg) -> tuple[float, float] | None:
    """Return a track foot-point in 0..1 camera coordinates.

    Engine observations use the source frame (often 1280/1920 wide), while the
    dashboard JPEG and its zone canvas are 640 wide. Comparing either pixel space
    directly makes a visually correct zone silently miss. Normalised geometry is
    the only common coordinate space and also handles mixed-resolution cameras.
    """
    if obs.foot_point is None:
        return None
    wh = (obs.meta or {}).get("frame_wh") or cfg.get("frame")
    if not wh or len(wh) != 2 or not float(wh[0]) or not float(wh[1]):
        return None
    return float(obs.foot_point[0]) / float(wh[0]), float(obs.foot_point[1]) / float(wh[1])


def observation_in_zone(obs, zone, cfg) -> bool:
    pt = observation_point_norm(obs, cfg)
    return bool(pt is not None and point_in_poly(pt, zone["poly_norm"]))


def observation_line_side(obs, line, cfg) -> float | None:
    pt = observation_point_norm(obs, cfg)
    return None if pt is None else line_side(pt, line["a_norm"], line["b_norm"])


def _scale_poly(poly, w, h):
    return [(float(x) * w, float(y) * h) for x, y in poly]


def zones_from_dict(cfg: dict) -> dict:
    """Build the pixel-scaled zone config from a raw dict (same shape as the YAML). Used
    for zones drawn LIVE in the UI (normalised polys/lines per camera). Returns
    {frame:(w,h), default:{...}, cameras:{cam:{zones,lines}}} in PIXELS."""
    cfg = cfg or {}
    w, h = cfg.get("frame", [1920, 1080])
    default = cfg.get("default", {}) or {}
    per_cam = cfg.get("cameras", {}) or {}

    def build(block):
        block = block or {}
        zones = []
        for z in block.get("zones", []):
            zones.append({"name": z["name"], "kind": z.get("kind", "intrusion"),
                          "dwell_s": float(z.get("dwell_s", 5)),
                          "poly_norm": [(float(x), float(y)) for x, y in z["poly"]],
                          "poly": _scale_poly(z["poly"], w, h)})
        lines = []
        for ln in block.get("lines", []):
            lines.append({"name": ln["name"],
                          "a_norm": (float(ln["a"][0]), float(ln["a"][1])),
                          "b_norm": (float(ln["b"][0]), float(ln["b"][1])),
                          "a": (float(ln["a"][0]) * w, float(ln["a"][1]) * h),
                          "b": (float(ln["b"][0]) * w, float(ln["b"][1]) * h),
                          "in_side": ln.get("in_side", "right")})
        return {"zones": zones, "lines": lines}

    return {"frame": (w, h), "default": build(default),
            "cameras": {c: build(b) for c, b in per_cam.items()}}


def load_zones(path: str) -> dict:
    """Load a zone config from a YAML file (a `default` block applies to any camera
    without its own entry). Thin wrapper over `zones_from_dict`."""
    try:
        import yaml
    except Exception:
        yaml = None
    cfg = {}
    if path and os.path.exists(path) and yaml is not None:
        cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return zones_from_dict(cfg)


def zones_for(cfg: dict, camera: str) -> dict:
    return cfg["cameras"].get(camera, cfg["default"])
