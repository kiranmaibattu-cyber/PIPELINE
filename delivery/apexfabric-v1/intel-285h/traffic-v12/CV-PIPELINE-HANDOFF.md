# Sporada Secure CV pipeline delivery contract

Status: required integration contract for the next Sporada Secure image.

This document is the handoff and acceptance checklist for the CV pipeline
developer. The machine-readable files beside it are authoritative:

- `image-contract.yaml`
- `desired-state.schema.json` and `desired-state.example.json`
- `analytics-event.schema.json` and `event.examples.json`

The control plane stores valid runtime evidence exactly as advertised. It does
not rewrite malformed event paths, synthesize missing snapshots, infer missing
bounding boxes, or translate line geometry into zones.

## Required HTTP service behavior

The image must expose one management port, normally `8080`, with:

| Endpoint | Required behavior |
|---|---|
| `GET /healthz` | Process liveness; fast `200` response while the process is healthy. |
| `GET /readyz` | `200` only after configuration is applied and event/snapshot endpoints can serve. |
| `GET /metrics` | The content type and format declared by the image contract. |
| `GET /events` | Long-lived `text/event-stream`; one complete JSON event in each `data:` record, terminated by a blank line. |
| `GET /snapshots/<camera-id>/<filename>` | Return the exact referenced image with `200` and `image/jpeg` or `image/png`. |

All endpoints must work through the Kubernetes pod proxy. They must not depend
on the request Host header, NodePort, host networking, localhost-only companion
services, or a browser session.

`/events` is a live-tail, at-most-once interface. Each connection starts at the
current EOF of `analytics.jsonl`; it must not replay records that predate the
connection, including after a pod restart. Every analytics message has
`id: <event_id>`, and an idle connection receives an SSE comment heartbeat every
15 seconds. Durable acknowledged delivery is a separate protocol and must not
be simulated by replaying this journal.

The runtime rotates `analytics.jsonl` before 512 MiB, retains only the active
file and `analytics.jsonl.1`, and keeps their combined size below 1 GiB. An SSE
client connected during rename-based rotation continues with the new active
file without reconnecting or receiving records from the retained predecessor.

Snapshot URLs are relative HTTP paths, not filesystem paths. The following is
valid:

```text
/snapshots/traffic-1/vehicle-13051.jpg
```

The v5 form below is invalid because it duplicates the route prefix:

```text
/snapshots/snapshots/traffic-1/vehicle-13051.jpg
```

Before publishing an SSE event, write the complete snapshot atomically and
verify that its declared URL returns it. Keep runtime evidence available long
enough for the collector to reconnect and fetch it; the minimum requirement is
10 minutes. An event must never advertise a temporary or partially written file.

## Dashboard events and evidence

Emit only the current-in-frame dashboard count events. Do not use cumulative
line-crossing events for the current people/vehicle dashboard totals.

| Application | Event type | Required event-specific data |
|---|---|---|
| `anpr` | `plate_read` | `payload.plate.text`, plate bounding box |
| `vehicle_counting` | `vehicle_count_per_frame` | `payload.count.total`, `payload.objects[]` for the vehicles currently inside that app's zone |
| `pedestrian_counting` | `pedestrian_count_per_frame` | `payload.count.total`, `payload.objects[]` for people currently inside that app's zone |
| `fire_smoke_detection` | `smoke_detected` | smoke bounding box and confidence |
| `fire_smoke_detection` | `fire_detected` | fire bounding box and confidence |

Every event in this table must carry:

- `payload.snapshot_ref`
- `payload.snapshot_url`
- `payload.snapshot_content_type`
- `payload.snapshot_assets.event_frame`

The event frame must be the full frame whose pixel coordinate system is used by
the bounding boxes. For count events, `payload.count.total` must equal the number
of qualifying objects in `payload.objects`. Zero is valid: emit an empty objects
array and an evidence frame if zero-count updates are emitted. Bounding-box
coordinates must be finite, non-null pixel coordinates with `x1 < x2` and
`y1 < y2`, inside the evidence image dimensions.

Use a globally unique, immutable `event_id`. Retries preserve the ID and exact
payload. `timestamp` is the UTC occurrence time in RFC3339 format. Do not reuse
IDs for subsequent frames.

## Separate four-point zones per application

People and vehicle counting require polygons, not counting lines. Each is
configured independently:

```text
config.zones.vehicle_counting[]
config.zones.pedestrian_counting[]
```

Each polygon has exactly four normalized `[x, y]` points, each coordinate in
`[0,1]`. Preserve the supplied point order. A valid quadrilateral must be
non-self-intersecting and have non-zero area. The pipeline must count an object
only for the application zone being evaluated; the two apps must not silently
share or overwrite geometry.

The runtime must apply configuration transactionally:

1. Read and validate the entire desired-state document.
2. Reject invalid or stale revisions without altering the last good state.
3. Apply all camera/app/zone changes together.
4. Report ready only after the new revision is active.
5. Ensure subsequent event `location.id` values match the configured zone ID.

The UI may supply separate zones for ANPR and fire/smoke using the same
four-point representation. People/vehicle zones are mandatory whenever their
corresponding apps are enabled.

## Persistence and shutdown

Runtime working snapshots belong under the declared persistent `/state` volume.
Write files with a temporary name and atomically rename them into place. On
SIGTERM, stop accepting new frames, complete or discard in-progress writes,
flush event state, close the SSE connection, and exit within the pod termination
grace period. Restarting a pod must not expose corrupt evidence.

The platform control plane copies evidence into its own content-addressed store,
serves historical evidence independently of the runtime, and enforces its 24-hour
and 500 MiB retention policy. The runtime must not write directly into the
control-plane database or snapshot directory.

## Security and operational requirements

- Run as the non-root UID/GID declared by the image contract.
- Read RTSP sources only through `file:/run/secrets/apexfabric/<camera-id>.rtsp`.
- Never emit credentials, host filesystem paths, base64 images, or authorization
  headers in events or logs.
- Keep the root filesystem read-only compatible; write only to declared state
  and temporary mounts.
- Bound queues and reconnect behavior. A slow SSE consumer must not cause
  unbounded memory growth.
- Log configuration rejection and snapshot-serving failures with camera ID and
  event ID, without secrets.
- Pin release images by digest and supply architecture, model versions, schemas,
  health checks, resource requirements, and an SBOM with the delivery.

## Release acceptance gate

The image is acceptable only when all checks pass:

1. The image contract and desired-state/event examples validate using
   `scripts/validate-sporada-contract.py`.
2. People and vehicle configuration each accepts exactly four-point zones and
   rejects a two-point line, three-point polygon, fifth point, out-of-range point,
   self-intersection, and zero-area polygon.
3. A live event of every required type validates against
   `analytics-event.schema.json`.
4. Every live event's `snapshot_url` returns `200`, an allowed image content type,
   non-empty content, and no more than 20 MiB through `kubectl get --raw`.
5. Each evidence image decodes successfully and every bounding box fits it.
6. Count totals equal the qualifying object arrays and change with the current
   frame rather than accumulating over time.
7. Disconnect/reconnect does not produce corrupt JSON; retries retain event IDs.
8. SIGTERM and restart preserve the last valid configuration and do not expose
   partial images.

Any failed item blocks catalog promotion. Do not label or publish the image as a
conforming Sporada Secure release until this gate passes.
