# Sentinel CV V4.1 Event Production Policy

Status: working design for review, not an implemented image or approved contract
Date: 2026-10-01
Baseline: Sentinel CV V4 draft and V18.1 inference behavior

## 1. Purpose and boundary

This document decides which edge observations become Management outputs, and
when. It precedes changes to desired-state and ingest schemas. All numerical
defaults below are initial tuning values, not accuracy claims; they require
validation on representative streams, including traffic1, traffic2, and ch9.

The edge owns detection, local temporal confirmation, camera-local presence
continuity, measurements, selected evidence, embeddings, and durable delivery.
Management owns rule interpretation, thresholds and schedules for alerts,
cross-camera association, named face recognition, semantic search, incident
state, and operator recheck decisions. No body/gait match names a person.

Policy outcomes are:

| Outcome | Meaning | Delivery |
| --- | --- | --- |
| Internal candidate | A model or tracker observation that has not passed gates | Never sent as a durable fact |
| Confirmed event | A positively observed transition or significant state change | Durable Event |
| Measurement | A value valid only at its `as_of` time | Durable typed Event initially; Management applies freshness |
| Artifact | Selected media or model vector supporting a parent fact | Linked evidence/embedding submission |
| Unknown | Observation ceased without proof of the opposite state | Explicit status/coverage signal; never a synthetic exit, zero, or completion |

The existing V4 Event -> Evidence -> Embedding acknowledgement order remains the
delivery baseline. These categories describe semantics, not three new APIs.

## 2. Fixed invariants

1. Each camera stream opening has one `stream_session_id` shared by all subject,
   zone, scene, and health producers. Reconnect or worker restart creates a new
   session. The old session's unfinished presences become unknown, not exited.
2. A `track_id` is tracker-local within one camera session. A `presence_id` is
   camera/session-local continuity across a short qualified track reassignment.
   Neither identifies a person across cameras.
3. Every subject-scoped Event references the canonical presence and the track
   that supplied its positive observation. Zone and dwell state use the presence,
   not a pipeline-private track ID. A `zone_episode_id` distinguishes repeated
   visits by the same presence to the same zone.
4. Emit a transition only from positive observations. Track disappearance,
   absent Events, camera failure, and timeouts do not prove exit or safety.
5. Persist each new output and its immutable IDs before attempting delivery.
   Retry with identical IDs and bytes. A new measurement or correction gets a
   new ID and explicitly relates to the earlier record when appropriate.
6. Use source-frame capture time for `observed_at` when reliable; record processing
   and receipt times separately. Management orders state by occurrence time and
   camera coverage, never HTTP arrival order alone.
7. Geometry IDs are Management-issued and interpreted under `config_revision`.
   All bounding boxes use normalized coordinates relative to the source frame.
8. Quality scores and vector similarities are model outputs, not calibrated
   identity probabilities. A vector is compared only within an identical,
   immutable embedding profile.
9. Periodic full-frame scene sampling and camera health do not depend on YOLO,
   tracking, or the presence registry.

## 3. Shared camera state

The camera worker maintains one registry of active and recently unobserved
presences. Each record contains: session ID, presence ID, active and previous
track IDs, first/last positive observation, current bounding box/class, local
appearance reference, zone episodes, best qualified media/vector candidates,
and last emitted semantic facts. The registry is operational state, not a
permanent identity gallery. Its mutation is serialized per camera/frame.

### 3.1 Presence state machine

```text
candidate -> active -> temporarily_unobserved -> active (qualified recovery)
                    |                         -> ended_unknown (grace expires)
                    -> positively_outside_zone -> zone exit for that zone
                    -> confirmed_line_crossing  -> separate line-cross fact
```

- New presence: require three qualifying processed observations by default.
  Initial observations may be consecutive; a configurable maximum gap prevents
  three widely spaced frames from being treated as continuous confirmation.
- Reassociation: default maximum gap 8 seconds, body cosine similarity at least
  0.78, center displacement at most 320 source-frame pixels, and no conflicting
  simultaneously active prior track. These are the existing V4 Re-ID starting
  values, not final universal thresholds. Use elapsed time and frame size to
  calibrate spatial plausibility. If appearance is unavailable, do not claim a
  high-confidence reassociation based on proximity alone.
- Preserve `presence_id` and zone episode on qualified reassociation. Report
  previous and new track IDs and association method/confidence so Management can
  inspect continuity. A failed check creates a new presence.
- A temporarily unobserved presence cannot advance confirmed dwell duration based solely
  on elapsed wall time. On recovery, resume from the original `started_at` only
  when the same in-zone episode is supported; indicate the observation gap.
- On grace expiry, emit an unknown/observation-ended status once. Do not emit
  `zone_exit`, `person_departed`, `vehicle_departed`, or completed dwell.
- On camera unavailability, mark all active presences unobserved. A new stream
  session starts new presences; Management may correlate them as candidates.

### 3.2 Production stages

```text
decoded frame -> model detections -> class/size/confidence/quality gates
              -> tracker + canonical presence registry
              -> geometry and temporal confirmation
              -> episode-level deduplication
              -> selected Event / measurement / artifact
              -> one camera outbox
```

The decoded frame is shared, but scene sampling remains an independent consumer.
The outbox should have one coordinated uploader per camera; child artifacts wait
for acknowledgement of their own parent Event.

## 4. Per-output production policy

The first column is the fact Management receives. All intervals/cooldowns are
per camera unless a more specific scope is stated. Geometry and class binding
come from the requested use case, not from applying every polygon to every app.

| Output | Gate and temporal confirmation | Association / dedup key | Emit, refresh, and end | Evidence |
| --- | --- | --- | --- | --- |
| `object_present` | Deduplicated person/vehicle box, valid class/size/confidence, stable presence in 3 processed observations | `(session, presence_id)` | Once on confirmation; on material class/geometry state change; optional 60 s freshness refresh. First sighting already inside a zone is presence, not `zone_entry`. | Subject crop only when selected by policy; routine refresh needs none |
| `zone_entry` | Seen outside, then clearly inside for 2 qualifying observations beyond boundary margin | `(session, presence_id, zone_id, zone_episode_id, entry)` | Once per entry episode. First seen inside starts an episode with unknown entry transition; it does not invent a crossing. | Context frame when requested |
| `zone_exit` | Clearly observed outside for 2 observations | `(session, presence_id, zone_id, zone_episode_id, exit)` | Once per episode. Never on disappearance or timeout. V5.2 has no line-to-zone exit mapping; `line_cross` is a separate fact. | Context frame when requested |
| `line_cross` | Track stable for at least 3 observations, endpoint-order direction, deadband, minimum displacement, actual segment intersection | `(session, presence_id, line_id, crossing_episode)` | Once per real crossing; initial 10 s per-presence/line cooldown, without suppressing an independently confirmed reverse crossing after leaving the deadband. | Frame near crossing when requested |
| `person_count` / `vehicle_count` | Recalculate the number currently visible inside a bound polygon from each processed frame, using deduplicated qualifying subjects; camera coverage reported. Count is `visible_now`, not cumulative arrivals or guaranteed occupancy. Debounce a changed value over 2 processed observations. | `(session, zone_id, object_type, count_method)` | Send the current total on initial healthy measurement, stable change, and 20 s healthy heartbeat. Zero requires an actual healthy measurement. Mark stale/unavailable as unknown at Management. | Normally none; optional snapshot on change |
| Management-derived dwell | Confirmed positive in-zone observations; retain one first-observed-inside time and episode across qualified reassociation | `(session, presence_id, zone_id, zone_episode_id)` | No separate V5.2 dwell Event. `object_present` carries first and latest confirmed in-zone times; later freshness updates and positive `zone_exit` let Management calculate duration. Observation gaps and unknown end prevent false completion. | Selected presence/entry/exit frames; no mandatory timer snapshot |
| `threshold_exceeded` | Valid configured threshold crossing using fresh count/dwell measurement | `(threshold_id, zone_episode_id or count-state epoch)` | Prefer Management deriving alerts from typed facts. If edge telemetry is retained for V4 compatibility, emit once on false-to-true crossing, never per frame. | Reuse triggering measurement evidence |
| `plate_read` | Internal plate crop passes size/sharpness/aspect/visibility gates; OCR passes confidence and multi-read consensus | `(session, vehicle_presence_id, plate_episode)` | Once on confirmed stable text. A justified correction is a new Event with `supersedes_event_id`; provisional display text is not a confirmed plate Event. An unreadable plate is an outcome/status, not an invented `plate_read`. | Source frame with normalized plate bbox; internal OCR crop is not uploaded |
| Face embedding | Face size, pose/visibility, sharpness/exposure and quality gates; finite normalized 512D vector; bounded best-candidate window | `(session, presence_id, face_profile_id, sample_revision)` | First best qualified sample; then only materially better quality after cooldown or a reviewed association reset. Large vector disagreement is a possible track error, not automatically an update. | Wider source-frame face crop and preprocessing ID; aligned model chip remains internal |
| Body embedding | Qualified person crop and finite normalized 384D vector; reject tiny/occluded crops | `(session, presence_id, body_profile_id, sample_revision)` | First qualified sample; then selected quality improvement or meaningful appearance change with cooldown. Avoid unconditional 5 s transmission. | Exact selected person crop |
| Gait embedding | Sufficient valid moving silhouettes from the same presence; initial minimum 20 usable frames, model sequence 30 frames | `(session, presence_id, gait_profile_id, sequence_revision)` | First qualified sequence; replacement only when sequence quality materially improves. No output for stationary/occluded/short tracks. | Representative frame plus sequence interval, frame count, silhouette provenance; do not imply one crop is the gait input |
| `scene_sample` | Valid decoded frame, valid SigLIP profile and bounded inference queue; independent of detections | `(camera, region, source_frame_id, sample_purpose)` | Periodic full-frame target every 2 s; 1 s for selected cameras/regions after capacity validation. Optional configured event-triggered sample; coalesce same-frame triggers and apply a separately configured trigger cooldown. | Selected JPEG and 768D scene embedding; trigger Event ID when applicable |
| `fire_smoke_suspected` | Repeated positive detections, initially 3 qualified observations within 2 s, with usable camera coverage | `(session, hazard_kind, region_episode)` | Once on suspected onset; update on material escalation. A clear signal needs sustained healthy negative observation, initially 10 s, and a distinct status/event. No safety confirmation claim. | Event-time context frame |
| `camera_health` | Decode success, frame age, and stream/coverage checks | `(camera, health_episode)` | Healthy/degraded/unavailable transitions; recovery to healthy; 30 s heartbeat. A healthy first frame does not substitute for later failure transitions. | Usually none |

The face best-candidate window starts at 1.5 seconds, preferred quality 0.65,
and current absolute quality floor 0.20. The floor is permissive and must be
raised or calibrated using live face crops before production. Minimum source
face size and pose/sharpness thresholds must be recorded by the model policy;
an aligned 112x112 model input does not prove that the original crop had enough
detail. Body/gait quality policies are independently tuned; a single shared
`minimum_quality` field is insufficient.

Face and body inference should use crops from the same decoded source frame and
the same canonical presence, but they have independent selection and emission
clocks. Face detection runs inside the original-frame person crop; AdaFace uses
its landmark-aligned 112x112 chip. TransReID uses the selected original-frame
person crop. The face UI evidence is a wider, in-frame square around the
detected face (initially about twice its largest side), so operators can inspect
context; record the source frame ID, original face box, evidence crop box, and
model preprocessing version. The aligned 112x112 chip is internal in V5.2 and
is not uploaded as a second face image. A face may be
selected and sent even when no new body vector is due, and vice versa. Both
refer to the same presence; neither requires a duplicate `object_present`.

The V18.1 analytics path emitted an instantaneous count on every processed
frame. V4 currently emits a `visible_now` count about once per second per zone.
The proposed policy keeps per-frame recomputation internally but sends only a
stable changed total and a freshness heartbeat. A transition from 3 to 4 sends
`count=4`, not `+1`; a later departure can send `count=3`. Cumulative in/out
totals, if required, come from validated line crossings and have different
semantics and fields.

At 20 cameras, a 2 s scene interval means about 10 SigLIP inferences per second
and 864,000 scene samples per day before event-triggered samples; 1 s doubles
both. JPEG transfer and storage may dominate vector bytes. Validate GPU budget,
queue latency, JPEG size, outbox disk usage, and Management ingest capacity at
the intended camera count. If overloaded, drop stale scene jobs with explicit
sampling-gap metrics; do not silently claim complete coverage. A 1-2 s image
sample still cannot prove a short action such as touching a scooter.

For `scene_sample`, an event trigger means Management configured a class of
triggers (for example confirmed zone entry or plate read) in desired state.
The edge does not interpret natural-language rules. A single-frame scene vector
supports candidate retrieval, not a confirmed interaction or temporal action.
For a polygon-scoped V5.2 scene sample, crop the unmasked, unpadded,
axis-aligned bounding rectangle of the polygon vertices. Record that source
rectangle on the JPEG evidence. The crop may show objects outside the polygon;
the zone ID is a selection scope, not proof of in-zone membership.

When several subject-producing bindings qualify at first confirmation, choose
one `object_present` owner. Presence bindings outrank identity and ANPR
bindings; break ties by lexicographically smallest `use_case_id`. Keep that
owner while enabled, even as zone membership changes. If a later revision
disables it, reselect without emitting another `first_seen`. `presence_id`
continues, and the owner ID does not claim current zone membership.

## 5. Management interpretation

Management uses `observed_at`, config revision, camera health, measurement
freshness, episode state, and evidence readiness when evaluating a rule. V5.2
defers the Management-to-edge recheck command. Management must not treat absence
of later edge output as proof that a condition cleared. Late arrivals are
retained by occurrence time and cannot silently overwrite newer current state.

Example: vehicle remains in parking zone for five minutes.

1. Edge confirms a vehicle presence and sends `object_present` with a zone
   episode and first-positive-inside time. If first seen inside, entry
   transition is unknown; no `zone_entry` is invented.
2. Edge sends sparse `object_present` freshness updates with the same episode
   ID and latest confirmed in-zone time. Management computes dwell and applies
   its own five-minute parking threshold; edge sends no milestone Event.
3. If the track is lost, edge marks it temporarily unobserved. A qualified new
   track can resume the same `presence_id`; no duplicate entry is emitted.
4. A positive outside observation sends `zone_exit` and completes the episode.
   A line crossing is separate. If only the grace timer expires, edge reports
   unknown instead.
5. Management groups repeated facts into one incident and may request operator
   review. Cross-camera identity is a separate Management decision.

Example timing for one continuously visible presence: at 10:00:00 the edge
sends `object_present` with `first_observed_inside_at=10:00:00`. A freshness
update at 10:01:00 advances `last_confirmed_inside_at`. At 10:05:00 a fresh
presence update gives Management enough coverage to evaluate its 300 s rule.
At 10:07:12 a positively observed exit sends `zone_exit`. Each update keeps
the same episode ID and first-inside time but gets a new Event ID. During an
observation gap, the displayed duration is uncertain.

Named face recognition uses a qualifying face-gallery comparison only. Body
and gait aid candidate association. Search results from scene vectors should
open the exact JPEG or a guaranteed recorder locator; the current V4 transport
requires JPEG evidence. Policy-driven video clip capture and temporal action
recognition are deferred from this working policy.

## 6. Boundary controls and edge policy

Management configures geometry once, binds use-case instances to geometry IDs,
selects object classes and embedding profiles, and sets the scene sampling
interval. The edge publishes supported use cases, event versions, profiles,
and limits. Unsupported configurations are rejected as a whole.

Confirmation counts, geometry margins, reassociation thresholds, count
debounce/heartbeat, face and body quality gates, best-shot windows, scene
coalescing/cooldowns, queue limits, and camera-health thresholds belong to the
edge production policy, not V5.2 desired state. Management owns dwell alert
thresholds; V5.2 carries zone episode timestamps and unknown states rather
than a separately configurable dwell milestone Event. None of these settings
can redefine track timeout as `zone_exit`, `visible_now` as occupancy, or
body/gait as named identity.

## 7. Contract and runtime changes required

This document does not claim the current V4 image implements the policy.

1. Add modular use-case bindings, shared geometry, scene sampling interval,
   and immutable embedding profiles to desired state. Keep internal emission
   gates in the edge production policy.
2. Add shared `stream_session_id`, canonical `presence_id`, and `zone_episode_id`
   plumbing across tracking, zones, dwell, Re-ID, and Events. Preserve explicit
   reassociation provenance and observation gaps.
3. Define event payloads for presence observation lost/recovered/ended unknown,
   plate correction and terminal unreadable outcome, hazard clear, scene trigger,
   and count/dwell freshness. Prefer separate versioned event schemas behind a
   stable transport envelope.
4. Route V4 `line_cross` through the validated crossing gate. Stop duplicate
   `object_present` production by base Events and Re-ID; attach selected vectors
   and evidence to one canonical parent fact.
5. Apply face best-candidate, quality, cooldown, and duplicate gates to V4
   delivery using the edge policy. Define
   modality-specific body and gait replacement tests.
6. Use one coordinated per-camera outbox uploader with bounded queues, delivery
   lag/disk-pressure metrics, and parent-dependent retries. Delivery rate is
   asynchronous; event emission rate is controlled by this policy.
7. Validate gates and false transitions against live or recorded traffic1,
   traffic2, and ch9 examples, including tracker switches, boundary jitter,
   missed frames, OCR variants, small faces, occlusion, and camera reconnect.

## 8. Decisions to calibrate before approval

The policy semantics above are the working decisions. Calibration must establish
whether the initial 3-observation presence gate, 2-observation boundary gate,
8 s reassociation grace, 10 s crossing cooldown, 2 s scene interval with
optional 1 s high-priority sampling, 20 s count heartbeat,
60 s dwell/presence heartbeat, 1.5 s face selection window, and fire/smoke
confirmation windows meet latency and false-positive targets on each camera
class. Measure Event rate, bytes per camera-hour, upload lag, missed short
events, and false exits/recoveries. Revise numerical defaults without weakening
positive-transition and unknown-state semantics.
