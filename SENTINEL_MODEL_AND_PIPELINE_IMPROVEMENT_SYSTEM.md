# Sentinel Model and Pipeline Improvement System

Status: proposed architecture and phased implementation plan  
Date: 2026-11-03  
Baseline: Sentinel V8 multibox contract. V8 preserves the V7 analytics wire
payloads but moves durable delivery, credentials, sequence allocation, and
Management upload ownership from the CV runtime to a delivery-agent sidecar.

## 1. Purpose

Sentinel needs a controlled way to learn from failures observed on real camera
streams. A missed detection, fragmented track, incorrect identity association,
unreadable plate, false fire alert, or irrelevant scene-search result may expose
a model weakness, but it may also be caused by preprocessing, tracking, policy,
runtime load, evidence association, or Management logic.

The system therefore must not send every suspicious frame directly into model
training. It must first preserve the original evidence and diagnostics, allow
the suspected failure to be reviewed, identify the responsible component, and
only then place an approved example into a task-specific dataset.

The target lifecycle is:

```text
Edge inference
      -> suspected failure or reported mistake
      -> improvement candidate and diagnostic evidence
      -> Management review and root-cause classification
      -> task-specific labelled dataset
      -> model training or engineering fix
      -> evaluation and approval
      -> controlled model/runtime deployment
```

The first deliverable is not automatic training. It is a reliable path from a
real camera observation to a reviewed, reusable improvement example.

## 2. Scope and Boundaries

### 2.1 Existing V8 production path

The V8 runtime remains the production observation path:

```text
RTSP/file source
  -> hardware decode
  -> motion gate and frame selection
  -> detection and tracking
  -> use-case pipelines
  -> one complete Observation/Evidence/Embedding package
  -> shared bounded CV handoff scheduler
  -> HTTP over delivery-agent Unix socket
  -> delivery-agent durable per-deployment outbox and sequence allocation
  -> /api/v7/ingest/*
  -> Management
```

This path continues to produce security and operational observations. The new
improvement system must not weaken its gates, delay its outputs, or consume its
event space. The CV runtime has no direct access to the delivery SQLite database,
the `/state` PVC, box/deployment credentials, or Management ingest endpoints.

### 2.2 New improvement path

The improvement path is independent:

```text
Shared decoded frames and model observations
  -> diagnostic checks
  -> suspected candidate trigger
  -> bounded evidence capture
  -> shared bounded CV handoff scheduler
  -> delivery-agent improvement handoff
  -> delivery-agent-owned durable candidate storage
  -> Management improvement API
  -> review, labelling, and dataset export
```

An improvement candidate is not a business Event, incident, or confirmed model
failure. It is a package that may be useful for investigation. It must not be
inserted into the ordered V7 production observation stream merely to reuse the
existing Event API.

### 2.3 V8 multibox topology

One Kubernetes deployment pod represents one immutable `deployment_id` and
contains one CV container and one delivery-agent sidecar. They share only the
Unix-socket `emptyDir`; only the delivery agent mounts and writes the
deployment-specific `/state` PVC.

```text
Physical box
  +-- deployment pod A
  |     +-- CV runtime A
  |     +-- delivery agent A
  |     +-- PVC A
  |
  +-- deployment pod B
        +-- CV runtime B
        +-- delivery agent B
        +-- PVC B
```

Multiple deployments on one box therefore have independent candidate queues,
quotas, durable state, placement generations, and credentials. Candidate
identity is anchored by `deployment_id`, `camera_id`, `stream_session_id`, and
`candidate_id`. A physical `box_id` and `placement_generation`, when required
for audit, are attached or verified by the delivery agent because the CV
runtime is not the authority for box credentials.

### 2.3 Responsibilities

| Component | Responsibilities |
| --- | --- |
| CV runtime | Detect technical uncertainty, retain a bounded in-memory frame history, record exact model/runtime context, create complete candidate packages, and retry exact handoff bytes until the delivery agent durably accepts or definitively rejects them |
| Delivery-agent sidecar | Validate and durably persist candidate packages, own local state and credentials, enforce candidate quotas and delivery priority, resume remote upload after restart, and report delivery health |
| Management | Receive candidates, create operator-reported candidates, associate existing events/evidence/identities, support review and root-cause classification, and organize approved examples |
| Training pipeline | Validate labels, version datasets, train or fine-tune a selected model, evaluate candidates, convert approved models, and publish immutable artifacts |
| Release process | Run compatibility checks, shadow/canary validation, promotion, monitoring, and rollback |

Training does not run on the production edge. The CV container remains an
inference and evidence-selection system; the delivery agent remains the only
durable local delivery owner.

## 3. Core Design Principles

1. Preserve uncertainty. Edge-generated candidates are `suspected`, not
   confirmed failures.
2. Preserve original pixels. The original decoded frame is the primary visual
   evidence; crops and model inputs are supplemental artifacts.
3. Record exact provenance. Every candidate identifies camera, time, runtime,
   policy, preprocessing, and model versions.
4. Separate diagnosis from training. A candidate can lead to model training,
   tracker changes, policy changes, runtime fixes, Management fixes, or no
   action.
5. Keep production delivery first. Candidate capture and upload must not delay
   Observations, required evidence, embeddings, camera processing, or health
   output. The shared CV scheduler and delivery agent must prioritize
   non-replaceable production facts over improvement work.
6. Collect selectively. Use bounded buffers, trigger-based capture,
   deduplication, quotas, and retention limits instead of recording every frame.
7. Require trustworthy labels. Existing model predictions can pre-label data,
   but they do not automatically become ground truth.
8. Extend through typed diagnostics. Use one common candidate envelope and a
   model-specific diagnostic payload rather than separate upload systems.
9. Promote conservatively. Completing a training job never authorizes automatic
   replacement of all production models.
10. Respect V8 ownership. The CV process does not create a second disk outbox,
    allocate production sequences, mount `/state`, hold delivery credentials,
    or call Management directly.

## 4. Candidate Sources

### 4.1 Edge-generated uncertainty

The edge can detect technical patterns that warrant review, including:

- A confirmed track disappears and a plausible new track appears shortly after.
- Detector candidates repeatedly fall below the collection threshold.
- A long person presence never obtains a usable face sample.
- Head-pose estimates are unstable or consistently reject otherwise usable
  faces.
- ANPR candidate readings disagree or expire without a reliable result.
- Fire/smoke confidence repeatedly crosses confirmation thresholds.
- A motion-gate probe finds an object while the gate was sleeping.
- A valid moving person repeatedly fails gait-sequence construction.

These triggers locate potentially useful evidence. They cannot determine ground
truth by themselves.

### 4.2 Management and operator reports

Some failures are visible only after local observations have reached
Management:

- One person is split into multiple cross-camera identities.
- Different people are incorrectly merged.
- A plate string is incorrect even though the crop is readable.
- A visible subject was never represented by an Event.
- A fire/smoke result is a false alarm or a real hazard was missed.
- A scene-search result is irrelevant or a known relevant scene is absent.
- Evidence is attached to the wrong subject or Event.
- Dwell, count, zone, or timestamp behavior is incorrect.

Management must be able to create a candidate from existing Event, Evidence,
Embedding, presence, identity, incident, and search-result records without
requiring the edge to recreate data that Management already holds.

## 5. Common Candidate Contract

The improvement contract is an additive companion to V8. V8 describes
production desired state, analytics payloads, CV-to-delivery-agent packages,
durability, sequence allocation, and Management ingest. The improvement
contract describes diagnostic packages and their review lifecycle.

V8 currently defines only production packages containing one observation plus
its evidence and embeddings. It defines no training-candidate payload or remote
candidate route. Therefore, implementing this design requires an explicit V8
delivery-agent extension; a candidate must not be disguised as an
`object_present`, `camera_health`, or another production observation.

The proposed contract family is `sentry-improvement-v1` and contains:

- `improvement-candidate.schema.json`
- `improvement-artifact.schema.json`
- `candidate-completion.schema.json`
- `candidate-review.schema.json`
- `candidate-resolution.schema.json`
- A registry of allowed candidate and diagnostic types

### 5.1 Common envelope

```json
{
  "schema_version": "1.0",
  "candidate_id": "tc-019...",
  "candidate_type": "suspected_track_fragmentation",
  "source": "edge_uncertainty",
  "certainty": "suspected",
  "status": "captured",

  "deployment_id": "test-1",
  "camera_id": "blr-cam-04",
  "stream_session_id": "ss-...",
  "observed_from": "2026-10-27T10:30:14.000Z",
  "observed_to": "2026-10-27T10:30:20.000Z",
  "timestamp_source": "edge_received_at",

  "runtime": {
    "contract_version": "8.0",
    "wire_schema_version": "7.0",
    "runtime_version": "v8-development",
    "policy_version": "...",
    "processed_fps": 5.0
  },

  "models": [
    {
      "role": "person_vehicle_detector",
      "profile_id": "...",
      "version": "...",
      "artifact_sha256": "...",
      "device": "GPU"
    }
  ],

  "related_event_ids": [],
  "related_evidence_ids": [],
  "related_presence_ids": ["P1", "P2"],
  "related_identity_ids": [],

  "diagnostics": {
    "type": "tracking_fragmentation",
    "payload": {}
  },

  "artifacts": [],
  "review": null
}
```

The final schemas must reject undeclared fields and validate the diagnostic
payload against its registered type. A diagnostic payload may expose internal
values that are intentionally absent from normal production Events, such as
rejected detections, tracker association costs, SixDRepNet angles, or individual
OCR candidates.

The CV-produced manifest does not claim an authoritative `box_id` or placement
generation. The delivery agent records the active box and placement provenance
at durable acceptance or remote delivery. This prevents stale or moved CV pods
from inventing multibox ownership.

### 5.2 Candidate states

```text
captured
  -> upload_pending
  -> uploading
  -> ready_for_review
  -> reviewed
  -> annotation_pending
  -> annotated
  -> approved_for_dataset
  -> included_in_dataset

Terminal alternatives:
  not_a_failure
  unusable
  duplicate
  engineering_fix
  policy_fix
```

The candidate record and each artifact have independent remote-delivery states
so a manifest acknowledgement cannot cause undelivered evidence to be
discarded. From the CV runtime's perspective, a candidate is produced only
after the delivery agent returns a durable or exact-duplicate acknowledgement.
Before that acknowledgement it remains an in-memory, in-flight package and may
be lost if the CV process crashes, matching V8 producer crash semantics.

## 6. Evidence and Diagnostic Capture

### 6.1 Per-camera rolling buffer

The CV runtime maintains a bounded in-memory buffer of references to recently
decoded frames and compact frame-level diagnostics. It does not create another
decoder, inference pipeline, disk outbox, or PVC writer.

Initial values for a camera processed at 5 FPS:

| Setting | Initial value |
| --- | ---: |
| Pre-trigger window | 3 seconds |
| Post-trigger window | 3 seconds |
| Maximum complete window | 10 seconds |
| Maximum selected frames | 50 |
| Primary pixels | Original decoded frame |

The limits must later be made resource-aware. JPEG encoding and package
construction occur only after a trigger selects a candidate. Durable writes
occur only inside the delivery agent after it validates and accepts the complete
package.

### 6.2 Frame-level diagnostics

For every selected frame, retain where applicable:

- `source_frame_id`, `observed_at`, and timestamp source.
- Whether decode succeeded.
- Whether motion gating allowed normal inference.
- Whether a periodic probe ran inference.
- Whether the frame was processed, skipped, or dropped.
- Detector input shape and resize/letterbox transform.
- Raw detector candidates needed for diagnosis, including rejected candidates.
- Acceptance/rejection decision and reason.
- Tracker observations, predictions, assignments, and ambiguity.
- Model queue delay and relevant pressure indicators.

Predicted tracker boxes must be marked as predictions and must never be recorded
as real detections.

### 6.3 Artifact types

Supported artifacts include:

- Original full frame.
- Original-pixel subject, vehicle, face, or plate crop.
- Exact model input when preprocessing itself may be responsible.
- Silhouette or segmentation mask.
- Short preview clip.
- Human-viewable overlay preview.
- Structured frame observations.
- Embedding references where permitted.

Overlay images are for review only. Training uses clean original frames and
separate annotations.

## 7. Candidate Coverage by Model and Pipeline

| Area | Candidate examples | Required evidence or feedback | Possible resolution |
| --- | --- | --- | --- |
| Person/vehicle detection | Missed object, false detection, wrong class, unstable box | Original frames, detector inputs and candidates, transforms, reviewed boxes/classes | Detector dataset or preprocessing fix |
| Tracking/presence | Fragmentation, ID switch, false recovery, premature end | Consecutive frames, detections, predictions, association data, old/new IDs | Tracker evaluation, association logic, detector improvement |
| SCRFD face detection | Missed face, false face, wrong person ownership | Full frame, person crop, face input, boxes, landmarks | Face detector or ownership logic |
| SixDRepNet | Frontal face rejected, side face accepted, unstable pose | Original face crop, exact input, pitch/yaw/roll, pose score, decision | Pose calibration, crop fix, or model evaluation |
| AdaFace | Same person not matched, different people merged | Evidence crop, aligned chip, embedding reference, quality, human same/different label | Crop/association fix, Management threshold, or face model dataset |
| TransReID | Cross-camera miss/merge, contaminated body crop | Original body crops, embeddings, camera/presence links, identity label | Re-ID evaluation/training or crop/association fix |
| GaitBase | Invalid sequence, contaminated silhouette, identity mismatch | Ordered frames, masks, motion state, sequence quality, identity label | Segmentation/sequence fix or gait dataset |
| Plate detector | Plate missed, false plate, cut-off box | Full frame, vehicle crop, detector input, reviewed plate box | Plate detector dataset |
| Plate OCR | Conflicting or incorrect text, readable marked unreadable | Original plate crop, exact OCR input, all reads, corrected text | OCR dataset or selection-policy fix |
| Fire/smoke | False alert, missed hazard, unstable confidence | Consecutive frames, boxes/masks, confidence history, human hazard label | Hazard model dataset or temporal policy fix |
| SigLIP2 scene retrieval | Irrelevant result, missed relevant frame, action-query mismatch | Query, frame, embedding profile, score/rank, relevance feedback | Retrieval tuning, reranking, or temporal-model requirement |
| Motion gate | False sleep, continuous false wake, slow-motion miss | Comparison frames, masks, scores, state changes, periodic-probe result | Motion algorithm/profile adjustment |
| Use-case logic | Wrong count, dwell, zone, timestamp, association, or evidence link | Events, frames, presence history, geometry, delivery records | Engineering or policy fix, not model training |

### 7.1 Identity-specific limitation

Face, body, and gait embedding failures usually cannot be confirmed by the edge.
They require same-person/different-person labels from Management or a reviewer.
Separate identity records do not by themselves prove that AdaFace, TransReID,
or GaitBase requires retraining; the cause may be crop quality, face ownership,
local presence fragmentation, similarity thresholds, or clustering logic.

### 7.2 Scene-search limitation

SigLIP2 produces single-frame semantic retrieval features. A failure on a query
that requires temporal action understanding may indicate that a clip/action
model is required rather than that the image encoder should be retrained.

## 8. First Edge Candidate: Suspected Track Fragmentation

The first executable candidate producer is
`suspected_track_fragmentation`. The common infrastructure remains generic.

### 8.1 Trigger

1. A confirmed presence has recent real detections.
2. Real detections stop without a positive exit.
3. Within a bounded time, a new track of the same compatible class appears in a
   plausible spatial and motion region.
4. Recovery does not confidently retain the original presence.
5. The pair is sufficiently plausible to justify review but not sufficiently
   certain for automatic merging.

The trigger freezes pre-gap, gap, and post-reappearance evidence. It must not
change the old or new `presence_id`.

### 8.2 Diagnostic outcome

The candidate must allow a reviewer to distinguish:

```text
No detector candidate
  -> possible detection-model miss

Low-confidence candidate rejected
  -> collection-gate or confidence-calibration issue

Accepted detection not assigned to the old track
  -> tracker/association issue

Inference skipped or frame dropped
  -> motion, scheduling, queue, or runtime issue

Subject not visibly observable
  -> occlusion or expected scene limitation

New track belongs to another subject
  -> not a fragmentation failure
```

### 8.3 Review labels

- Same subject and track fragmented.
- Different subjects and separation was correct.
- Missed detector observation.
- Low-confidence observation rejected.
- Tracker association failure.
- Occlusion.
- Inference skipped.
- Frame dropped or decode unavailable.
- Insufficient evidence.
- Not a failure.

## 9. V8 Handoff and Management Delivery

The improvement service uses a channel separate from production Observation,
Evidence, and Embedding endpoints. It has two boundaries: CV to delivery agent,
then delivery agent to Management.

### 9.1 Proposed CV-to-delivery-agent extension

V8 currently exposes `POST /v1/packages` for production packages. The proposed
additive local endpoint is:

```text
POST /v1/improvement-packages
Content-Type: multipart/form-data

manifest:
  common candidate envelope plus artifact descriptors

artifact parts:
  exact bytes declared by the manifest
```

The endpoint name is provisional, but its ownership is not: the CV runtime
hands the package to the delivery agent over
`/run/sentinel-delivery/agent.sock`; it does not call Management.

Local handoff behavior should mirror V8 production durability:

- `candidate_id` is a UUID and the local idempotency key.
- The CV scheduler retries the exact candidate ID, manifest, and artifact bytes.
- First durable acceptance returns `201 durable` only after database commit,
  artifact fsync, and directory fsync.
- An exact replay returns `200 duplicate`.
- Reusing a candidate ID with different bytes returns non-retryable `409`.
- Capacity or temporary durability failures return bounded retryable errors.
- The delivery agent validates artifact part names, byte sizes, SHA-256 values,
  ownership references, and diagnostic schema before persistence.
- The CV runtime releases package memory only after durable/duplicate
  acknowledgement or a final failed-handoff decision.

The first implementation should keep a candidate package within V8's existing
64 MiB local request ceiling. If a future sequence requires more data, the
contract must add explicit bounded chunking; it must not silently raise limits
or write temporary candidate files from the CV container.

### 9.2 Delivery-agent candidate storage

The delivery agent owns the improvement queue and artifacts on its
deployment-specific PVC. Candidate state may use separate tables or a separate
database owned by the same agent, but never a second writer.

Improvement records do not receive V7 production stream sequences and do not
advance or block `highest_contiguous_sequence`. They use their own candidate ID
namespace and delivery state. The agent attaches authoritative multibox
provenance such as current `box_id` and placement generation without exposing
credentials to the CV runtime.

Candidate storage and scheduling require separate limits:

- Production records and required evidence have reserved capacity.
- Improvement packages are lower priority and are rejected or evicted according
  to an explicit candidate-retention policy before production records.
- Candidate uploads cannot consume all simultaneous remote upload workers.
- There is one deployment-wide candidate scheduler, not per-camera retry queues.
- Improvement pressure is visible in delivery-agent status without being mixed
  with camera health.

### 9.3 Delivery-agent-to-Management API

A practical remote transaction is:

```text
POST /api/v1/improvement/candidates
  -> validate manifest and reserve candidate ID
  -> return upload session and artifact destinations

PUT <artifact upload destination>
  -> upload original evidence or diagnostic object

POST /api/v1/improvement/candidates/{candidate_id}/complete
  -> verify required artifacts, sizes, and SHA-256 values
  -> mark ready_for_review
```

These routes are proposed additions and are not part of the current V8
contract. They may use the delivery agent's existing authenticated Management
connection, but they remain logically separate from `/api/v7/ingest/*`.

Management-created candidates can use server-side operations such as:

```text
POST /api/v1/improvement/candidates/from-observation
POST /api/v1/improvement/candidates/from-identity
POST /api/v1/improvement/candidates/from-search-result
```

The exact remote endpoint names remain subject to Management API conventions.

### 9.4 Delivery guarantees

- Candidate and all local artifacts are accepted as one complete durable agent
  package before the CV runtime calls the candidate produced.
- Remote retries reuse identical IDs and bytes.
- Every artifact includes content type, byte size, and SHA-256.
- Remote manifest and artifact acknowledgements are tracked independently.
- Delivery-agent restart resumes partial remote delivery without creating a
  duplicate candidate.
- CV restart after durable handoff does not lose the package; CV failure before
  durable acknowledgement may lose the in-memory package and allocates no
  production sequence.
- Production Observation and required-evidence delivery always has higher
  priority.
- Backpressure, rejected capture, quota exhaustion, eviction, and upload delay
  are reported as delivery-agent telemetry.

## 10. Management Catalog and Review

The catalog should support filtering by:

- Candidate type and source.
- Camera, deployment, and time range.
- Object class.
- Runtime, policy, and model version.
- Presence, identity, Event, and Evidence identifiers.
- Confidence, gap duration, quality, and review status.
- Root cause and selected resolution.

The initial review screen should show:

- Before, during, and after frames in timestamp order.
- Clean frame and optional diagnostic overlay.
- Raw and accepted model observations.
- Track/presence transitions.
- Applied gates and rejection reasons.
- Model/runtime provenance.
- Related production records.
- Review labels appropriate to the candidate type.

Root-cause classes should include:

```text
model_detection
model_embedding
model_ocr
model_hazard
model_retrieval
preprocessing
tracking_association
crop_or_subject_association
policy_or_gate
motion_or_scheduling
decode_or_runtime
management_matching
delivery
expected_scene_limitation
not_a_failure
unknown
```

Resolution actions should include:

```text
include_in_training_dataset
include_in_evaluation_dataset
engineering_fix
policy_adjustment
management_fix
collect_more_evidence
no_action
```

## 11. Deduplication, Retention, and Safety

Without controls, one difficult camera can fill storage with equivalent
candidates. The first implementation should enforce:

- At most one fragmentation candidate for the same presence episode and
  candidate window.
- Coalescing of old/new track evidence into one candidate.
- Per-camera and per-candidate-type cooldowns.
- Daily candidate-count and byte quotas.
- Management-side visual-similarity and metadata deduplication.
- Priority for underrepresented camera conditions and failure types.
- Configurable local and Management retention.
- Access control and audit records for face, identity, and plate data.
- Encryption in transit and approved storage controls.
- No camera credentials or secret URLs in candidate metadata.

Dropping a low-priority unaccepted candidate under declared storage pressure is
permitted. Silently dropping an accepted candidate or an artifact promised by
its manifest is not permitted.

## 12. Dataset Creation

Evidence collection, candidate review, and dataset creation are separate stages.

| Stage | Output |
| --- | --- |
| Evidence collection | Original artifacts, model observations, provenance, and diagnostics |
| Candidate review | Verified description of what happened and probable root cause |
| Annotation | Task-specific ground truth such as boxes, text, identity relations, masks, or relevance |
| Dataset build | Validated, deduplicated, versioned examples with train/validation/test assignments |

Task-specific exports include:

- Detection: original image, object class, and reviewed bounding boxes.
- Face detection: image, face box, landmarks, visibility, and pose category.
- OCR: plate crop, exact text or unreadable label, and crop validity.
- Face/body Re-ID: sample pairs or groups labelled same, different, or uncertain.
- Gait: ordered sequence, masks, quality, and identity relationship.
- Fire/smoke: temporal frames and reviewed hazard/negative class.
- Scene retrieval: query/frame pair and relevance label.
- Tracking evaluation: consecutive frames with consistent object identities.

Dataset rules:

- Preserve source candidate IDs for traceability.
- Remove duplicates before splitting.
- Prevent frames from one continuous episode from being split across training
  and evaluation sets.
- Prefer camera-aware and time-aware splits to expose generalization failures.
- Keep a fixed regression test set that is never used for training.
- Record annotation policy, reviewer, approval, and dataset version.

## 13. Training, Evaluation, and Release

Training is model-specific. A generic orchestration service may launch jobs, but
each model requires its own labels, metrics, preprocessing, and acceptance
criteria.

No candidate model is promoted based only on training loss. Evaluation should
include:

- Detector precision, recall, class behavior, object-size behavior, and temporal
  consistency.
- Tracking fragmentation, ID switches, recovery precision, and false merges.
- Exact plate accuracy, unreadable accuracy, and false-read rate.
- Re-ID same/different separation, false merges, and cross-camera behavior.
- Hazard precision, recall, temporal stability, and hard-negative behavior.
- Retrieval relevance by query category and rank.
- Runtime latency, throughput, memory, queue pressure, and GPU/NPU compatibility.
- Observation, evidence, embedding, timestamp, and delivery regression tests.

Release flow:

```text
trained
  -> evaluated
  -> approved candidate
  -> OpenVINO conversion and device validation
  -> immutable model registry artifact
  -> recorded-video comparison
  -> shadow deployment
  -> limited canary cameras
  -> broader promotion or rollback
```

The current safe deployment mechanism can continue baking an approved model into
an immutable runtime image. A future signed model-bundle mechanism can reduce
full-image rebuilds, but it requires model compatibility checks, checksums,
atomic activation, and rollback before adoption.

## 14. Phased Implementation Plan

### Phase 1: Contract and registry

- Finalize the common candidate, artifact, completion, review, and resolution
  schemas.
- Define the candidate-type registry and diagnostic payload validation.
- Define the additive V8 CV-to-delivery-agent improvement-package endpoint,
  durable acknowledgement, conflict, backpressure, and status schemas.
- Agree remote API acknowledgement and idempotency behavior with Management.
- Define access, retention, and quota policy.

### Phase 2: Edge collection foundation

- Add the shared per-camera rolling buffer.
- Record frame-level inference, detection, tracking, motion, and queue metadata.
- Add candidate evidence selection and complete in-memory package construction.
- Extend the one deployment-wide CV handoff scheduler with a lower-priority,
  bounded improvement-work tier.
- Extend the delivery agent with the only durable improvement outbox, candidate
  quota, remote uploader, and delivery-health metrics.

### Phase 3: First end-to-end candidate

- Implement `suspected_track_fragmentation`.
- Hand the candidate manifest and artifacts to the V8 delivery agent.
- Verify exact-byte retry, durable/duplicate acknowledgement, conflict handling,
  CV restart after durable handoff, and delivery-agent restart during remote
  upload.
- Confirm there is no effect on production FPS or delivery priority.

### Phase 4: Management catalog and review

- Store candidate metadata and artifacts.
- Display frame sequence and diagnostics.
- Add fragmentation review labels and root-cause selection.
- Support candidate creation from existing Management observations.

### Phase 5: First dataset export

- Export reviewed tracking sequences as an evaluation dataset.
- Allow missed detections to receive reviewed boxes and enter a detector dataset.
- Establish deduplication, versioning, and fixed validation/test splits.

### Phase 6: Additional candidate producers

Add model-specific producers and review forms in this order:

1. Person/vehicle detection.
2. Plate detection and OCR.
3. Face detection, head pose, and identity feedback.
4. Body Re-ID and gait.
5. Fire/smoke.
6. Scene retrieval.
7. Motion gate and use-case/runtime diagnostics.

### Phase 7: Automated training and controlled release

- Add reproducible training jobs.
- Add evaluation reports and acceptance gates.
- Add model registry and immutable model provenance.
- Add shadow/canary deployment, monitoring, promotion, and rollback.

## 15. First Milestone Acceptance Criteria

The initial milestone is complete when a real or replayed fragmented track can
follow this path:

1. The CV runtime identifies a suspected fragmentation without altering production
   presence IDs.
2. The CV runtime captures clean original frames before, during, and after the gap.
3. The package records raw detections, acceptance decisions, tracker state,
   timestamps, motion state, model versions, and runtime version.
4. The CV runtime hands the exact complete package to the delivery agent without
   writing `/state`, allocating a sequence, holding credentials, or contacting
   Management.
5. Once the delivery agent durably acknowledges the package, candidate and
   artifacts survive CV and delivery-agent restarts and resume remote upload.
6. The candidate consumes no V7 production sequence and cannot block a required
   Observation/Evidence/Embedding package.
7. Management receives the package idempotently and verifies every artifact.
8. A reviewer can identify whether the cause was detection, tracking, occlusion,
   skipped inference, runtime behavior, a different subject, or uncertainty.
9. An approved missed-detection example can be exported with a reviewed class
   and bounding box.
10. Candidate processing does not reduce required production delivery or cause a
   measurable unapproved inference regression.

## 16. Decisions and Open Items

Agreed direction:

- Use a Model and Pipeline Improvement Catalog, not a tracking-only catalog.
- Build the improvement contract as an additive companion to V8 without
  changing V7 analytics payload semantics.
- Preserve V8 ownership: CV produces and hands off; the delivery agent owns
  durability, credentials, placement provenance, and remote upload.
- Keep candidates out of the permanent ordered production sequence.
- Support edge-generated uncertainty and Management-generated reports.
- Preserve original frames and attach model-specific artifacts and diagnostics.
- Begin with track fragmentation while keeping the infrastructure generic.
- Require review and root-cause classification before dataset inclusion.
- Keep training and promotion outside the production edge runtime.

Items to finalize before implementation:

- Local V8 improvement endpoint, package, acknowledgement, status, and error
  schema names.
- Management endpoint naming; authentication remains delivery-agent-owned.
- Object-storage mechanism and signed-upload behavior.
- Per-camera CV memory limits and per-deployment delivery-agent candidate disk,
  count, byte, and remote-worker limits.
- Candidate retention and privacy rules for faces, identities, and plates.
- Exact fragmentation trigger and ambiguity thresholds.
- Whether preview clips are required in the first release or generated by
  Management from JPEG sequences.
- Annotation tool integration versus an initial built-in review interface.
- Dataset registry and training-orchestrator technology.

These open items affect implementation details but do not change the core
architecture.
