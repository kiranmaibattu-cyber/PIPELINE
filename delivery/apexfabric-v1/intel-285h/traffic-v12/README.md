# Traffic Pilot Runtime - ApexFabric V1 Intel Delivery

This package supports vehicle counting, pedestrian counting, ANPR, fire/smoke detection, and anonymous face-sample extraction for central management recognition.

## Image

```text
ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime:intel-285h-2026.09.18-v12
```

The GHCR package is public. The complete offline archive remains saved locally
as `image-2026.09.18-v12.tar`; Git contains its SHA-256 file but not the 1.9 GB
archive.

The image listens on `0.0.0.0:8080`, runs as UID/GID `10001`, uses the Intel 285H runtime base, and has no UI or Redis dependency. Mount desired state at `/configs/desired_state.json`, optional face management settings at `/configs/face-management.json`, camera and management secrets under `/run/secrets/apexfabric`, and persistent state at `/state`.

## Public Contract

- `GET /healthz`
- `GET /readyz`
- `GET /metrics`
- `GET /events` as live-tail, at-most-once server-sent events with 15-second
  idle heartbeats and `id: <event_id>` on every analytics message
- `GET /snapshots/<state-relative-path>` for event images

`/metrics` follows `metrics.schema.json`. Analytics events follow `analytics-event.schema.json`; snapshot fields use `snapshot_ref` and `snapshot_url`, not host filesystem paths.

Every `/events` connection starts at the current end of
`/state/events/analytics.jsonl`. Existing records are never replayed after a
collector reconnect or container restart. The journal rotates before 512 MiB
and retains only `analytics.jsonl` plus `analytics.jsonl.1`, keeping journal
storage below 1 GiB while connected clients continue on the new file.

## Desired State

Each camera uses `solution_pack: "sporada-secure"` and one or more of these apps:

- `vehicle_counting`
- `pedestrian_counting`
- `anpr`
- `fire_smoke_detection`
- `face_recognition`

All application zones are optional. Vehicle and pedestrian counting use their
four-point zone when supplied and otherwise count occupancy across the whole
frame. ANPR, fire/smoke, and face recognition also process the whole frame when
their zone is absent. Lines are not accepted
by this contract. See `desired-state.example.json`.

## Runtime Details

API-only multi-analytics runtime for Intel 285H supporting people counting,
vehicle counting, ANPR, fire/smoke detection, and anonymous face samples. The image contains no UI, no npm,
and no Vite dashboard. It exposes runtime control and observation endpoints on
`:8080`.

## Runtime

The container starts:

```text
python -m traffic_pilot_runtime.solution_image_entrypoint
```

It reads `/configs/desired_state.json`, validates it against the V1 contract,
accepts `vehicle_counting`, `pedestrian_counting`, `anpr`/`plate_detection`,
`fire_smoke_detection`, and `face_recognition`, compiles the active runtime plan, writes
`/plans/traffic-pilot.runtime_plan.json`, generates the worker camera config, and
starts the OpenVINO worker.

## Hot Reload

The runtime checks desired state every 3 seconds. A newer valid revision is
compiled before it is applied. Geometry-only changes update the running worker
without restarting either the worker or container. Topology changes replace the
worker only after validation and config generation succeed. Invalid updates are
rejected and the previous worker stays active.

## Required Mounts

```text
/configs/desired_state.json
/configs/face-management.json (optional)
/run/secrets/apexfabric/*.rtsp
/run/secrets/apexfabric/management-token (when face upload is enabled)
/state
/dev/dri
/dev/accel
```

Camera `source` values in desired state must be secret references such as:

```text
file:/run/secrets/apexfabric/cam1.rtsp
```

The secret file may contain an RTSP/HTTP stream URL or an absolute local video
path mounted into the container.

## Counting Geometry

Each counting application may use its own four-point polygon:

```text
zones.vehicle_counting    -> vehicle zone occupancy
zones.pedestrian_counting -> pedestrian zone occupancy
```

When a counting zone is omitted, its events use the synthetic
`zone:whole_frame` geometry and report whole-frame occupancy. The entire
`config` object may be omitted when no application requires an ROI.


## Smoke/fire snapshots

When `fire_smoke_detection` raises a fire or smoke alert, the worker writes a JPEG frame snapshot under `/state/snapshots/<camera_id>/` and includes the snapshot path in the alert payload. The image bytes are not embedded in the event so message payloads stay small for MQTT, Kafka, SSE, and similar sinks.
