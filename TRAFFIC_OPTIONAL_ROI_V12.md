# Traffic v12 Optional Counting ROI

## Behavior

Image: `localhost/traffic-pilot-runtime:intel-285h-2026.09.18-v12`

Public GHCR image:
`ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime:intel-285h-2026.09.18-v12`

Immutable GHCR reference:
`ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime@sha256:0dc4fba44ba00adecd17476903d78aecad485bc70c6ff152e5c1f07cff8c0a07`

Final local image ID:
`c492c418bcfaace681e4fa384cb8ce8bdaa9cbbd51d7562b48edb7be897b7275`

Final local image digest:
`sha256:0dc4fba44ba00adecd17476903d78aecad485bc70c6ff152e5c1f07cff8c0a07`

- `vehicle_counting` and `pedestrian_counting` no longer require `config` or a
  polygon in desired state.
- Without an application ROI, counting covers the full frame and emitted
  locations use `zone:whole_frame`.
- With a four-point application ROI, counting covers that polygon and events
  carry the configured zone ID.
- Supplied polygons remain strictly validated. Unknown config fields,
  malformed coordinates, zero-area polygons, and self-intersections are
  rejected.
- ANPR, fire/smoke, and anonymous face sampling retain their existing optional
  ROI behavior.

## Management Input

This is valid and selects full-frame counting:

```json
{
  "edge_id": "edge-01",
  "revision": 1,
  "cameras": [{
    "camera_id": "traffic-1",
    "source": "file:/run/secrets/apexfabric/traffic-1.rtsp",
    "solution_pack": "sporada-secure",
    "fps": 5,
    "apps": ["vehicle_counting", "pedestrian_counting"]
  }]
}
```

Management can later add `config.zones.vehicle_counting` and
`config.zones.pedestrian_counting` with a higher revision. A geometry-only
update is applied by the running worker without restarting the worker or Podman
container.

## Verification

`python3 scripts/test_optional_counting_roi_live.py` was run against
`rtsp://192.168.1.95:8554/traffic1`.

- 220 initial events used `zone:whole_frame`.
- Revision 2 produced 248 events using `vehicle-roi` and `pedestrian-roi`.
- The container ID and OpenVINO worker PID remained unchanged.
- All 468 events validated against `analytics-event.schema.json`.
- All 468 referenced snapshots existed in the mounted `/state` volume.
- All 40 traffic runtime tests passed.

The reusable test stores its report, SSE captures, state volume, and container
log under `run/traffic-v12-optional-roi-*/`; `run/` is test output and is not a
release input.
