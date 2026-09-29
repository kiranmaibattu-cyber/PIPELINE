# Sentinel CV image contract v4 — V1 draft

Status: **DRAFT FOR CV-TEAM REVIEW**. This package defines the proposed V1
boundary between a Sentinel CV runtime and the ApexFlo management control
plane. A CV image is not v4-conformant until its behavior has been tested and
the image has been explicitly cataloged by an administrator.

Handoff revision: **4.0-draft.1 (2026-09-29)**. This directory is the complete,
self-contained CV-team review package. Its interface is `/api/v4/ingest/*`.
Control-plane rollout status is deliberately not part of the contract: the CV
team can review and implement this package while management-plane deployment is
validated separately. V2 and v3 packages are historical references only. A
deployment uses v4 exclusively and must not mix contract payloads.

## 1. Normative files and precedence

The files in this directory form one contract:

| File | Normative purpose |
| --- | --- |
| [`image-contract.yaml`](image-contract.yaml) | Runtime, security, resource, delivery and operational requirements. |
| [`desired-state.schema.json`](desired-state.schema.json) | Management-issued runtime configuration. |
| [`observation.schema.json`](observation.schema.json) | Closed, typed Event vocabulary selected by `event_type`. |
| [`evidence.schema.json`](evidence.schema.json) | Evidence metadata sent with binary multipart content. |
| [`embedding.schema.json`](embedding.schema.json) | Face, body, gait and scene embedding submissions. |
| [`embedding-profile.schema.json`](embedding-profile.schema.json) | Catalog declaration for every emitted embedding space. |
| [`acknowledgement.schema.json`](acknowledgement.schema.json) | Successful ingest acknowledgement. |
| [`EVENT-VOCABULARY.md`](EVENT-VOCABULARY.md) | Field meaning, units, stability and cross-event semantics. |
| [`CV-TEAM-HANDOFF.md`](CV-TEAM-HANDOFF.md) | Implementation checklist and unresolved CV questions. |

JSON Schemas are authoritative for payload shape. `image-contract.yaml` is
authoritative for runtime and delivery behavior. `EVENT-VOCABULARY.md` is
authoritative for semantic meaning and units. If these disagree, the package
must be corrected and reviewed; an implementation must not choose whichever
interpretation is convenient.

The database table name `observations` and payload key `observation_id` are
retained technical identifiers. Product and UI language calls these records
**Events**.

## 2. Division of responsibility

The CV runtime owns facts observed at the edge:

- stream decoding, detection and tracking;
- typed measurements and positive zone/line transitions;
- evidence generation;
- declared face, body, gait and scene vectors;
- camera health reporting; and
- a durable local submission outbox.

The management plane owns decisions and durable business state:

- deployment, camera, zone, line, threshold and profile identifiers;
- desired-state revisions and camera credential Secrets;
- persistence, retention and searchable Event history;
- identity records and cross-camera candidate review;
- Guard Rules, Incidents and alerts.

CV must not emit a management `person_id`, decide watchlist membership, claim
that vector similarity is a confirmed identity, or open an Incident directly.
A CV `threshold_exceeded` Event is telemetry; management still decides whether
an active Guard Rule matches.

## 3. Runtime requirements

The image must:

- run on `amd64` and the Intel Core Ultra 285H hardware profile;
- run as UID/GID `10001:10001` without privilege escalation;
- support a read-only root filesystem;
- listen on port `8080` without requiring host networking;
- expose `GET /healthz`, `GET /readyz`, and `GET /metrics`;
- read desired state from `/configs/desired_state.json`;
- read camera URLs only from
  `file:/run/secrets/sentinel/cameras/<camera-id>.url`;
- read the deployment bearer token from `SENTINEL_EDGE_TOKEN`;
- use `/state` for its durable outbox and Event journal; and
- use only `/tmp/sentinel` for disposable working data.

The declared resource envelope is 8 requested/16 limited CPU cores, 16 GiB
requested/32 GiB limited memory, and at most eight camera streams. The runtime
may use `/dev/dri` and `/dev/accel`. An image that needs different mounts,
privileges, devices, ports or credential paths does not satisfy this draft.

Readiness means more than “the process is alive.” `/readyz` must fail when the
runtime has no valid current desired state, cannot read required Secrets, or
cannot initialize a configured stream/output. `/healthz` reports process
liveness. Metrics must not expose credentials or raw embedding vectors.

## 4. Desired state

Management mounts one document conforming to
`desired-state.schema.json`. The runtime must accept only `contract: v4`, its
own `deployment_id`, and a strictly increasing positive `revision`.

Each camera configuration contains:

- the management-issued edge, deployment and camera IDs and secret-file source;
- processing FPS;
- enabled outputs and Event types;
- polygon zones and ordered two-point lines;
- configured count/dwell thresholds; and
- enabled embedding profile IDs.

The runtime must poll the mounted file at least every two seconds. It must
validate a complete new revision before applying it atomically. On a malformed,
stale or unsupported revision it must retain the last valid configuration,
become not-ready, and expose a diagnostic without printing secret contents.

`observations` is mandatory. Any enabled embedding output requires `evidence`.
`scene_sample` and `fire_smoke_suspected` also require evidence. Zone and
threshold IDs are opaque management identifiers and must be echoed exactly;
CV must not invent substitutes.

## 5. Event vocabulary

Every Event is a closed object validated by `observation.schema.json`. It has
a common envelope and exactly one typed branch:

| Event type | Meaning |
| --- | --- |
| `object_present` | Baseline person/vehicle detection with required session, track and normalized full-frame geometry. |
| `person_count`, `vehicle_count` | As-of zone measurement with explicit semantics, method and coverage. |
| `dwell` | Ongoing or positively completed stay by a session-scoped subject. |
| `threshold_exceeded` | Advisory crossing of a management-issued configured threshold. |
| `plate_read` | Visible plate text, quality, completeness and normalized plate box. |
| `line_cross` | Positive crossing of a configured line in `forward` or `reverse` direction. |
| `zone_entry`, `zone_exit` | Positive polygon-boundary transitions; track loss is not an exit. |
| `scene_sample` | Full-frame or polygon-scoped JPEG sample, optionally linked to a separately submitted scene vector. |
| `fire_smoke_suspected` | Suspected fire/smoke signal with evidence; never a safety confirmation. |
| `camera_health` | Timestamped coverage state and reason. |

Important semantics:

- `observed_at` is occurrence time in RFC 3339 UTC, not HTTP arrival time.
- A new measurement or state transition gets a new globally unique
  `observation_id`; retries reuse the original ID and exact bytes.
- `stream_session_id` changes whenever a camera stream is reopened or the CV
  process restarts that stream.
- `track_id` is unique and stable only within one stream session.
- Optional `presence_id` may bridge a short CV-side track reassignment within
  the same camera/session. It is not a person or cross-camera identity.
- `bbox_normalized` coordinates are finite `[0,1]` values relative to the full,
  unrotated source frame with top-left origin.
- `zone_exit` and completed dwell require a positive observation. Missing
  tracks, stale frames and camera failure do not prove departure.
- V18.1 count output is normally `visible_now`, not persistent occupancy.
  Stale/degraded/unavailable counts must not be presented as zero or occupancy.

See `EVENT-VOCABULARY.md` for every required field, unit and stability rule.

## 6. Evidence and scene samples

Evidence is uploaded as `multipart/form-data` to
`POST /api/v4/ingest/evidence`: a JSON `metadata` part plus one binary `file`
part. Images may be JPEG or PNG up to 20 MiB. Generic clip transport supports
MP4 up to 500 MiB, but V1 defines no policy-driven clip-capture desired state.

Metadata must declare exact byte size and `sha256:<lowercase hex>` content
digest. The API verifies MIME type, size, hash, capture time and clip duration.
Evidence cannot be accepted before its parent Event has been acknowledged.

A `scene_sample` always references JPEG evidence. `zone_id: null` means the
full frame; otherwise it must reference a configured polygon. The Event can
exist without a vector. If `embedding_id` is present, the vector is submitted
separately through the ordinary embedding route after the evidence is
acknowledged and must use the scene profile and the same parent Event.

## 7. Embedding profiles and identity interpretation

Every emitted embedding must exactly match a profile declared by the cataloged
image and enabled for that camera. Compatibility is equality across profile
ID, kind, model ID/version, dimension, embedding space, distance metric and
normalization. Vectors from unequal profiles must never be compared.

| Profile ID | Kind | Model | Dimension | Metric / normalization |
| --- | --- | --- | ---: | --- |
| `adaface-ir101-v18.1` | face | AdaFace IR101 INT8 | 512 | cosine / L2 |
| `transreid-ssl-v18.1` | body | TransReID SSL INT8 | 384 | cosine / L2 |
| `gaitbase-v18.1` | gait | GaitBase INT8 | 4096 | cosine / L2 |
| `siglip2-base-v1` | scene | `google/siglip2-base-patch16-224` revision `75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2` | 768 | cosine / L2 |

The AdaFace, TransReID and GaitBase `v18.1` version labels are provisional
until CV supplies pinned weight artifact revisions. Changing a model, weights,
preprocessing that changes the vector space, dimension, metric or normalization
requires a new immutable profile ID and a compatible management storage table.

Named recognition is **face-only** and requires management review. Body and
gait matches are candidate-association evidence only and must never be shown as
confirmed identity. Scene vectors are for future shared-space semantic search,
not identity. Plate text is a vehicle observation, not person identity.

## 8. Submission protocol

The management base URL comes from `SENTINEL_INGEST_BASE_URL`, defaulting to
`http://sentinel-ingestor.sentinel.svc.cluster.local:8080`. Every request uses
`Authorization: Bearer <SENTINEL_EDGE_TOKEN>`. The token is bound to one v4
deployment and its authorized camera IDs.

| Order | Endpoint | Content | Idempotency key |
| ---: | --- | --- | --- |
| 1 | `POST /api/v4/ingest/events` | `observation.schema.json` JSON | `observation_id` |
| 2 | `POST /api/v4/ingest/evidence` | Metadata JSON plus binary multipart file | `evidence_id` |
| 3 | `POST /api/v4/ingest/embeddings` | `embedding.schema.json` JSON | `embedding_id` |

An embedding requires both its parent Event and evidence to have been
acknowledged. A producer may continue unrelated outbox records while one chain
is waiting, but it must never send a child before its own parent.

The runtime must persist payloads and binary evidence in `/state` before the
first attempt. It removes them only after a valid management acknowledgement.
Retries use the same ID and exact bytes. Network loss, management downtime,
credential failure or process restart must not silently discard queued data.

## 9. Responses, retries and dead letters

`200` and `201` with a valid acknowledgement are success. A repeated ID with
identical bytes returns an existing acknowledgement and is also success.

| Result | Required CV behavior |
| --- | --- |
| Timeout, connection failure, `408`, `425`, `429`, or `5xx` | Retain the outbox record and retry with exponential backoff plus jitter; honor `Retry-After` up to 60 seconds. |
| `401` or `403` | Pause submissions, retain all records, alert the operator, and retry the same bytes only after credentials/configuration are repaired. |
| `400`, `404`, `405`, `413`, `415`, or `422` | Create an operator-visible terminal dead letter containing status, response and retained payload; do not retry automatically. |
| `409` | Terminal idempotency conflict: retain an operator-visible dead letter with the original payload and reason; never generate a replacement ID for the same fact. |
| Other `4xx` | Terminal operator-visible dead letter; no automatic retry. |
| Malformed `2xx` acknowledgement | Retain and retry; alert the operator. |

Default request timeout is 30 seconds. Retry delay starts at 250 ms, grows
exponentially with jitter, and is capped at 60 seconds. Retry attempts remain
unlimited while the outbox record is retained.

## 10. Local retention and recovery

The CV runtime retains local evidence for seven days by default. At
1,932,735,283 bytes it must delete the oldest unprotected evidence until usage
is at or below 1,610,612,736 bytes, removing dependent local vectors with it.
These limits apply to the CV runtime's `/state`; management has a separate
retention policy after successful upload.

After a worker restart, the runtime resumes the durable outbox in dependency
order. It must not regenerate IDs, resend changed bytes under an existing ID,
or treat a missing acknowledgement as proof that management did not store the
record.

## 11. Security requirements

- Camera URLs and credentials exist only in mounted Secret files.
- Desired state, Events, logs and metrics must never contain camera passwords
  or the bearer token.
- The runtime must reject camera IDs, deployment IDs, revisions, zones,
  thresholds and profiles that are not in its current desired state.
- It must not require privileged mode, host networking, writable host paths or
  additional Linux capabilities.
- Live SSE, if the image exposes `/events`, is diagnostic only and must never
  contain raw embeddings. Durable management submission remains authoritative.

## 12. V1 exclusions

The V1 contract does not include violence detection, frequent-visitor
detection/alerting, action-context matching, policy-driven clip capture, or an
edge-owned named-identity decision. Generic evidence transport may carry an
MP4 produced for an otherwise supported Event, but no V1 desired-state fields
configure pre/post-roll clip policy.

Face crop/alignment, detector class IDs, tracker mechanics, gait silhouette
buffers and other model internals are implementation details. They must not be
added to desired state or emitted as management rule inputs.

## 13. Conformance checklist

Before catalog approval, the CV image must demonstrate:

1. It starts as UID/GID 10001 with a read-only root filesystem and only the
   declared writable mounts/devices.
2. Health, readiness and Prometheus metrics behave as defined.
3. Valid desired-state revisions apply atomically; invalid/stale revisions keep
   the last valid state and make readiness fail.
4. Camera credentials are read exclusively from the declared Secret files.
5. Every enabled Event type validates against the canonical schema and uses
   management-issued IDs from the active revision.
6. Bounding-box, timestamp, dwell, threshold and zone/line relational rules are
   respected, including positive-only exits.
7. All declared embedding profiles match emitted payloads exactly; a gait
   vector contains 4096 finite values.
8. Event → evidence → embedding ordering survives a forced process restart.
9. Same-ID/same-bytes retries succeed; changed bytes under the same ID become a
   visible 409 dead letter.
10. Transient, authentication and permanent validation failures follow the
    required retry/pause/dead-letter behavior without data loss.
11. V18.1 counts are emitted as `visible_now` unless the implementation can
    prove the stronger `current_occupancy` semantics and required counters.
12. Face is the only modality eligible for named recognition; body/gait remain
    explicitly labelled association candidates.

Open items before final approval are tracked in
[`CV-TEAM-HANDOFF.md`](CV-TEAM-HANDOFF.md).
