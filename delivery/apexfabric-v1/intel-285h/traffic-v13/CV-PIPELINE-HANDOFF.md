# Sporada Secure five-application runtime handoff

This directory is the implementation contract for one CV image providing all five applications: ANPR, vehicle counting, pedestrian counting, smoke/fire detection, and face recognition. Face recognition detects and tracks faces and produces embeddings, but it does **not** identify people. ApexFabric owns persistent person IDs, nearest-neighbor matching, unknown clustering, naming, and sighting history.

## Required runtime interfaces

The container listens on port `8080` and implements:

| Interface | Method/protocol | Purpose |
|---|---|---|
| `/healthz` | `GET` | Process liveness; `200` while the process is alive. |
| `/readyz` | `GET` | `200` only after configuration and models are loaded. |
| `/metrics` | `GET` | Prometheus text metrics. Never include embeddings or credentials. |
| `/events` | SSE | Live, at-most-once analytics events matching `analytics-event.schema.json`. |
| `/snapshots/<camera-id>/<filename>` | `GET` | JPEG or PNG evidence referenced by an event. |

The runtime also makes an outbound request for every emitted face sample:

```text
POST http://apexfabric-ui.apexfabric.svc/internal/face-samples
Content-Type: application/json
Authorization: Bearer ${APEXFABRIC_FACE_IDENTITY_TOKEN}
```

The token is injected from a Kubernetes Secret. Do not put it in desired state, image layers, event payloads, metrics, or logs.

## Application and event matrix

All dashboard events use the common envelope in `analytics-event.schema.json`.

| Application | Event type | Required payload data |
|---|---|---|
| `anpr` | `plate_read` | `plate`, plate `subject`, event-frame snapshot |
| `vehicle_counting` | `vehicle_count_per_frame` | Current-frame `count`, qualifying `objects`, `location`, event-frame snapshot |
| `pedestrian_counting` | `pedestrian_count_per_frame` | Current-frame `count`, qualifying `objects`, `location`, event-frame snapshot |
| `fire_smoke_detection` | `smoke_detected` or `fire_detected` | Detected `subject`, event-frame snapshot |
| `face_recognition` | `face_seen` | Anonymous face metadata, event frame, face crop; plus the separate embedding request |

People and vehicle counts are the number currently visible in the applicable frame/zone. They are not cumulative line-crossing totals. `payload.count.total` must equal the qualifying objects included in that event.

Every event must reference evidence that already exists and can be fetched from the runtime. Embeddings are forbidden in `/events` for every application.

## Two records for each face observation

Every qualifying face observation produces two records:

1. A `face_seen` event on `/events`. This contains the face location and snapshot URLs, but no embedding.
2. A `POST /internal/face-samples`. This contains the embedding, but no name or persistent identity decision.

Use the exact same `event_id`, `sample_id`, `camera_id`, timestamp, quality, and face-crop URL in both records. Delivery order does not matter. The platform joins retained evidence by `event_id` when the Faces UI is read.

`sample_id` identifies one observation and must be globally unique. Generate it once and reuse it for retries. `event_id` identifies the corresponding analytics event. Neither is a persistent face/person ID.

## Face analytics event

Publish one JSON object in the SSE `data` field. Set the SSE `id` field to the same `event_id`.

```text
id: face-cam-1-1789636800-track-52
event: face_seen
data: {"schema_version":"1.0",...}

```

The complete event is in `event.examples.json`; validate against `analytics-event.schema.json`. Important rules:

- `solution_pack` is `sporada-secure`.
- `application` is `face_recognition`.
- `event_type` is `face_seen`.
- `payload.person_id` and `payload.match_confidence` are always `null`.
- `payload.subject.bbox` uses normalized `0..1` coordinates with `(x1,y1)` at top-left and `(x2,y2)` at bottom-right.
- Both `event_frame` and `face_crop` snapshot assets are required.
- Snapshot paths must begin with `/snapshots/`, must not contain `..`, and must remain readable long enough for the collector to fetch them.
- Never include the embedding in this event, the event journal, or normal logs.

## Embedding request

Validate the outbound body against `face-sample.schema.json`.

```json
{
  "sample_id": "sample-01K59EXAMPLE0000000000000",
  "event_id": "face-cam-1-1789636800-track-52",
  "camera_id": "face-cam-1",
  "observed_at": "2026-09-17T12:00:00Z",
  "model_id": "face-embedding-model-v1",
  "dimensions": 512,
  "embedding": [0.012, -0.073, "... exactly 512 finite numbers total ..."],
  "quality": 0.91,
  "face_crop_url": "/snapshots/face-cam-1/face-crop.jpg"
}
```

The ellipsis above is documentation only and must never be sent. The real array must contain exactly `dimensions` finite JSON numbers. NaN and Infinity are invalid JSON and are rejected.

Model identity is part of the biometric domain. Keep `model_id` stable for byte-compatible embedding behavior. The platform compares embeddings only when both `model_id` and `dimensions` match. A model change requires a new model ID; do not silently reuse the old ID.

Embeddings may be normalized by the runtime, but the server also computes cosine similarity without assuming unit length. A zero vector is invalid for useful matching and must not be submitted.

## Response and retry behavior

Successful first submission returns HTTP `201`:

```json
{
  "sample_id": "sample-01K59EXAMPLE0000000000000",
  "person_id": "person-server-generated-value",
  "match_confidence": 0.94,
  "threshold_used": 0.82,
  "status": "matched"
}
```

For a new unknown cluster, `match_confidence` is `null` and `status` is `created`. A repeated `sample_id` is idempotent and returns its existing server assignment with `duplicate: true`. The runtime may record success metrics, but must otherwise ignore `person_id`; it must not cache that value or put it into later events.

Handle responses as follows:

| Status | Runtime action |
|---|---|
| `201` | Success. Do not resubmit except after an uncertain transport failure. |
| `400` | Permanent payload/model/quality error. Drop after logging bounded metadata only. |
| `401` | Credential/configuration failure. Do not retry rapidly; report unhealthy submission metrics. |
| `408`, `429`, `500`, `502`, `503`, `504` | Retry with exponential backoff and jitter using the same `sample_id`. |
| Other `4xx` | Treat as permanent unless the platform contract is updated. |

Use a 10-second request timeout, at most five attempts, an initial backoff around 250 ms, and a maximum backoff of 10 seconds. Bound the submission queue so an unavailable management service cannot exhaust memory or stall video processing.

## When to emit

Do not submit every detected frame. Maintain camera-local face tracks and emit only when all applicable gates pass:

- A new track appears; or
- The face changes materially; or
- The configured cooldown has elapsed;
- And the quality score is at least `config.emission.minimum_quality`.

The desired state supplies `cooldown_seconds` and `material_change_threshold`. The material-change metric is owned by the CV implementation, but it must be deterministic, documented with the model, and interpreted consistently across releases. The runtime should suppress duplicate emissions from the same track between these gates.

Quality must be a calibrated `0..1` composite appropriate for recognition, considering face size, blur, pose, occlusion, and illumination. Detection confidence is not a substitute for face quality.

## Snapshot requirements

For each emitted observation, write:

- `event_frame`: the full processed camera frame.
- `face_crop`: a recognition-useful crop around the face, with enough margin to remain understandable in the UI.

Write each file atomically before publishing the event. Return the correct `Content-Type`. Apply the retention configuration under `/state/snapshots`, but do not delete a newly emitted asset before the platform has had a reasonable opportunity to fetch it.

## Desired state and secrets

Read `/configs/desired_state.json` and validate it against `desired-state.schema.json`. Camera RTSP values are not present in JSON. `source` points to a mounted secret file such as:

```text
file:/run/secrets/apexfabric/face-cam-1.rtsp
```

Read the referenced file at runtime. Never print the RTSP URL. Support strictly increasing hot-reload revisions without restarting healthy, unchanged camera pipelines.

Each camera may enable any non-empty subset of the five applications. Application zones use normalized four-point polygons. Omitted optional zones mean full-frame processing; the runtime must not invent a hidden zone. The example face model contract is `face-embedding-model-v1` with 512 dimensions. If the delivered image uses a different real model or dimension, update `image-contract.yaml`, the desired-state schema/example, and test vectors together before delivery.

## Minimum metrics

Expose at least:

```text
face_tracks_active{camera_id}
face_samples_emitted_total{camera_id}
face_samples_suppressed_total{camera_id,reason}
face_sample_submissions_total{camera_id,outcome}
face_sample_submission_latency_seconds
face_snapshot_write_failures_total{camera_id,asset}
```

Keep labels bounded. Never use `sample_id`, `event_id`, track ID, image paths, embeddings, or credentials as metric labels.

## Acceptance checklist

- The image runs as UID/GID `10001`, supports a read-only root filesystem, and writes only to declared state/temp mounts.
- Health, readiness, metrics, SSE, and snapshot endpoints pass their contracts.
- Each of the five applications can be enabled independently and together on one camera.
- Live ANPR, current-frame vehicle/people count, smoke/fire, and face events validate against the unified event schema.
- Current-frame counts equal their qualifying object arrays and are not cumulative counters.
- A single stationary tracked face does not generate an event every frame.
- Repeating the same `sample_id` does not create another identity or sighting.
- Similar embeddings with the same model ID cluster server-side without runtime person IDs.
- Different model IDs are never compared.
- Face event and embedding bodies contain no credentials; analytics events contain no embeddings.
- Logs and metrics contain no raw embeddings, RTSP credentials, or face images.
- Network interruption does not stop inference and does not create an unbounded retry queue.
- Snapshot URLs remain fetchable and refer to the same observation as the embedding request.
