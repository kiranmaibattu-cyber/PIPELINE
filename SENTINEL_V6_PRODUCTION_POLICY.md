# Sentinel V6.0 Production Event Policy

Status: proposed production policy for V6.0 runtime hardening
Date: 2026-10-09
Scope: adaptive gates, presence continuity, event emission, evidence selection,
embeddings, ANPR, dwell support, scene sampling, health, and timestamp semantics.

This policy defines when edge observations become Management outputs. It is more
specific than the wire contract: the contract says what can cross the boundary;
this policy says what the edge should trust enough to send.

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

The runtime should resolve gates per camera and use case, then report the
resolved values to Management as policy telemetry.

Inputs to the resolver:

- Frame size and configured processing FPS.
- Object size distribution.
- Plate size distribution.
- Blur/sharpness and exposure.
- Detection dropout rate.
- Track fragmentation rate.
- OCR stability and unreadable rate.
- Outbox and model queue pressure.

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

## 4. Common Detection, Dedup, and Tracking Gates

Current V6.0 defaults:

| Gate | Current value | Meaning |
| --- | ---: | --- |
| Person/vehicle detector confidence | `0.30` | Detection enters tracking |
| Detection class confidence config | `0.25` per class | Config fallback, detector uses minimum confidence |
| Dedup IoU | `0.55` | Overlapping boxes are duplicate candidates |
| Dedup containment | `0.85` | Box mostly inside another box is duplicate candidate |
| Tracker max distance | `320 px` | Match detection to prior track center |
| Tracker max disappeared | `45 frames` | Tracker memory before deregister |
| Tracker bbox smoothing | `0.25` | Smooth bbox changes |
| Tracker min IoU | `0.01` | Minimal shape overlap for far matches |
| Tracker class aware | `false` | Allows class correction across similar vehicle classes |
| Presence stable hits | `3` | Confirmation before subject events |
| Candidate max gap | `1.5 s` | Reset weak candidate if observations are too sparse |

Production changes:

- `max_distance_px` should be resolved from frame size, FPS, observed speed, and
  missed frame count instead of fixed globally.
- `max_disappeared` should be expressed as seconds and converted to frames.
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
| Boundary margin | `0.01` normalized |
| Transition votes | `2` observations |

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
| Count debounce | `2` observations |
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

### Face

Inputs:

- Person presence.
- Face bbox and landmark-aligned model chip.
- AdaFace embedding.
- Face quality.

Current gates:

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
| Event confidence | `0.55` |
| Model cadence | every `5th` processed frame |
| Positive confirmation | `3` positives within `2 s` |
| Clear confirmation | `10 s` negative |
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

## 15. Implementation Priorities

1. Add adaptive policy resolver and resolved-policy telemetry.
2. Make presence unobserved and ended-unknown timeouts configurable/adaptive.
3. Add ANPR best-candidate first-read window.
4. Add vehicle local recovery after track breaks.
5. Add `identity_sample_group_id` for connected face/body/gait samples.
6. Add timestamp source fields and ensure event/evidence/embedding times refer
   to the selected source frame.
7. Keep count logic as-is except policy telemetry.
