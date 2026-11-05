# Sentinel V6.0 Production Event Policy

Status: implemented production-policy baseline for V6.0 runtime hardening
Date: 2026-10-29
Scope: adaptive gates, presence continuity, event emission, evidence selection,
embeddings, ANPR, dwell support, scene sampling, health, and timestamp semantics.

This policy defines when edge observations become Management outputs. It is more
specific than the wire contract: the contract says what can cross the boundary;
this policy says what the edge should trust enough to send.

For Sentry V8, this policy also owns the exact CV-side package scheduling,
priority, reservation, and coalescing choices. The V8 contract defines only the
cross-component non-starvation and reporting guarantees.

## 1. Core Rule

```text
Use adaptive gates for collection and recovery.
Use strict selected gates for Management events.
A weak frame may help internal tracking, but only a confirmed best observation
becomes a durable Management event.
```

Do not collapse all gates into one threshold. Every producer uses three layers:

| Layer | Purpose | Production posture |
| --- | --- | --- |
| Collection gate | Keep useful internal state alive | Tolerant and adaptive |
| Confirmation gate | Decide whether an observation is real enough | Stable over time |
| Emission gate | Send Management facts, evidence, and embeddings | Conservative |

## 2. Shared IDs and Time

- `track_id` is camera/session-local and temporary.
- `presence_id` is camera/session-local continuity across a qualified track break.
- Management owns cross-camera identity, named face recognition, incidents, dwell
  rules, and alerts.
- `observed_at` must mean when the selected source frame was observed, not when
  the HTTP request reached Management.
- `evidence.captured_at` and `embedding.captured_at` must match the selected
  event frame/sample time.

Timestamp fields should be interpreted as:

| Field | Meaning |
| --- | --- |
| `observed_at` | Source frame time if available, otherwise edge frame receive time |
| `timestamp_source` | `source_pts`, `rtsp_ntp`, or `edge_received_at` |
| `processed_at` | Optional edge inference/event creation time |
| `ingested_at` | Management receive time, owned by Management |

If true camera/media time cannot be extracted, edge must use frame receive time
and report `timestamp_source=edge_received_at`.

## 3. Adaptive Policy Resolver

The runtime resolves bounded gates per camera and subsystem, then reports the
effective values in `/healthz` under each camera's `resolved_policy`. Resolved
policy is operational telemetry, not a desired-state input and not an Event.

Inputs to the resolver:

- Frame size and configured processing FPS.
- Object size distribution.
- Plate size distribution.
- Blur/sharpness and exposure.
- Detection dropout rate.
- Track fragmentation rate.
- OCR stability and unreadable rate.
- Outbox and model queue pressure.

The implementation keeps separate person and vehicle distributions. For every
real (non-predicted) tracked detection it records:

```text
area_coverage = bbox_area / frame_area
object_scale  = sqrt(area_coverage)
width_ratio   = bbox_width / frame_width
height_ratio  = bbox_height / frame_height
```

`object_scale` therefore remains comparable between 720p, 1080p and 4K input.
The current bounded camera-view decisions are:

| Observation | Resolved collection mode |
| --- | --- |
| scale `< .075`, dropout `>= .35`, or 8+ fragmentations/60 s | `tolerant_collection` |
| scale `>= .18`, dropout `<= .08`, and at most 1 fragmentation/60 s | `strict` |
| otherwise | `balanced` |

Presence, zone and count resolve this independently for people and vehicles.
Tracking also uses bbox diagonal/frame diagonal, observed motion, frame size and
processing FPS to resolve association distance and time-based retention.

Absolute pixels are retained where model readability depends on real source
detail. ANPR therefore uses both frame-relative plate coverage and actual plate
width/height, sharpness, OCR confidence, text agreement and unreadable rate.
Identity uses person coverage plus face width, SixDRepNet pose quality, face
quality, face-detection success, frame sharpness and exposure.

Recommended modes:

| Mode | Typical camera | Behavior |
| --- | --- | --- |
| `strict` | close, clear ANPR/entrance camera | Faster emission, stricter quality |
| `balanced` | normal wide security camera | Moderate wait and quality |
| `tolerant_collection` | low-light, occlusion, distant plate camera | Collect longer, emit fewer weak facts |

Bad camera quality should increase collection attempts and wait time; it should
not lower the truth standard for emitted Events.

Example telemetry:

```json
{
  "camera_id": "blr-cam-12",
  "resolved_policy": {
    "mode": "balanced",
    "tracking": {
      "max_distance_px": 360,
      "max_disappeared_seconds": 8
    },
    "presence": {
      "stable_hits": 3,
      "unobserved_after_seconds": 5,
      "ended_unknown_after_seconds": 18
    },
    "anpr": {
      "candidate_window_seconds": 2.5,
      "ocr_min_confidence": 0.32,
      "confirm_min_reads": 2,
      "min_plate_width_px": 24
    }
  }
}
```

Implemented bounded profiles:

| Subsystem | Strict | Balanced | Tolerant collection |
| --- | --- | --- | --- |
| Presence | `3` hits, unobserved `2 s`, ended `8 s` | `3`, `5 s`, `18 s` | `4`, `5 s`, `20 s` |
| Zone | margin `.008`, `2` votes, `.25 s` | `.012`, `2`, `.50 s` | `.018`, `4`, `.80 s` |
| Count | `2` votes, `.25 s` | `2`, `.25 s` | `4`, `.80 s` |
| ANPR | `1.5 s`, OCR `.40`, `3` reads | `2.5 s`, `.35`, `2` | `3.5 s`, `.35`, `2` |
| Identity | face `48 px`/quality `.40`, body `40x80` | face `32 px`/`.30`, body `32x64` | face `28 px`/`.30`, body `28x56` |
| Fire/smoke | score `.62`, `4` positives | `.65`, `5` | `.70`, `6` |

Mode changes require two consecutive five-second resolver evaluations. The
resolver may extend collection/recovery, but cannot lower the final ANPR or
hazard emission standard below the balanced truth gate.

For a small/distant tracked subject (normalized width below `0.05`, normalized
height below `0.05`, or maximum normalized side below `0.12`), the event layer
extends the resolved unobserved window by `2x`, capped at `10 s`, and the
ended-unknown window by `1.1x`, capped at `22 s`. This suppresses short
detector-dropout lifecycle pairs without treating tracker predictions as fresh
observations.

`/healthz` reports the raw measurements, person/vehicle profiles, effective
gates and `decision_reasons`. Elevated pressure doubles optional periodic scene
sampling intervals; critical pressure suppresses optional periodic sampling.
Positive event-triggered scene evidence still passes through the hard outbox
capacity guard. These are edge-runtime decisions and do not alter V6 payloads.

## 4. Common Detection, Dedup, and Tracking Gates

Current V6.0 defaults:

| Gate | Current value | Meaning |
| --- | ---: | --- |
| Person/vehicle detector confidence | `0.30` | Detection enters tracking |
| Detection class confidence config | `0.25` per class | Config fallback, detector uses minimum confidence |
| Dedup IoU | `0.55` | Overlapping boxes are duplicate candidates |
| Dedup containment | `0.85` | Box mostly inside another box is duplicate candidate |
| Tracker max distance | `320 px` fallback | Runtime resolves a bounded frame/motion-aware value |
| Tracker max disappeared | `45 frames` fallback | Runtime resolves seconds and converts using processing FPS |
| Tracker bbox smoothing | `0.25` | Smooth bbox changes |
| Tracker min IoU | `0.01` | Minimal shape overlap for far matches |
| Tracker class aware | `false` | Allows class correction across similar vehicle classes |
| Presence stable hits | `3` | Confirmation before subject events |
| Candidate max gap | `1.5 s` | Reset weak candidate if observations are too sparse |

Implemented production behavior:

- `max_distance_px` is resolved from frame size, FPS, observed speed, and
  missed frame count instead of fixed globally.
- `max_disappeared` is expressed as seconds and converted to frames.
- Dedup thresholds remain defaults, but camera telemetry should flag over-merge
  and duplicate-track patterns.

Suggested timeout classes:

| Camera mode | `presence_unobserved_after` | `presence_ended_unknown_after` |
| --- | ---: | ---: |
| Close/clear | `2 s` | `8 s` |
| Wide/occlusion | `4-5 s` | `15-20 s` |

Guardrails:

- Track loss is not exit.
- Timeout is not exit.
- Camera failure is not exit.
- Absence of a later event is not proof that a condition cleared.

## 5. Presence Lifecycle

State machine:

```text
candidate
  -> active
  -> temporarily_unobserved
       -> recovered
       -> ended_unknown
  -> positively_exited_zone
```

New presence gate:

- Qualifying detection confidence passes the collection gate.
- Track is stable for `3` hits by default.
- If scoped to geometry, subject point must be inside the bound geometry.

Recovery policy:

For person recovery, compare:

- Time gap.
- Location and motion plausibility.
- Body embedding similarity.
- Face embedding if visible.
- Gait sequence if available.
- Absence of a conflicting active person.

For vehicle recovery, compare:

- Time gap.
- Location and motion plausibility.
- Vehicle class and bbox size.
- Plate text if available.
- Vehicle appearance/color embedding if added later.
- Absence of conflicting active vehicle.

Outputs:

- `object_present` on first confirmed presence and freshness update.
- `presence_recovered` when the same presence is reconnected.
- `presence_unobserved` or current `track_lost` when temporarily not observed.
- `presence_ended_unknown` only after the recovery grace expires.

Lifecycle outputs are emitted only when a person/vehicle presence use case owns
the presence. Counting-only, ANPR-only, and identity-only bindings keep local
association state but do not emit `track_lost`, `presence_recovered`, or
`presence_ended_unknown` traffic.

Recommended rename: treat `track_lost` as `presence_unobserved` semantically.

## 6. Vehicle and Person Presence

Inputs:

- Decoded source frame.
- YOLO bbox, class, confidence.
- Tracker `track_id`.
- Binding scope: `full_frame` or `geometry`.

Current emission gate:

- Detection confidence at least `0.30`.
- Stable track for `3` hits.
- Binding matches object type and scope.

Output:

```text
object_present
  presence_id
  track_id
  object_type
  vehicle_type when object_type=vehicle
  bbox_normalized
  confidence
  first_seen_at
  zone_episodes
  evidence_ids on selected first sighting
```

Frequency:

- First confirmed presence.
- Freshness update every `60 s` while present.

Evidence:

- Subject crop from original source frame on first selected presence.
- Routine freshness update should not require another crop.

Guardrails:

- Do not emit every detection.
- Do not duplicate `object_present` for ANPR, face, body, or gait.
- First sighting already inside a zone starts an episode with unknown entry; it
  does not invent `zone_entry`.

## 7. Zone Entry, Zone Exit, and Dwell

Inputs:

- Canonical presence.
- Bbox bottom-center point.
- Polygon geometry.

Current gates:

| Gate | Value |
| --- | ---: |
| Presence stable hits | `3` |
| Boundary margin | adaptive `.008-.018` normalized; balanced `.012` |
| Transition votes | adaptive `2-4` observations; balanced `2` |
| Transition hold | adaptive `.25-.80 s`; balanced `.50 s` |

Outputs:

```text
zone_entry / zone_exit
  presence_id
  track_id
  object_type
  zone_id
  zone_episode_id
  first_observed_inside_at
  transition_at
  evidence: context frame
```

Dwell policy:

- V6.0 does not need a separate dwell timer event.
- Management derives dwell from `zone_episode_id`, `first_observed_inside_at`,
  `last_confirmed_inside_at`, `zone_exit`, and unknown/recovery states.
- During `temporarily_unobserved`, dwell is uncertain.
- On qualified recovery, keep the same `zone_episode_id` and report the
  observation gap.

Guardrails:

- `zone_entry` requires positive outside-to-inside transition.
- `zone_exit` requires positive inside-to-outside transition.
- Track loss, timeout, and camera failure never complete dwell or create exit.

## 8. Count

Inputs:

- Stable visible tracks.
- Optional polygon geometry.

Logic:

```text
if geometry is configured:
  count stable tracks inside the zone
else:
  count stable tracks in the whole frame
```

Current gates:

| Gate | Value |
| --- | ---: |
| Track stable hits | `3` |
| Count debounce | `2` observations plus `.25 s` balanced hold |
| Heartbeat | `20 s` |

Output:

```text
zone_count
  zone_id or null
  object_type
  count
  count_semantics: visible_now
  coverage_state
  as_of
```

Guardrails:

- Count is `visible_now`, not cumulative arrivals.
- `count=0` is valid only from fresh healthy camera coverage.
- Camera unavailable should become unknown/stale at Management, not zero.

## 9. Line Crossing

Inputs:

- Stable presence.
- Previous and current subject point.
- Counting line and direction mapping.

Current gates:

| Gate | Value |
| --- | ---: |
| Presence stable hits | `3` |
| Deadband | `abs(side) >= 0.005` |
| Minimum movement | `0.02` normalized |
| Segment intersection | required |
| Cooldown | `10 s` per presence/line |

Output:

```text
line_cross
  presence_id
  track_id
  object_type
  line_id
  direction_raw
  direction
  crossed_at
  evidence: context frame
```

Guardrails:

- Line crossing is separate from zone exit.
- Jitter near the line must not produce repeated cross/uncross events.

## 10. ANPR

Inputs:

- Stable vehicle presence.
- Vehicle crop from the original source frame.
- Plate bbox detected inside vehicle crop and mapped back to source frame.
- Plate crop from original source frame.
- OCR text and confidence.

Current V6.0 collection gates:

| Gate | Current value |
| --- | ---: |
| Vehicle stable hits | `3` |
| Plate detector confidence | `0.35` |
| Plate min width | `16 px` |
| Plate min height | `5 px` |
| Plate sharpness | `0.0` |
| Plate aspect ratio | `0.8 - 12.0` |
| OCR min confidence | `0.25` |
| OCR confirm reads | `1` |
| Indian plate regex | disabled |
| OCR attempts | `12` |
| Unreadable delay | `3 s` |

Required production emission policy:

- Do not emit the first OCR guess immediately.
- Collect candidates for an adaptive best-candidate window.
- Score by OCR confidence, plate width/height, sharpness, aspect ratio, repeated
  text agreement, and vehicle presence continuity.
- Emit one best `plate_read`.
- Emit `plate_outcome=unreadable` when a plate candidate exists but readable
  text cannot stabilize within the attempt/window policy.

Implemented balanced emission gate:

- Candidate window `2.5 s`.
- OCR confidence at least `0.35` for emission (`0.25` remains an internal OCR
  collection threshold).
- At least two distinct accepted OCR observations, or one result at least
  `0.82` after the complete window.
- Plate at least `22 x 7 px`, sharpness at least `8`, aspect ratio `0.8-12.0`.
- A repeated high-confidence result may emit early only after two distinct OCR
  observations. Re-reading stale OCR metadata does not count as agreement.

Suggested adaptive ranges:

| Mode | Candidate window | OCR min | Confirm reads | Min plate width | Sharpness |
| --- | ---: | ---: | ---: | ---: | ---: |
| Close/clear | `1-1.5 s` | `0.40` | `3-4` | `32 px` | `20` |
| Balanced/wide | `2-3 s` | `0.30-0.35` | `2` | `20-24 px` | `5-10` |
| Tolerant collection | `3+ s` | collect low, emit high | `2+` | `16-24 px` | adaptive |

Correction gate after first read:

- Same text: resend only after material confidence improvement and cooldown.
- Different text: correction only if much stronger or stable across repeated reads.
- Lower-confidence alternate text for the same presence must be suppressed.

Output:

```text
plate_read
  presence_id
  track_id
  zone_id
  plate_text
  confidence
  plate_bbox_normalized
  supersedes_observation_id
  evidence: source frame
```

Evidence policy:

- OCR plate crop is internal.
- Management receives source frame plus `plate_bbox_normalized`.
- Vehicle crop may be retained internally or sent only if contract later adds it.

## 11. Person Identity: Face, Body, and Gait

The three modalities are independent samples tied to the same local presence.
Each output must include:

- `camera_id`.
- `stream_session_id`.
- `presence_id`.
- `track_id`.
- `source_frame_id` or sequence interval.
- `observed_at` / `captured_at`.
- `use_case_id`.

Add `identity_sample_group_id` so Management can group face/body/gait samples
selected from the same presence window even when emitted as separate events.

The runtime creates this group internally. The strict V6.0 wire schemas do not
currently permit this field, so it is not added to Event/Embedding payloads.
`presence_id`, sample timestamps, and model profile IDs remain the V6.0 wire
correlation keys. Adding the group to the wire requires a future contract/schema
revision, not an undocumented V6 extension.

### Face

Inputs:

- Person presence.
- Face bbox and landmark-aligned model chip.
- AdaFace embedding.
- Face quality.

Implemented emission gates:

| Gate | Value |
| --- | ---: |
| Presence stable hits | `3` |
| Best candidate window | `1.5 s` |
| Resend improvement | `+0.10` quality |
| Feature cooldown | `5 s` |

Output:

```text
person_feature_sample
  kind: face
  profile_id: adaface-ir101-v18.1
  quality
  sample_reason
  evidence: wider face_crop
  embedding: 512D
```

Evidence:

- Face evidence crop comes from the original source frame.
- Evidence crop includes surrounding pixels/context.
- Aligned 112x112 chip remains internal.

### Body

Inputs:

- Original-frame person crop.
- TransReID vector.

Current gates:

| Gate | Value |
| --- | ---: |
| Person crop min size | `64 x 32 px` |
| Body quality floor | `0.20` |
| Internal sample cadence | about `1 s` |
| Resend improvement | `+0.15` quality |
| Feature cooldown | `5 s` |

Output:

```text
person_feature_sample
  kind: body
  profile_id: transreid-ssl-v18.1
  evidence: subject_crop
  embedding: 384D
```

### Gait

Inputs:

- Presence track.
- Motion/silhouette sequence.
- GaitBase vector.

Current gates:

| Gate | Value |
| --- | ---: |
| Collection cadence | every `3rd` frame |
| Usable sequence | sequence required |
| Target usable silhouettes | about `20-30` |
| Resend improvement | `+0.20` quality |
| Feature cooldown | `5 s` |

Output:

```text
person_feature_sample
  kind: gait
  profile_id: gaitbase-v18.1
  sequence_started_at
  sequence_ended_at
  usable_silhouette_count
  evidence: representative frame/crop
  embedding: 4096D
```

Guardrails:

- Face-only supports named recognition.
- Body and gait support candidate association only.
- Do not send fake or zero embeddings for absent modalities.
- Do not send embeddings aimlessly; send first qualified best and material
  improvements.

## 12. Scene Search

Inputs:

- Decoded full frame.
- Optional polygon-scoped crop.

Current gates:

| Gate | Value |
| --- | ---: |
| Tracking dependency | none |
| Periodic interval | desired-state, usually `2 s` |
| Event-trigger cooldown | `1 s` |
| Outbox pressure limit | `1 GB` pending outbox/evidence |

Output:

```text
scene_sample
  trigger: interval/event
  scope: full_frame/geometry
  zone_id
  triggering_observation_id when event-triggered
  evidence: frame or scene_region_crop
  embedding: 768D SigLIP2
```

Guardrails:

- Scene embedding is search evidence, not action proof.
- 1-2 second image sampling can miss short actions.
- Polygon crop is an axis-aligned source rectangle of polygon vertices; it is a
  selection scope, not proof every visible object is inside the polygon.

## 13. Fire and Smoke

Inputs:

- Fire/smoke detector boxes.
- Optional zone geometry.

Current gates:

| Gate | Value |
| --- | ---: |
| Detector confidence | `0.35` |
| Event confidence | adaptive `.62-.70`; balanced `.65` |
| Model cadence | every `5th` processed frame |
| Positive confirmation | adaptive `4-6` spatially consistent positives; balanced `5` within `3 s` |
| Clear confirmation | `10 s`, at least `5` evaluated negatives, no evaluation gap over `2.5 s` |
| NMS IoU | `0.45` |
| Model input size | `320` |

Outputs:

```text
fire_smoke_suspected
  hazard
  score
  zone_id
  hazard_episode_id
  evidence: context frame

fire_smoke_cleared
  hazard
  zone_id
  hazard_episode_id
```

Guardrails:

- `suspected` is not a confirmed incident.
- `cleared` means observed clear for the configured interval, not a safety
  guarantee.
- Dropped, skipped, or unevaluated frames never count as negative evidence; a
  long evaluation gap resets the clear candidate.

## 14. Health and Delivery

Health outputs:

- Healthy/degraded/unavailable transitions.
- Healthy heartbeat every `30 s`.
- Backpressure degraded report every `60 s`.

Delivery order:

```text
1. Event JSON
2. Evidence image
3. Embedding vector
```

Guardrails:

- Persist event, evidence, and embedding before first send.
- Retry with identical IDs and bytes.
- Do not drop promised evidence after the parent event is accepted.
- Embeddings wait for event and evidence acknowledgement.
- Optional scene jobs may be skipped before event creation under pressure.
- Sampling gaps must be reported.

## 15. V8 CV-to-Agent Handoff Scheduling

The V8 CV runtime has exactly one deployment-scoped in-memory handoff
scheduler shared by all camera workers. Camera workers do not own retry queues
or delivery-agent upload threads.

Current implementation profile:

| Setting | Value |
| --- | ---: |
| Retained packages, including in-flight/retrying | `8` |
| Retained package bytes | `256 MiB` |
| Simultaneous agent handoffs | `4` |
| Replaceable package reservation limit | `4` |
| Replaceable byte reservation limit | `128 MiB` |

Priority tiers, highest first:

1. Critical: fire/smoke suspected or cleared and camera-health state/reason
   transitions.
2. Durable facts: zone entry/exit, line crossing, plate read/outcome, presence
   lifecycle, first/materially changed presence, and event/view-triggered scene
   context.
3. Identity samples: selected face, body, and gait packages.
4. Replaceable: interval scene samples and unchanged count, presence, and
   camera-health heartbeats.

Pressure behavior:

- Never cancel an in-flight handoff.
- Optional/replaceable work cannot consume the reserved half of scheduler
  capacity.
- Before rejecting non-replaceable work, remove or coalesce queued replaceable
  work that has not yet been submitted.
- Keep only the newest queued interval scene sample for the same camera,
  use-case, scope, and zone.
- Keep only the newest unchanged count heartbeat for the same camera,
  use-case, object type, and zone.
- Keep only the newest periodic presence update for the same camera, use-case,
  and presence.
- Keep only the newest unchanged camera-health heartbeat for the same camera,
  state, and reason.
- Order remaining non-replaceable work by priority tier, `observed_at`, then
  `package_id`.
- A rejected or retry-exhausted package is reported in CV health/metrics and is
  never counted as produced.

These values are production-policy tuning, not desired-state inputs and not
V8 wire-contract fields. They can be changed and requalified without changing
the V8 package or Management ingest schemas.

## 16. V6 Implementation Status

Implemented:

1. Bounded adaptive resolver with hysteresis and `/healthz` telemetry.
2. Resolution/motion-aware tracker association and time-based retention.
3. Adaptive presence unobserved/ended-unknown windows and ambiguity-aware local
   recovery.
4. Positive zone transition votes/hold; track loss never creates exit.
5. Debounced `visible_now` counts with 20-second freshness heartbeat.
6. ANPR distinct-observation best-candidate window, quality gates, unreadable
   outcome, confidence improvement, cooldown, and correction protection.
7. Independent face/body/gait selection from original-frame crops.
8. Spatially and temporally confirmed fire/smoke episodes.
9. Source-frame event/evidence/embedding times and `/healthz` timestamp-source
   telemetry.
10. Durable Event -> Evidence -> Embedding delivery and scene backpressure.

Contract-preserving deferrals:

- `identity_sample_group_id` and per-Event `timestamp_source` are not placed on
  the V6 wire because the strict 6.0 schemas do not define them.
- Cross-camera identity, named recognition, incidents, dwell rules, and alerts
  remain Management responsibilities.
