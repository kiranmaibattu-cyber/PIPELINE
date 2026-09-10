# Surveillance Management Synchronization

Opt-in edge implementation and HTTPS test simulator; no production management
application is included. Existing desired-state camera/app configuration remains
unchanged. This work is now published as September 10 v5; v2 remains unchanged.

## Enable

Mount `management-sync.example.json` as `/configs/management.json`, supply the
token and optional private CA certificate as Secrets, and set
`MANAGEMENT_SYNC_CONFIG=/configs/management.json`. Publicly trusted HTTPS servers
do not require `ca_file`. Certificate verification remains enabled. HTTP is only
accepted with explicit `allow_insecure_loopback: true` for localhost tests.
Redirects are not followed with credentials.

The existing `/state` mount persists:

- `surveillance/face_gallery/managed.json`: authoritative applied face-gallery
  revision, atomically replaced as one document. Recognition swaps to the new
  in-memory gallery without a worker restart. An empty `people` list deletes all.
- `surveillance/management/sync.sqlite`: pending uploads and durable command results.
- `surveillance/management/enrollment/<session_id>`: private staged enrollment,
  separate from the active recognition gallery.
- `surveillance/management/identity-mappings.json`: management aliases scoped by
  camera, stream session and edge-local identity. These do not merge local tracks.
- `surveillance/management/artifacts`: selected ReID source-frame evidence.
- Existing history, rejoin gallery and event snapshots remain in their current paths.

The sync volume is bound to one edge ID. It cannot be reused for another edge
without explicit data migration. Secrets and biometric storage require deployment
access controls; this code does not provide encryption at rest or disk quotas.

## Management Receiver Contract

All requests carry a Bearer token and an edge-specific path `/v1/edges/{edge_id}`.
The receiver must authorize that token for the edge, rather than trusting a body ID.

| Method / suffix | Request / response |
|---|---|
| GET `/commands` | Return `{"commands": [...]}`, at most 20 unacknowledged commands |
| POST `/command-results` | Store durable result; acknowledge `{"command_id": "..."}` |
| PUT `/artifacts/{sha256}` | Raw JPEG; verify and persist bytes before replying `{"sha256": "..."}` |
| POST `/records` | Persist upload envelope; acknowledge `{"record_id": "..."}` |
| POST `/status` | Receive runtime health and pending-record count; reply with JSON |

Inputs and uploads are described by `management-command.schema.json` and
`management-record.schema.json` in the Surveillance delivery folder. Each image
contains the implementation; schemas/examples are integration documents in Git.

One polling worker uses exponential retry (up to 30 seconds), independent of the
camera process. Unacknowledged records survive restart in SQLite. Artifacts are
uploaded before their referencing records. Repeated uploads are expected;
management must deduplicate by `(edge_id, record_id)` and artifact checksum.
Acknowledging a record means both the metadata and referenced artifacts are durable.
Older-session queued events retain original IDs/timestamps and are not fresh alerts.
Missing event artifacts are explicitly listed in `missing_artifact_kinds`.

The existing `/events` SSE API still works independently and is not a delivery
acknowledgement mechanism. Central upload artifacts have `artifact_id` checksums;
edge-relative snapshot URLs are not central URLs. Management must expose its own
artifact retrieval URLs after ingestion. `/metrics` reports `runtime.management_sync`.

## Commands And Enrollment

Commands contain `command_id`, `type`, and `payload`. Results are `applied`,
`rejected`, or `interrupted`. Reusing an ID with different content is rejected.
After a crash during a command, do not blindly replay its side effects: inspect
state and issue a new command ID if needed. Gallery revisions additionally reject
stale or conflicting updates.

1. `gallery.status` returns current revision and exact compatible `embedding_space`.
2. `enrollment.start` takes `session_id`, `person_id`, `camera_id` and starts edge capture.
3. `enrollment.status` returns capture state and coverage. Use a new command ID for each poll.
4. `enrollment.stop` stops capture without committing it. `enrollment.retake` restarts
   after discarding that session's staged samples. `enrollment.cancel` rolls them back.
5. `enrollment.save` requires a frontal sample, persists a candidate and queues an
   `enrollment_candidate` upload with templates and available staged chips. It does
   not activate recognition. `samples[i].template_index` identifies the template;
   its `artifact_key` identifies the chip in `artifacts`. Management retrieves the
   uploaded bytes by `artifact_id`, not by the edge-relative `ref`.
6. Management validates the candidate and sends `gallery.replace` with a higher
   revision and the complete enrolled-gallery snapshot.

Gallery entries use stable `person_id`, editable `display_name`, group, and 1-20
finite 512-dimensional templates. The model XML/BIN fingerprint and preprocessing
contract must match. Updates invalidate cached recognition links. Do not mix body,
face or gait vectors, or different model versions, in a single similarity space.
The current inline command path is limited to 16 MiB per request and at most
1000 people per snapshot. Large-gallery artifact/delta delivery is not implemented.

Authenticated commands are also available through POST `/api/management/commands`
on the public edge API. Use the management token. Legacy local enrollment/gallery
mutation endpoints are blocked while sync is enabled to prevent competing writers.
Other existing runtime APIs still need ingress authentication and network isolation.

## Central ReID Search

The exporter selects at most one observation per track/modality every five seconds.
Records include normalized vectors, model-space fingerprints, quality, source
coordinates and time, camera/session/track/local identity, optional central identity,
and available source-frame evidence. There is no additional learned quality-ranking
policy yet. Enrollment candidates carry vectors, not a live FAISS index file.

Management builds its own modality/model-specific search indexes from these
records. `identity.mappings.replace` sends versioned central aliases for future
observation uploads; it is not an instruction to force a local ReID merge or
rewrite old events. Central indexing, historical merge policy and central search
UI are outside this edge implementation. The simulator performs a compatible-vector
self-match only, not a cross-camera identity accuracy evaluation.

## Limits Before Production

- Active enrollment sessions do not resume automatically across restart. Staged
  files survive, but recovery/import of interrupted sessions needs an operator flow.
- Enrollment still uses the existing capture/model implementation, including its
  existing hardware behavior. Main-camera decode/inference placement is unchanged.
- Per-command receipts and artifact retention need a production retention policy.
  The queue has no disk quota yet; monitor volume usage during extended outages.
- Saved enrollment candidates now include available staged face chips, with
  `samples[].template_index` linking each template to `artifacts` through
  `artifact_key`. Chips are copied to persistent content-addressed JPEG evidence
  before queuing; the uploader sends them before the candidate. A missing chip
  path is explicit as `missing_reason`; invalid or missing referenced files reject
  save. Live enrollment previews remain separate. No automatic deletion of historical biometric observations is implied
  by deleting a face-gallery identity.
- No signed gallery artifacts, command expiration, multi-tenant authorization
  server, or cross-edge conflict resolver is provided by the test simulator.
- Synchronization starts with new records after it is enabled. Existing historical
  galleries, FAISS files and old observation history are not automatically backfilled.
  Token/CA configuration is loaded at startup; credential rotation currently needs
  a container restart.

Tests: `python3 -m unittest discover -s tests -q` and
`python3 scripts/test_management_sync_live.py`. The latter starts a temporary HTTPS
receiver, sends management commands to the actual image, uses ch9, simulates an
outage/restart, validates evidence checksums and preserves results under ignored
`run/exact-evidence-management-*/`. It stops its test container and receiver.

## Verified Candidate: 2026-09-10

Local image: `surveillance-edge-runtime:intel-285h-2026.09.10-v4`.
Image ID: `628948492f39c659cea7ec9b653b68752fe3ad5c32329dda86e9e671c153a7ec`.
It is built locally, not yet archived or published.

Final run: `run/exact-evidence-management-1789039785/`.
At the script's verification checkpoint: 217 events, 102 observations, 315 artifacts.
After enrollment-control testing and shutdown, saved receiver files contained
310 events, 149 observations and 316 artifacts. All 459 final record envelopes
and the metrics payload validated against the schemas. Artifact checksums were
verified by the HTTPS receiver before acknowledgement.

All 11 exact record IDs queued during the simulated outage reached the receiver
after container restart. Gallery revision 1 persisted; revision 2 removed the
synthetic identity. Unauthenticated public commands returned 401; authenticated
commands worked. Versioned identity mapping was acknowledged. The central test
self-query matched a received compatible vector at approximately 1.0 cosine score.

Management started enrollment, observed capture state (2 staged samples), stopped
and cancelled it. The active gallery remained empty. Saving staged candidates,
recognizing synthetic compatible face vectors, rejecting conflicting revisions,
deletion/cache invalidation, and command replay were verified in automated tests,
including 9 tests inside the actual image. The full host suite passed 89 tests.

The final live run exported body embeddings; an earlier integration run also
exported gait embeddings. No usable live face embeddings were exported in these
runs, and known-person recognition/cross-camera matching accuracy is not verified.
There is still no live enrollment-save-to-management-commit-to-recognition proof
with a consenting, known test subject. These limits must not be confused with the
    successful transport and persistence tests. All test containers were stopped.

## Saved Enrollment Evidence Candidate: v5

Local image: `surveillance-edge-runtime:intel-285h-2026.09.10-v5`.
Image ID: `be1f7931fe45e926f09ef13c14a0182c8952151686458f67dd8765808c8039de`.
Built on the unchanged versioned Intel base. Now published to GHCR with registry
digest `sha256:30aac3bad8d953ef7391b6de2f1bcb6487534345b1f189db003d5871ba225680`.
No new Docker archive was generated. See `SURVEILLANCE_V5_DEPLOYMENT.md`.

Saved candidates now carry template-linked JPEG chips in persistent management
artifacts. Save rejects paths escaping the staged gallery and missing/invalid
referenced chips. Templates with no chip path explicitly report that absence.
Candidates over 20 templates are rejected to match the gallery commit contract.
Neither saving nor uploading a candidate activates recognition.

Validation: 92 host tests passed, including 12 management tests also run inside
this image. Synthetic lifecycle coverage now includes chip extraction, artifact
upload before the candidate, management commit, gallery reload, face match and
identity revocation. A separate exporter test verifies central mappings in future
observations and prevents mapping reuse across stream sessions.

Live HTTPS simulator run: `run/exact-evidence-management-1789041791/`, using ch9.
All 3 captured outage record IDs arrived after restart. Gallery persistence and
deletion passed. Enrollment captured 2 staged samples and was stopped/cancelled,
not saved or committed. Final receiver files contain 136 events, 76 observations
and 133 artifacts; all 212 record envelopes and metrics validated. This run
exported body vectors. The test container was stopped and removed.

Live known-person enrollment-save/commit/recognition, recognition accuracy, and
multi-camera/multi-edge identity accuracy remain unverified. Live enrollment
previews, interrupted-session recovery, and retention/quota policies remain
outstanding; the synthetic lifecycle does not establish live recognition accuracy.
