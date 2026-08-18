import json
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .common import positive_number

logger = logging.getLogger(__name__)

DEFAULT_PROCESSOR_CONFIG_URL = (
    "http://proxy/api/cameras/processor-config"
    "?limit=5&offset=0&configured_only=false"
)
STREAM_SCHEMES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")
USE_CASE_RULES = {
    "vehicle_counting": {
        "line_purposes": {"object_counting"},
        "constraint_zone_types": {"counting"},
    },
    "pedestrian_counting": {
        "line_purposes": {"pedestrian_counting"},
        "constraint_zone_types": {"pedestrian"},
    },
    "wrong_way_driving_detection": {
        "line_purposes": {"wrong_way_direction"},
        "constraint_zone_types": {"counting"},
    },
    "stopped_vehicle_detection": {"zone_types": {"stopped_vehicle"}},
    "vehicle_in_pedestrian_zone_alert": {"zone_types": {"pedestrian"}},
    "plate_detection": {"zone_types": {"plate_roi"}},
    "parking_violation_detection": {"zone_types": {"no_parking"}},
}


def docker_processor_config_url(url):
    url = url or DEFAULT_PROCESSOR_CONFIG_URL
    return (
        url.replace("http://localhost:8077", "http://proxy")
        .replace("http://127.0.0.1:8077", "http://proxy")
        .replace("https://localhost:8077", "http://proxy")
        .replace("https://127.0.0.1:8077", "http://proxy")
    )


def processor_config_url_with_defaults(url):
    parts = urlsplit(docker_processor_config_url(url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("limit", "5")
    query.setdefault("offset", "0")
    query.setdefault("configured_only", "false")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def fetch_processor_config(url, api_key, timeout=20):
    if not api_key:
        raise ValueError("PROCESSOR_API_KEY or --processor-api-key is required")

    response = requests.get(url, headers={"X-API-Key": api_key}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Processor config response must be a JSON object")
    return payload


def safe_source_name(camera, index, used_names):
    raw_name = str(camera.get("name") or camera.get("camera_id") or f"camera_{index}")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name).strip("_.-") or f"camera_{index}"
    if name not in used_names:
        used_names.add(name)
        return name

    camera_id = str(camera.get("camera_id") or index)
    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", camera_id)[-8:] or str(index)
    unique_name = f"{name}_{suffix}"
    counter = 2
    while unique_name in used_names:
        unique_name = f"{name}_{suffix}_{counter}"
        counter += 1
    used_names.add(unique_name)
    return unique_name


def is_file_source(source_type, uri):
    if source_type == "file":
        return True
    normalized_uri = (uri or "").lower()
    return not normalized_uri.startswith(STREAM_SCHEMES)


def geometry_id(item, prefix):
    if item.get("id"):
        return str(item["id"])
    return f"{prefix}:{json.dumps(item.get('points', []), sort_keys=True)}"


def geometry_points(item):
    points = item.get("points") or []
    parsed = [
        (int(point.get("x", 0)), int(point.get("y", 0)))
        for point in points
        if isinstance(point, dict)
    ]
    if item.get("shape") == "rectangle" and len(parsed) >= 2:
        (x1, y1), (x2, y2) = parsed[:2]
        parsed = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return parsed


def ccw(a, b, c):
    return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d):
    return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)


def point_in_polygon(point, polygon):
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        xi, yi = current
        xj, yj = previous
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        ):
            inside = not inside
        previous = current
    return inside


def line_touches_zone(line, zone):
    line_points = geometry_points(line)
    zone_points = geometry_points(zone)
    if len(line_points) < 2 or len(zone_points) < 3:
        return False

    start, end = line_points[:2]
    if point_in_polygon(start, zone_points) or point_in_polygon(end, zone_points):
        return True

    edges = zip(zone_points, zone_points[1:] + zone_points[:1])
    return any(segments_intersect(start, end, edge_start, edge_end) for edge_start, edge_end in edges)


def normalize_runtime_analytics(camera):
    analytics = (camera or {}).get("analytics") or {}
    normalized = {}
    for use_case, config in analytics.items():
        if not config.get("enabled"):
            continue

        rule = USE_CASE_RULES.get(use_case)
        lines = config.get("lines") or []
        zones = config.get("zones") or []
        masks = config.get("masks") or []
        if not rule:
            normalized[use_case] = {"lines": lines, "zones": zones, "masks": masks}
            continue

        line_purposes = rule.get("line_purposes") or set()
        zone_types = rule.get("zone_types") or set()
        constraint_zone_types = rule.get("constraint_zone_types") or set()
        matching_lines = [
            line for line in lines if line.get("purpose") in line_purposes
        ]
        matching_zones = [
            zone for zone in zones if zone.get("type") in zone_types
        ]
        constraint_zones = [
            zone for zone in zones if zone.get("type") in constraint_zone_types
        ]

        line_zone_links = []
        active_lines = []
        if matching_lines:
            for line in matching_lines:
                touched_zones = [
                    zone
                    for zone in constraint_zones
                    if line_touches_zone(line, zone)
                ]
                if not constraint_zones or touched_zones:
                    active_lines.append(line)
                line_zone_links.append(
                    {
                        "line_id": geometry_id(line, "line"),
                        "zone_ids": [
                            geometry_id(zone, "zone") for zone in touched_zones
                        ],
                    }
                )

        normalized[use_case] = {
            "lines": active_lines,
            "zones": matching_zones,
            "constraint_zones": constraint_zones,
            "line_zone_links": line_zone_links,
            "masks": masks,
        }
    return normalized


def processor_camera_sources(payload, file_loop=True):
    sources = {}
    camera_configs = {}
    used_names = set()

    for index, camera in enumerate(payload.get("cameras") or [], start=1):
        if camera.get("enabled") is False:
            continue

        source = camera.get("source") or {}
        processing = camera.get("processing") or {}
        uri = source.get("uri")
        if not uri:
            logger.warning(
                "Skipping camera without source uri: %s", camera.get("camera_id")
            )
            continue

        name = safe_source_name(camera, index, used_names)
        file_source = is_file_source(source.get("type"), uri)
        fps = positive_number(processing.get("fps")) or positive_number(
            source.get("fps")
        )

        runtime_analytics = normalize_runtime_analytics(camera)
        source_config = {
            "uri": uri,
            "live": not file_source,
            "loop": bool(file_loop) if file_source else False,
            "camera_id": camera.get("camera_id"),
            "analytics": camera.get("analytics") or {},
            "runtime_analytics": runtime_analytics,
        }
        if fps:
            source_config["fps"] = fps

        sources[name] = source_config
        camera["runtime_analytics"] = runtime_analytics
        camera_configs[name] = camera

    if not sources:
        raise ValueError("Processor config did not return any usable enabled cameras")
    return sources, camera_configs


def load_processor_camera_sources(args):
    config_file = getattr(args, "processor_config_file", None)
    if config_file:
        return _load_camera_sources_from_file(config_file, args)
    return _load_camera_sources_from_url(args)


def _load_camera_sources_from_file(config_file, args):
    """Read processor config from a local JSON file (Phase 1 / dev workflow)."""
    while True:
        try:
            with open(config_file) as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError("Processor config file must contain a JSON object")
            sources, camera_configs = processor_camera_sources(
                payload, file_loop=args.file_loop
            )
            logger.info(
                "Loaded %d camera source(s) from processor config file %s",
                len(sources),
                config_file,
            )
            return sources, camera_configs
        except (OSError, ValueError) as exc:
            logger.error(
                "Processor config file %s is unusable: %s. Retrying in %.1fs.",
                config_file,
                exc,
                args.processor_config_retry_interval,
            )
            time.sleep(args.processor_config_retry_interval)


def _load_camera_sources_from_url(args):
    url = processor_config_url_with_defaults(args.processor_config_url)
    while True:
        try:
            payload = fetch_processor_config(
                url,
                args.processor_api_key,
                timeout=args.processor_config_timeout,
            )
            sources, camera_configs = processor_camera_sources(
                payload,
                file_loop=args.file_loop,
            )
            logger.info(
                "Loaded %d camera source(s) from processor config endpoint %s",
                len(sources),
                url,
            )
            return sources, camera_configs
        except requests.RequestException as exc:
            logger.error(
                "Processor config API is not reachable at %s: %s. "
                "Retrying in %.1fs.",
                url,
                exc,
                args.processor_config_retry_interval,
            )
        except ValueError as exc:
            logger.error(
                "Processor config API returned unusable config from %s: %s. "
                "Retrying in %.1fs.",
                url,
                exc,
                args.processor_config_retry_interval,
            )
        time.sleep(args.processor_config_retry_interval)
