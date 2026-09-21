# Sporada Secure v14 CV image contract

This is the build contract for the five-application CV image. The runtime detects faces and creates embeddings, but ApexFabric owns durable face media, identity clustering, names, merges, and retention.

## Camera sources

Each camera `source` is a file reference such as:

```text
file:/run/secrets/apexfabric/camera7.url
```

Read the UTF-8 URL from that file. The value may use `rtsp://`, `rtsps://`, `http://`, or `https://`. Do not log the URL or copy it into desired state, events, metrics, or error responses. HTTP(S) sources may be continuous video endpoints, HLS manifests, or MJPEG endpoints supported by the runtime's decoder. Readiness must report a configured but temporarily unreachable stream without exposing credentials.

## Runtime endpoints

The container listens on port `8080` and provides:

| Interface | Contract |
|---|---|
| `GET /healthz` | `200` while the runtime process is alive. |
| `GET /readyz` | `200` after desired state and models are loaded and the worker is running. |
| `GET /metrics` | Prometheus text; never include embeddings, tokens, or camera URLs. |
| `GET /events` | Live, at-most-once SSE matching `analytics-event.schema.json`. |
| `GET /snapshots/<camera-id>/<filename>` | Runtime evidence for ordinary analytics events. |

## Face observation delivery

A face must never be submitted without a successfully created crop. Durable crop delivery is independent of SSE and uses the following ordered transaction.

### 1. Create one durable outbox record

Create the crop first, calculate its SHA-256 digest, and persist one outbox record containing:

- `sample_id`, generated once and reused for every retry;
- `event_id`, shared with the optional `face_seen` SSE event;
- `camera_id`, `track_id`, and `observed_at`;
- model ID, dimensions, embedding, and quality;
- local crop path, byte length, media type, and SHA-256 digest;
- independent artifact and embedding acknowledgement state.

Do not put raw embeddings in the event journal or normal logs. Do not remove the crop or outbox record until both management acknowledgements have succeeded.

### 2. Upload the face crop

```http
POST /internal/face-artifacts/<url-encoded-sample-id> HTTP/1.1
Host: apexfabric-ui.apexfabric.svc
Authorization: Bearer ${APEXFABRIC_FACE_IDENTITY_TOKEN}
Content-Type: image/jpeg
X-ApexFabric-Artifact-SHA256: sha256:<64-lowercase-hex-characters>
Content-Length: <bytes>

<exact JPEG bytes>
```

PNG is also accepted with `Content-Type: image/png`. Maximum body size is 20 MiB. The checksum is calculated over the exact request body.

Successful creation returns `201`; an idempotent retry of the same `sample_id`, checksum, and bytes returns `200`:

```json
{
  "artifact_id": "artifact-sha256-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "sample_id": "sample-01K59EXAMPLE0000000000000",
  "sha256": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "content_type": "image/jpeg",
  "size_bytes": 48192,
  "status": "created"
}
```

Validate the response against `face-artifact-response.schema.json` and persist it in the outbox before proceeding. Reusing a `sample_id` with different bytes or checksum returns `409` and is a permanent producer error.

### 3. Submit the embedding

Only after artifact acknowledgement, submit JSON to:

```http
POST /internal/face-samples HTTP/1.1
Host: apexfabric-ui.apexfabric.svc
Authorization: Bearer ${APEXFABRIC_FACE_IDENTITY_TOKEN}
Content-Type: application/json
```

```json
{
  "sample_id": "sample-01K59EXAMPLE0000000000000",
  "event_id": "camera7-1789636800-track-52",
  "camera_id": "camera7",
  "track_id": "52",
  "observed_at": "2026-09-21T12:00:00Z",
  "model_id": "face-embedding-model-v1",
  "dimensions": 512,
  "embedding": ["exactly 512 finite JSON numbers"],
  "quality": 0.91,
  "artifact_id": "artifact-sha256-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "artifact_sha256": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

The string shown inside `embedding` is documentation shorthand only. The actual payload must contain exactly 512 finite JSON numbers. Validate against `face-sample.schema.json`.

Management returns `201` for both a new assignment and an idempotent retry. The runtime must not cache or reuse the returned `person_id`; it is management-owned.

### 4. SSE event

The runtime may publish the corresponding `face_seen` event before or after the durable transaction. Use the same `sample_id` and `event_id`. The event contains no embedding and must not be required for the face to appear in the Faces UI. Its edge-relative `event_frame` and `face_crop` references remain useful, retention-bounded telemetry evidence but are not the durable identity crop. Successful identity delivery must not immediately invalidate those event URLs.

### 5. Complete the outbox record

Remove the outbox record and its local crop only when both conditions are durable locally:

1. Artifact upload was acknowledged with the expected checksum.
2. Embedding submission was acknowledged for the same `sample_id` and `artifact_id`.

After an uncertain transport failure, retry with the same IDs and bytes.

## Response and retry rules

| Status | Required behavior |
|---|---|
| `200`, `201` | Persist acknowledgement and advance the outbox state. |
| `400`, `404`, `409`, `413`, `415`, `422` | Permanent producer/contract error; retain bounded diagnostics without embedding or image bytes. |
| `401`, `403` | Credential/configuration failure; back off and report unhealthy delivery metrics. |
| `408`, `425`, `429`, `500`, `502`, `503`, `504` | Retry indefinitely while the bounded outbox record exists, with exponential backoff and jitter. |
| Network timeout/reset | Retry with the same IDs, checksum, and bytes. |

The local outbox must itself be bounded by age and bytes. When pressure requires dropping an unacknowledged record, increment an explicit failure metric and delete its crop atomically with the record.

## Face quality and clustering inputs

- Keep `model_id` stable only while embedding behavior is byte-compatible.
- L2 normalization is recommended; management also calculates cosine similarity without assuming normalization.
- Do not submit zero vectors, NaN, or Infinity.
- Include a stable `track_id` so management can aggregate observations from the same track.
- Prefer a crop with the complete face, modest context, and consistent alignment. Avoid excessively tight crops.
- Continue applying the configured quality, material-change, new-track, and cooldown gates.

## Acceptance criteria

The image is acceptable when all of the following pass:

1. RTSP, RTSPS, HTTP, and HTTPS camera secrets are read without leaking their values.
2. A face crop is created before any face delivery record.
3. Killing the SSE connection does not prevent the crop and embedding from reaching management.
4. Killing the network after artifact acknowledgement resumes at embedding submission without uploading different bytes.
5. Killing the network before acknowledgement retries the same `sample_id`, checksum, and crop bytes.
6. A duplicate artifact or embedding request is acknowledged idempotently.
7. The local record is removed only after both acknowledgements.
8. `/events` never contains an embedding.
9. Logs and metrics never contain embeddings, tokens, camera URLs, or image bytes.
10. All five applications continue to conform to `analytics-event.schema.json` and `desired-state.schema.json`.
