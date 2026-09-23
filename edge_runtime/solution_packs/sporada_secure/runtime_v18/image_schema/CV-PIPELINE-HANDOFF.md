# Sporada Secure v18 CV pipeline handoff

This revision implements the A→B vehicle entry/exit contract without changing
or replacing `vehicle_counting`, and moves detector-class ownership entirely
to CV.

## Two separate vehicle applications

| Application | Geometry | Output |
|---|---|---|
| `vehicle_counting` | Four-point zone in `config.zones.vehicle_counting` | Existing `vehicle_count_per_frame` SSE events containing the number of vehicles currently inside the zone. |
| `vehicle_entry_exit_counts` | Directional two-point lines in `config.counting_lines.vehicle_entry_exit_counts` | One durable multipart submission when a tracked vehicle completes a line crossing. No per-frame events and no edge-side cumulative total. |

Both applications may be enabled on the same camera. They must not share or
overwrite each other's configuration or event behavior.

## Crossing semantics

The configured line is oriented from endpoint `a` to endpoint `b`. Endpoint
order is meaningful and must not be sorted or otherwise changed. Looking from
`a` toward `b`, the left side is **IN** and the right side is **OUT**:

- a track moving from the right side to the left side emits `direction: "in"`;
- a track moving from the left side to the right side emits `direction: "out"`.

This is represented by `direction_mapping.right_to_left = "in"` and
`direction_mapping.left_to_right = "out"`. Swapping endpoints `a` and `b`
swaps which physical side of the image is IN and OUT. With normalized image
coordinates whose Y axis increases downward, calculate
`side = (b_y-a_y)*(p_x-a_x) - (b_x-a_x)*(p_y-a_y)`. A positive value is the
visual left side of A→B and a negative value is the visual right side. Apply a
small dead band around zero so tracker jitter on the line does not count.

A crossing is complete only when all of the following are true:

1. The CV runtime's internal detector-class policy identifies the track as a vehicle; Management does not configure model class labels.
2. The same stable tracker ID has existed for at least `minimum_track_age_frames`.
3. Its representative point moves from one configured side of the oriented line to the other by at
   least `minimum_crossing_displacement` in normalized image coordinates.
4. The same `(camera_id, line_id, track_id)` has not emitted another crossing
   within `crossing_cooldown_seconds`.

The recommended representative point is the bottom-center of the vehicle box.
Do not count a box merely touching the line, jittering on the line, appearing on
the opposite side, or remaining stationary near it.

## Vehicle-class ownership

`vehicle_classes` is forbidden in desired state and must not be required or
accepted by the CV desired-state validator. The adapter must not copy a
Management class list into worker configuration, and the crossing pipeline
must not gate tracks against a per-line class list. CV keeps its detector/model
specific vehicle filter internally. A model-label change therefore requires no
Management or desired-state release.

Each completed submission reports the CV result in `vehicle.class`. Keep that
nested shape; do not introduce a top-level `vehicle_class`. The value must be a
1–64 character label matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.

## Authoritative delivery

For each completed crossing:

1. Render an evidence frame containing the vehicle bounding box, counting line,
   and `IN` or `OUT` direction. Do not crop away the surrounding scene.
2. Create the stable `event_id` once.
3. Persist the JSON metadata and JPEG/PNG to the runtime's durable outbox before
   the first network attempt.
4. Send `POST /internal/vehicle-crossings` as `multipart/form-data` with an
   `event` JSON part and a `snapshot` image part.
5. Authenticate with `Authorization: Bearer $APEXFABRIC_FACE_IDENTITY_TOKEN`.
6. Treat HTTP 200 (`existing`) and 201 (`created`) as acknowledgement.
7. Remove the outbox record only after acknowledgement. All retries use exactly
   the same `event_id` and bytes.

Do not also emit `vehicle_entry_exit_crossed` on `/events`; management creates
the retained analytics event from the acknowledged multipart request. This
prevents double ingestion. Existing events for the other five applications,
including `vehicle_count_per_frame`, continue on `/events` unchanged.

The runtime must declare `vehicle_entry_exit_crossed` as durable-only. If it
accidentally reaches the SSE normalization path, suppress it and increment
`vehicle_crossing_sse_suppressed_total`; never add an SSE mapping for it.
Expose outbox depth, acknowledgement, and submission-failure metrics as named
in `image-contract.yaml`.

`vehicle.class` in the multipart JSON is the normative class location. It is a
bounded safe string owned by CV, not a closed Management enumeration.

Example request:

```bash
curl -X POST http://apexfabric-ui.apexfabric.svc/internal/vehicle-crossings \
  -H "Authorization: Bearer $APEXFABRIC_FACE_IDENTITY_TOKEN" \
  -F 'event=@vehicle-crossing.example.json;type=application/json' \
  -F 'snapshot=@crossing.jpg;type=image/jpeg'
```

## Ownership of totals

The edge never sends a running, hourly, or daily cumulative entry/exit count.
Management deduplicates on `event_id` and calculates totals using `observed_at`
in the configured site timezone. A delayed retry is therefore assigned to the
day on which the crossing occurred, not the day on which it arrived.

## Unchanged v14 requirements

HTTP/HTTPS/RTSP camera sources, health/readiness/metrics endpoints, live-tail
SSE behavior, snapshots, face artifact upload, face-sample delivery, embedding
privacy, retention, and process/device requirements remain unchanged. The v14
`face-sample.schema.json` and `face-artifact-response.schema.json` remain the
normative schemas for face recognition.
