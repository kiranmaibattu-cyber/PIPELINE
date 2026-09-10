# Surveillance V5 Deployment And Management Integration

## Release

- Image: `ghcr.io/kiranmaibattu-cyber/surveillance-edge-runtime:intel-285h-2026.09.10-v5`
- Registry digest: `sha256:30aac3bad8d953ef7391b6de2f1bcb6487534345b1f189db003d5871ba225680`
- Image/config ID: `be1f7931fe45e926f09ef13c14a0182c8952151686458f67dd8765808c8039de`
- Target: Intel 285H, linux/amd64. No Jetson or Metis support in this release.
- One running workload container; unchanged Intel base is a build parent, not a sidecar.
- Models remain baked into `/models/surveillance`; no model PVC or download required.
- Port 8080, UID/GID 10001:10001. Host Intel kernel drivers and device permissions are required.
- GHCR visibility checked during publication: private. Authenticated pulls or a Kubernetes
  imagePullSecret are required until the package owner changes visibility to Public.

The published image contains the tested runtime changes. Schemas, this guide and
deployment examples are delivered in Git, not injected into the running image.
Traffic remains on its existing v2 release and is not rebuilt by this update.

## Changes Since V2

- People counting supports full-frame occupancy without geometry, directional
  crossings with lines, and per-polygon occupancy with `config.zones.people_counting`.
  Lines and polygons produce separate modes when both are configured.
- Opt-in authenticated HTTPS management commands and durable upload queue.
- Versioned managed face gallery, staged enrollment and central identity aliases.
- ReID observation export with modality/model-space identifiers and available evidence.
- Saved enrollment chips linked to their corresponding templates; evidence uploads
  precede the candidate upload. Management approval is required to activate recognition.
- Existing exact-frame event evidence, main hardware placement and live desired-state
  reload are preserved. Graph changes can restart the internal worker, not the container.

## Mounts

There is no new standalone gallery volume. New state directories use the existing
required `/state` persistent mount. Mount it once per edge workload; do not share a
single SQLite state directory among different edges or concurrent replicas.

| Container path | Source and access | Purpose |
|---|---|---|
| `/configs` | ConfigMap or managed directory, read-only | `desired_state.json` and optional `management.json` |
| `/run/secrets/apexfabric` | Kubernetes Secret or protected directory, read-only | RTSP references, management token, optional private CA |
| `/state` | PVC or host persistent directory, read-write | All surveillance durable state under `/state/surveillance` |
| `/plans` | Writable temporary directory/emptyDir | Compiled runtime plan and generated config |
| `/tmp/apexfabric` | Writable temporary directory/emptyDir | Runtime temporary files |
| `/dev/shm` | Container shared memory | Frame transport; not persistent evidence storage |
| `/dev/dri`, `/dev/accel` | Device mappings, not storage volumes | Intel GPU and NPU access |

Provide write permission for UID/GID 10001 on writable mounts. Mount `/configs`
as a directory, not a single-file bind or Kubernetes `subPath`: atomic desired-state
updates must be visible. Keep the default writable container filesystem unless
all temporary write paths for a read-only-root deployment are provisioned.

Persistent layout (paths relative to `/state/surveillance`):

| Path | Content |
|---|---|
| `face_gallery/managed.json` | Applied management-owned gallery revision and templates |
| `face_gallery/index.json`, `vectors.npy`, `chips/` | Legacy local gallery files; managed mode uses `managed.json` |
| `reid_gallery/`, `history/` | Existing local ReID and observation history |
| `events.jsonl`, `snapshots/`, `crops/` | Local events and evidence |
| `management/sync.sqlite` | Durable pending records, command receipts and bound edge ID |
| `management/enrollment/<session_id>/` | Private staged samples, session metadata and saved candidate |
| `management/identity-mappings.json` | Versioned central aliases scoped by camera, session and local identity |
| `management/artifacts/` | Persistent content-addressed observation/enrollment JPEGs |

Container stdout/stderr logs are handled by the container platform, not automatically
persisted by this PVC. Configure cluster log collection separately.

An edge PVC is not automatically mounted at management. The edge uploads bytes and
records over HTTPS; management must persist them in its own storage and return its
own artifact URLs. An edge-relative `/snapshots/...` URL is not a central URL.

## Management Inputs

1. Mount desired state at `/configs/desired_state.json`: edge ID, increasing revision,
   camera Secret references, selected apps, FPS, counting lines and intrusion/counting
   polygons. The runtime polls every two seconds and rejects invalid revisions.
2. Mount `management-sync.example.json` as `/configs/management.json` and set
   `MANAGEMENT_SYNC_CONFIG=/configs/management.json` to enable central synchronization.
3. Supply token and optional private CA at the paths specified in that JSON.
   HTTPS certificate verification stays enabled. Token/CA rotation currently requires restart.
4. Return commands on the receiver's `GET /v1/edges/{edge_id}/commands` endpoint,
   or submit an authenticated `POST /api/management/commands` to the edge.

Command envelope:

```json
{"command_id":"gallery-status-1","type":"gallery.status","payload":{}}
```

Commands: `gallery.status`, `gallery.replace`, `identity.mappings.replace`,
`enrollment.start`, `enrollment.status`, `enrollment.stop`, `enrollment.save`,
`enrollment.cancel`, `enrollment.retake`. Use a new command ID for each new operation
or status poll. Duplicate identical IDs return durable prior results; conflicting
reuse is rejected. Gallery and mapping revisions reject stale/conflicting updates.

Enrollment flow: start -> inspect status -> stop -> save -> receive candidate ->
management validates -> management sends higher-revision `gallery.replace`.
Candidate `samples[].template_index` links to `templates`; `artifact_key` links to
`artifacts`, whose uploaded `artifact_id` is the JPEG SHA-256. Saving does not
activate recognition. Legacy enrollment/gallery mutation APIs are blocked in sync mode.

## Edge Output APIs

| Method/path | Output |
|---|---|
| `GET /healthz` | Liveness |
| `GET /readyz` | Runtime readiness |
| `GET /metrics` | JSON devices, FPS, runtime/reload state and management queue metrics |
| `GET /events` | SSE analytics and heartbeats; not an acknowledged delivery queue |
| `GET /snapshots/<state-relative-ref>` | Available persistent event evidence bytes |
| `GET /api/face_gallery?detail=1` | Active edge gallery information |
| `GET /api/history` | Local history/search statistics |
| `GET /api/search?q=...` | Existing edge-local search, not a multi-edge central search endpoint |
| `GET /api/person?gid=...` | Local person dossier |
| `POST /api/management/commands` | Authenticated command result: applied/rejected/interrupted |

The legacy `/api/enrollment` view is not the authoritative status of a staged
management session; use `enrollment.status` with its session ID. Live staged previews
are not uploaded by this protocol. Existing read APIs need ingress authentication
and network isolation; bearer protection on commands does not secure every endpoint.

## Receiver APIs Implemented By Management

All paths below are relative to `/v1/edges/{edge_id}`. Every request carries
`Authorization: Bearer <token>`. Authorize that token for the edge on the receiver.

| Method/path | Direction and required acknowledgement |
|---|---|
| `GET /commands` | Management returns `{"commands": [...]}` with at most 20 commands |
| `POST /command-results` | Edge sends result; management acknowledges matching `command_id` |
| `PUT /artifacts/{sha256}` | Edge sends JPEG bytes; management verifies/persists then acknowledges `sha256` |
| `POST /records` | Edge sends envelope; management persists then acknowledges matching `record_id` |
| `POST /status` | Edge sends runtime health and pending count; management returns JSON |

Record kinds are `event`, `reid_observation`, and `enrollment_candidate`. ReID
records carry camera/session/track/local identity, optional central identity,
capture coordinates/time, quality, normalized modality vectors, model-space
fingerprints and available source evidence. Face-recognition events can include
central person ID and gallery revision. Do not mix incompatible embedding spaces.

Uploads are at-least-once: deduplicate by `(edge_id, record_id)` and artifact hash.
Unacknowledged records survive restart. Old queued events retain original timestamps
and sessions; they are historical deliveries, not new real-time alerts. Missing event
artifacts are explicit. Management builds and owns the central search index; local
FAISS files are not synchronized as a shared multi-edge index.

## Deployment And Verification

Use `docker/docker-compose.surveillance-v5.yml` with local configs/secrets prepared
according to its mount paths. Authenticate to GHCR before pulling while private.
The example pins the registry digest and enables management sync; it requires a real
receiver configuration, not the example hostname. Hardware access and capacity must
still be checked on each deployment node; advertised stream limits are not a benchmark.

92 host tests and 12 in-image management tests passed. Ch9 HTTPS simulator test:
136 final events, 76 observations, 133 evidence files; all 3 captured outage records
arrived after restart. Gallery persistence/deletion and enrollment controls passed.
Synthetic tests cover candidate chips, upload ordering, commit, gallery reload,
recognition, deletion and future-observation alias propagation.

Not yet proven: live known-person enrollment-save/commit/recognition or cross-edge
identity accuracy. No production central management service is included. Interrupted
session recovery, live enrollment previews, retention/quotas, encryption at rest and
historical biometric deletion still need production work. Gallery deletion alone
does not delete historical records. See `MANAGEMENT_IDENTITY_SYNC.md` for detail.

Schemas/examples: `delivery/apexfabric-v1/intel-285h/surveillance/` contains desired-state,
analytics-event, metrics, management-command and management-record schemas plus
management configuration and polygon counting examples. No test secrets, RTSP recordings,
biometric artifacts or local `run/` output are included in the Git release.
