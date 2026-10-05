# Sentry V5.2 boundary-contract draft

This is a proposed, V5-based contract package. It does not modify the original
`newdetails/sentry-v5-*` files, the V5.1 experiment, a runtime image, or
Management ingest. `/api/v5` routes, V5 app names, flat Events, source-frame
geometry, four embedding profiles, health/readiness endpoints, and Event -> Evidence -> Embedding delivery
remain the base. The payload version is `5.2` because the wire fields change.

## Management to edge

Management sends a desired-state revision listing cameras, reusable
polygons/lines, and V5-app bindings with stable `use_case_id`s. There is no
separate `apps` list to keep in sync with those bindings. One polygon
can serve many bindings; one binding can use several polygons. A `full_frame`
binding uses no geometry and need not supply a `geometry` object. Counts, ANPR, person identity, scene search, and
fire/smoke may run full-frame. A line is required for line-crossing apps.
Zone entry/exit and zone-based dwell require a polygon. Internal gates such as
minimum track age, face size, OCR consensus, reassociation thresholds, and
queue length are not Management inputs.

`embedding_profile_ids` means which modalities to attempt, not which results
must exist. If no usable face is visible, a qualifying body vector may still
be sent for the same `presence_id`. Gait is independent and sequence-dependent.
An absent modality produces no fake or zero vector. Only a qualified face may
support named recognition; body and gait are candidate-association evidence.
The V5 semantic profile remains optional on `vehicle_presence`,
`person_presence`, and `person_identity` for selected subject crops; it is not
limited to periodic scene frames. Those crop vectors are attached to the
selected `object_present` and its `subject_crop`, not sent per detection.
Semantic vectors have no synthetic quality score; face, body, and gait samples
carry their own selected-sample quality.

## Edge to Management

The original V5 Event names remain. The added names are `presence_recovered`,
`presence_ended_unknown`, `plate_outcome`, and `fire_smoke_cleared`.
Management-to-edge recheck and `recheck_id` are deferred. Subject Events
require camera-local `presence_id` and stream
session. A zone episode carries `zone_episode_id`, first positive in-zone time,
latest confirmed in-zone time, and observation state. A first sighting already
inside a zone has `entry_observed=false` and does not invent `zone_entry`.
Track loss is unknown, not a zone exit or completed dwell. Positive reobservation
emits `presence_recovered` even if the tracker retained the same track ID; a
new track ID requires qualified local reassociation. A line crossing is
a separate fact; this draft does not claim that crossing a line proves exit
from a polygon. When several bindings apply to one subject, `object_present`
is produced once for that confirmed presence using one owner `use_case_id`.
At first confirmation, eligible presence bindings take priority over identity
or ANPR bindings; ties use the smallest `use_case_id` in lexical order. That
owner stays fixed while enabled, even if the subject later moves between
zones. Its ID identifies the original event owner, not current zone membership.
If a subject is first confirmed outside every bound polygon, no owner is
assigned until its first confirmed observation inside an eligible binding.
If a revision disables it, the edge selects a new owner without another
`first_seen`; the same `presence_id` continues.
Selected face, body, or gait outputs use `person_feature_sample` Events with
their own `person_identity` use-case ID, evidence, and embedding. They do not
create another `object_present`.

`vehicle_presence` and `person_presence` supply the timestamps and episode
status from which Management calculates dwell. There is no separate dwell app
or milestone Event in this package. Management rules set dwell thresholds.
A freshness update must preserve the same presence and episode IDs. When
unobserved, Management cannot assume continuous dwell through that gap.

`zone_count` is an as-of measurement of currently visible subjects, not a
cumulative entry total. A healthy full-frame count uses `zone_id: null`; an
unavailable camera cannot emit an inferred zero. Line-crossing Events are
separate from count measurements.

## Evidence

ANPR sends the selected **source frame** as `frame` evidence and the plate's
normalized bbox in `plate_read`. Management draws the box on that frame; the
edge need not burn an overlay into the JPEG. There is no separate plate-crop
upload. In/out crossings and positive zone transitions
reference selected context frames. Selected `person_feature_sample` Events
attach a person crop, wider face crop, or representative gait frame; face and
body samples can arrive independently. Scene samples link the exact frame and SigLIP2 vector.
Routine count and health updates normally have no image. Policy-driven clips,
action recognition, and Management-to-edge recheck remain deferred.
ANPR and fire/smoke Events include a zone ID when bound to a polygon, or null
when operating on the full frame. A changed camera view can produce a
`scene_sample` with `trigger: view_changed` and a camera-health update.

For face, the uploaded `face_crop` includes surrounding context from the
original frame. Its source-frame crop box and the face embedding's model and
`preprocessing_id` are recorded. The landmark-aligned 112x112 chip used inside
AdaFace is **not** uploaded as separate evidence; exact chip pixels therefore
cannot be reconstructed solely from the uploaded media if preprocessing changes.

For scene search, each periodic or configured-trigger sample is one
`scene_sample` Event with the source-frame time and IDs of its JPEG evidence
and SigLIP2 embedding. A full-frame binding uploads `frame` evidence. A
polygon binding uploads `scene_region_crop` evidence with its source-frame
bbox, and the embedding must be computed from that same source region before
model preprocessing. The crop is the unmasked, unpadded, axis-aligned bounding
rectangle of the polygon vertices. It can therefore show objects outside the
polygon; `zone_id` identifies the configured selection area, not proof that
every visible object is inside it. A
binding with several polygons produces a separate sample per selected polygon.
The example requests a 2-second interval on its `scene_search` binding; 1
second is a configurable option that still requires capacity validation. Triggered and
periodic work on the same frame should be coalesced by the edge. No scene
sample is produced when the scene app is disabled.

The edge persists an Event and all promised media before first submission.
It sends Event, then linked evidence, then any embedding after evidence ack.
Retries keep IDs and bytes unchanged. Optional scene jobs are skipped before
an Event is created under overload; coverage gaps are reported.

## Separate production policy

The edge's detection, quality, temporal confirmation, reassociation, best-shot,
and dedup algorithms are specified and calibrated outside this wire contract
in `../../V4_1_EVENT_PRODUCTION_POLICY_WORKING.md`. This contract states only
the meanings Management can rely on and the inputs/outputs crossing the
boundary. Management sets only the scene sampling interval in desired state;
presence/count freshness cadence remains edge policy. The example 2-second
scene interval requires capacity and live-stream validation before release.

JSON Schema checks local shape. The YAML `relationalValidation` list contains
cross-document constraints such as binding references, event/evidence links,
positive exits, source-frame bbox ordering, and exact embedding profiles.
