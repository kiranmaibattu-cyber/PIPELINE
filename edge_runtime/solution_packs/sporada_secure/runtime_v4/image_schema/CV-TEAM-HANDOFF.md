# Sentinel v4 CV team handoff — DRAFT FOR REVIEW

Start with [README.md](README.md), then [image-contract.yaml](image-contract.yaml) and [EVENT-VOCABULARY.md](EVENT-VOCABULARY.md). The JSON Schemas in this directory are the draft machine-readable payload definitions. This `4.0-draft.1` package is **ready for CV-team review and implementation**. Do not claim that a particular CV image is v4-conformant until it passes the conformance checklist and is explicitly approved and cataloged.

## What the image must declare and do

- Declare `contract: v4` in its catalog entry. The control plane must bind its bearer token to that deployment, contract version, and camera allowlist. The image sends only v4 payloads to `/api/v4/ingest/...`; it does not mix v2 and v4 on one deployment.
- Read revisioned desired state from `/configs/desired_state.json`; use only management-issued camera, zone/line, threshold, deployment and embedding-profile IDs. Keep camera credentials in mounted secret files, never in desired state or events.
- Emit one typed Event per recorded observation. `event_type` selects a closed branch in `observation.schema.json`; Guard Rules must not depend on arbitrary `attributes`. `object_present` requires a session/track and normalized source-frame bounding box. A lost track is **not** a `zone_exit`.
- Persist Event and binary evidence into `/state` before first send. Send and acknowledge the Event, then evidence, then any embedding. Retries keep the same IDs and bytes. Evidence references may show pending until the evidence upload succeeds.
- Fill `models.embedding_profiles.declarations` for the concrete image using `embedding-profile.schema.json`; each embedding names a declared `profile_id` and has `dim == len(vector)`. The management database currently seeds AdaFace IR101/512, TransReID SSL/384, GaitBase/4096, and SigLIP2/768 with physical tables. The first three `v18.1` model-version labels are provisional until CV supplies exact weight revisions; the SigLIP2 revision is pinned. Model swaps require new profile IDs/tables, never a silent in-place rename. Empty declarations are valid only if the image emits no embeddings.
- Named recognition is face-only after management review. Body and gait similarity are candidate-association evidence, never a confirmed identity. `presence_id` may bridge a short CV-side track break within the same camera stream session, but is not a person ID.
- V18.1 counts are `visible_now` by default, not persistent occupancy. `scene_sample` may be emitted without a vector but needs an evidence reference; any `embedding_id` refers to a separate, ordered scene embedding submission.
- Handle HTTP failures as specified by `delivery.non2xxNon409`: retry transient failures; pause and retain on auth failures; dead-letter permanent validation/route errors and 409 conflicts visibly. Never silently discard a failed submission or send dependents before their parent acknowledgement.

## Review decisions needed

1. Can the tracker emit a **positive** zone exit, and what does it do when tracking is lost or a camera becomes unavailable?
2. Will CV emit `threshold_exceeded` telemetry, or should management derive all threshold crossings from count/dwell measurements?
3. What is the exact ordered-line side convention and how are `forward`/`reverse` assigned?
4. What are the exact model artifact revisions/hashes for AdaFace, TransReID and GaitBase, and will this image emit all four seeded profiles?
5. Expected per-camera rates, clock skew, evidence capture-to-observation timing, and maximum outbox size during network outages.

Violence detection and frequent-visitor identification/alerting are excluded from this draft. Embedding transport remains because it serves continuous enrollment and candidate association; it does not itself define a frequent-visitor rule. V2/v3 artifacts are historical references only; a v4 image must use only the v4 desired state, schemas, and ingest routes in this package.

The JSON Schemas enforce shapes, enums, required fields, and numeric ranges. Ingest must also check relations that ordinary JSON Schema cannot express: `x1 < x2` and `y1 < y2`; `as_of`, `crossed_at` or `transition_at` equals `observed_at`; dwell start/end ordering; threshold `over`/`under` versus the bound; declared profile equality and vector length; uniqueness of desired-state IDs and valid zone/threshold/profile references; exact agreement between each camera ID and its secret-file name; ID membership in the token-bound desired state and its revision; evidence byte count/hash, evidence role/type compatibility, and clip-time ordering. These are required v4 validations, not optional UI checks.

## Differences from v2

| Area | v2 | Draft v4 / reason |
|---|---|---|
| Events | Broad observation with optional, free-form `event_type`/`attributes` | Closed typed events so rule inputs have explicit semantics. |
| Geometry and identity | Optional bbox/session/track | Required normalized bbox for baseline detection and required session-scoped tracks for subject events. |
| Configuration | Polygon zones, fixed outputs | Revisioned polygons, lines, app selection and thresholds; event IDs map to desired state. |
| Counts and health | Not explicit | Typed as-of counts, coverage, camera health, and positive transitions prevent false zero/departure. |
| Embeddings | Fixed 512 face/body, 1024 scene | Seeded per-image declarations backed by physical 512/384/4096/768 tables. |
| Ingest | `/api/v1/ingest/...` | `/api/v4/ingest/...` and contract-bound token so versions do not collide. |
| Errors | Retry list and 409 dead-letter | Also explicit auth pause and permanent 4xx handling. |
| Scene output/live SSE | Separate v2 schemas | Not required by this v4 draft; durable submissions are authoritative. |
