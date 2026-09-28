# Sentinel Edge-to-Management Contract

Working draft for mapping the Sentinel PRD onto the current V18-style edge image.

## 1. Boundary

The edge image is the observation producer. It detects, tracks, crops, embeds, and
delivers evidence. Management is the product brain. It owns rules, schedules,
semantic search, incidents, response workflows, alerts, calls, agent runs,
acknowledgment, resolution, reporting, and permissions.

The product flow is:

```text
Desired state -> Edge observations/evidence -> Management rules/search/state -> Incidents/workflows/outputs
```

Do not make the edge open incidents. The edge should say:

```text
Vehicle V was visible in zone Z from T1 to T2.
Person sample S has body/face/gait embeddings.
Camera C cannot currently provide evidence.
```

Management decides:

```text
This violates rule R, opens incident S-104, alerts the operator, calls the guard,
schedules a recheck, asks the agent to investigate, and later resolves the case.
```

## 2. V18 Baseline To Preserve

V18 already gives the right direction for this split:

- Live SSE analytics events exist for ANPR, vehicle counting, pedestrian
  counting, fire/smoke, and face_seen.
- Durable vehicle entry/exit crossings are sent through an acknowledged upload
  path, not duplicated onto SSE.
- Face samples use an internal/durable path; embeddings are absent from SSE.
- Desired-state hot reload works without restarting the container or OpenVINO
  worker.
- Management owns cumulative entry/exit totals.
- Face artifact upload survives restart and preserves sample identity.
- V18 validation proves transport and event generation on tested streams, not
  field accuracy, ANPR accuracy, recognition accuracy, violence accuracy, or
  maximum multi-camera capacity.

Sentinel should extend this pattern rather than collapsing product logic into
the image.

## 3. Desired State Shape

Desired state sent to edge should contain physical observation configuration,
not business policy.

Edge desired state should include:

- `edge_id`
- `revision`
- cameras and source secrets
- enabled apps per camera
- normalized zones
- normalized lines and direction mapping
- evidence capture settings
- emission/cooldown settings
- quality thresholds
- embedding model spaces
- optional local gallery revision for face-only recognition

Management should not send these as edge desired state:

- after-hours schedules
- parking grace periods
- loitering duration rules
- watchlists as incident policy
- severity
- recipients
- call escalation
- acknowledgment logic
- resolution logic
- agent objectives
- retention/legal policy beyond evidence capture hints

Example desired state:

```json
{
  "edge_id": "sentinel-north-yard-01",
  "revision": 18,
  "cameras": [
    {
      "camera_id": "rear_gate",
      "source": "file:/run/secrets/apexfabric/rear_gate.url",
      "solution_pack": "sentinel-security",
      "fps": 10,
      "apps": [
        "anpr",
        "vehicle_counting",
        "vehicle_entry_exit_counts",
        "people_counting",
        "person_entry_exit_counts",
        "zone_presence",
        "line_crossing",
        "face_recognition",
        "reid",
        "violence_detection"
      ],
      "config": {
        "zones": {
          "anpr": [{"id": "gate_plate", "name": "Gate ANPR", "poly": [[0.1,0.2],[0.9,0.2],[0.9,0.7],[0.1,0.7]]}],
          "vehicle_counting": [{"id": "gate_vehicle_area", "name": "Gate vehicle area", "poly": [[0.1,0.2],[0.9,0.2],[0.9,0.8],[0.1,0.8]]}],
          "people_counting": [{"id": "rear_gate_people", "name": "Rear gate people", "poly": [[0.1,0.2],[0.9,0.2],[0.9,0.8],[0.1,0.8]]}],
          "restricted_area": [{"id": "rear_gate_restricted", "name": "Rear gate restricted", "poly": [[0.2,0.2],[0.8,0.2],[0.8,0.8],[0.2,0.8]]}],
          "no_parking": [{"id": "gate_no_parking", "name": "Gate no-parking lane", "poly": [[0.1,0.4],[0.9,0.4],[0.9,0.8],[0.1,0.8]]}],
          "face_recognition": [{"id": "face_gate", "name": "Face capture gate", "poly": [[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]}],
          "violence_detection": [{"id": "loading_bay", "name": "Loading bay", "poly": [[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]}]
        },
        "lines": {
          "vehicle_entry_exit_counts": [
            {
              "id": "main_gate_line",
              "name": "Main gate line",
              "a": [0.25, 0.7],
              "b": [0.75, 0.7],
              "direction_mapping": {
                "right_to_left": "in",
                "left_to_right": "out"
              }
            }
          ],
          "person_entry_exit_counts": [
            {
              "id": "rear_gate_person_line",
              "name": "Rear gate person line",
              "a": [0.2, 0.55],
              "b": [0.8, 0.55],
              "direction_mapping": {
                "right_to_left": "in",
                "left_to_right": "out"
              }
            }
          ]
        },
        "emission": {
          "presence_update_seconds": 10,
          "cooldown_seconds": 5,
          "material_change_threshold": 0.2
        },
        "evidence": {
          "event_frame": true,
          "object_crop": true,
          "face_crop": true,
          "clip_seconds_before": 5,
          "clip_seconds_after": 10
        },
        "embedding": {
          "body": {"enabled": true, "embedding_space": "transreid_ssl_int8@v1:l2:384", "dimensions": 384},
          "face": {"enabled": true, "embedding_space": "adaface-ir101-int8@v1:aligned112:l2:512", "dimensions": 512},
          "gait": {"enabled": true, "embedding_space": "gaitbase_int8@v1:30x64x44:parts16x256:l2:4096", "dimensions": 4096}
        },
        "face_recognition": {
          "mode": "face_only",
          "minimum_quality": 0.35,
          "preferred_quality": 0.65,
          "selection_window_seconds": 1.5,
          "gallery_revision": 12
        }
      }
    }
  ]
}
```

## 4. Event And Evidence Rules

Every event should have:

- `schema_version`
- `event_id`
- `timestamp` or `observed_at`
- `camera_id`
- `solution_pack`
- `application`
- `event_type`
- `payload`

Important payload fields:

- `zone_id` or `line_id`
- `object_type`
- `track_id`
- `presence_id` for continuous zone stay
- `sample_id` for embedding/crop observations
- `bbox`
- `confidence`
- `quality`
- `first_seen_at`
- `last_seen_at`
- `duration_seconds`
- `evidence_status`
- `snapshot_assets`
- `missing_artifact_kinds`

Evidence should include, when relevant:

- source event frame
- object crop
- vehicle crop
- plate crop
- person crop
- face crop
- short clip
- bounding boxes
- normalized zone/line metadata
- embedding artifact or durable record reference
- model id / embedding space / dimensions
- quality and confidence

Embeddings should not be emitted over public SSE. Use durable records/artifact
upload paths like the existing face sample and management sync designs.

## 5. Core Event Families

### 5.1 Object Seen

Use for direct observations that do not yet imply a zone lifecycle.

```json
{
  "schema_version": "1.0",
  "event_id": "rear_gate:object_seen:track-44:2026-09-24T10:00:00Z",
  "timestamp": "2026-09-24T10:00:00Z",
  "camera_id": "rear_gate",
  "solution_pack": "sentinel-security",
  "application": "object_detection",
  "event_type": "object_seen",
  "payload": {
    "object_type": "person",
    "track_id": "44",
    "bbox": {"x1": 210, "y1": 120, "x2": 360, "y2": 520},
    "confidence": 0.91,
    "zone_id": "rear_gate_restricted",
    "evidence_status": "available",
    "snapshot_assets": {
      "event_frame": {"url": "/snapshots/rear_gate/object-44.jpg", "content_type": "image/jpeg"}
    }
  }
}
```

Management uses this for search, live activity, agent evidence, and simple
presence rules.

### 5.2 Zone Presence Lifecycle

Use for dwell, no-parking, loitering, after-hours presence, restricted-area
presence, and recheck.

Events:

- `object_entered_zone`
- `object_presence_update`
- `object_exited_zone`
- `object_visibility_lost`
- `object_reassociated_in_zone`

The stable grouping key is `presence_id`, not `track_id`.

```json
{
  "schema_version": "1.0",
  "event_id": "rear_gate:gate_no_parking:presence-abc123:update:2026-09-24T10:02:00Z",
  "timestamp": "2026-09-24T10:02:00Z",
  "camera_id": "rear_gate",
  "solution_pack": "sentinel-security",
  "application": "zone_presence",
  "event_type": "object_presence_update",
  "payload": {
    "presence_id": "rear_gate:gate_no_parking:presence-abc123",
    "zone_id": "gate_no_parking",
    "zone_name": "Gate no-parking lane",
    "object_type": "vehicle",
    "track_id": "44",
    "previous_track_ids": [],
    "first_seen_at": "2026-09-24T10:00:00Z",
    "last_seen_at": "2026-09-24T10:02:00Z",
    "duration_seconds": 120,
    "bbox": {"x1": 211, "y1": 173, "x2": 491, "y2": 422},
    "confidence": 0.94,
    "evidence_status": "available",
    "snapshot_assets": {
      "event_frame": {"url": "/snapshots/rear_gate/no-parking-abc123.jpg", "content_type": "image/jpeg"},
      "vehicle_crop": {"url": "/snapshots/rear_gate/no-parking-abc123-vehicle.jpg", "content_type": "image/jpeg"}
    }
  }
}
```

Management uses `presence_id` to create at most one incident per continuous
episode and to attach later updates/rechecks to the same case.

### 5.3 Track Change And Re-ID Repair

`track_id` is a tracker label. It can change. The edge or Management should not
treat a new track id as a new real-world object without checking evidence.

If the edge can confidently link the new track to the existing zone stay, emit:

```json
{
  "event_type": "object_reassociated_in_zone",
  "timestamp": "2026-09-24T10:01:05Z",
  "camera_id": "rear_gate",
  "payload": {
    "presence_id": "rear_gate:gate_no_parking:presence-abc123",
    "zone_id": "gate_no_parking",
    "object_type": "vehicle",
    "previous_track_id": "44",
    "track_id": "91",
    "previous_track_ids": ["44"],
    "first_seen_at": "2026-09-24T10:00:00Z",
    "last_seen_at": "2026-09-24T10:01:05Z",
    "gap_seconds": 3,
    "duration_seconds": 65,
    "association": {
      "method": "spatial_reid",
      "status": "linked",
      "confidence": 0.88
    }
  }
}
```

If confidence is weak, emit `object_visibility_lost` and start a new
`presence_id` for the new track. Management may show the relation as uncertain,
but should not count it as confirmed continuous dwell.

Recommended association bands:

- high confidence: auto-link, same `presence_id`
- medium confidence: candidate only, no automatic dwell continuity
- low confidence: no link

Exact thresholds must be validated on site footage.

### 5.4 Count Events

Use current count events for occupancy only. Do not use them for entry/exit
totals.

Events:

- `vehicle_count_per_frame`
- `people_count_event`

Payload should include count, zone, object boxes, timestamp, and evidence.
Management displays current occupancy, applies count thresholds, and handles
site-wide aggregation carefully. Missing camera coverage means unavailable, not
zero.

### 5.5 Crossing Events

Vehicle crossings should preserve the V18 durable upload pattern. Add person
crossings using the same semantics if needed.

Events/records:

- `vehicle_crossing`
- `person_crossing`

Edge sends one durable record per completed crossing with:

- stable `event_id`
- `observed_at`
- `camera_id`
- `line_id`
- `direction`
- `track_id`
- crossing point
- object class/type
- evidence frame

Management deduplicates by `event_id` and computes daily/hourly totals in the
site timezone. Edge must not send running totals.

### 5.6 ANPR Events

Events:

- `plate_read`
- `unknown_plate_vehicle_seen`

Payload:

- plate text when readable
- OCR confidence
- plate bbox if available
- vehicle bbox
- vehicle track id
- event frame
- vehicle crop if available
- plate crop if available

V18 currently exposes ANPR event frame, not separate plate/vehicle crops. For
Sentinel, separate crops are desirable because Management needs plate review,
search, and evidence export.

Management owns plate history, watchlist comparison, return rules, frequent
vehicle grouping, and incidents for configured plates of interest.

### 5.7 Appearance Samples And Re-ID

Use durable management records for body, face, and gait embeddings. The edge
extracts crops and embeddings; Management stores indexes and handles search,
association, and identity graph logic.

For gait, V18 uses one OpenCV MOG2 background model per fixed camera. It crops
the foreground mask to the tracked YOLO26 person box, applies morphological
cleanup and OpenGait 64x44 normalization, then sends 30 accepted silhouettes to
GaitBase. A neural instance-segmentation model is not part of the V18 gait path.
Camera movement, background warm-up, merged foregrounds, and insufficient motion
must suppress gait evidence or reduce its quality rather than create identity.
Record kind:

- `reid_observation`

Payload should include:

- `observation_id`
- `camera_id`
- `stream_session_id`
- `track_id`
- local identity id if available
- captured time
- bbox/source coordinates
- quality
- embeddings array
- modality: `body`, `face`, `gait`
- `embedding_space`
- dimension
- vector
- artifact references for event frame/person crop/face crop when available

Management builds separate indexes per modality and embedding space. Do not mix
body, face, and gait vectors in one similarity space. Do not mix model versions.

Use cases Management handles from these records:

- same-camera track repair
- cross-camera candidate matching
- person journey
- frequent visitor grouping
- Ask Guard visual search
- “show earlier”
- “monitor this person”
- incident evidence linking

### 5.8 Face Recognition: Face-Only

Face recognition must use face evidence only. It must not confirm a person using
body appearance, gait, clothing, zone, or track continuity.

Face-only recognition can emit:

- `face_seen`
- `face_recognized_event`
- `face_unknown_event`
- `face_quality_rejected`
- `face_enrollment_candidate_created`

Face sample durable payload follows the existing V18-compatible face sample idea:

```json
{
  "sample_id": "sample-face-001",
  "event_id": "rear_gate:face_seen:sample-face-001",
  "camera_id": "rear_gate",
  "track_id": "44",
  "observed_at": "2026-09-24T10:00:00Z",
  "model_id": "face-embedding-model-v1",
  "dimensions": 512,
  "embedding": [0.01],
  "quality": 0.72,
  "artifact_id": "artifact-sha256-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "artifact_sha256": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Management result semantics:

- high-confidence enrolled face match: recognized candidate/person
- low-quality face: do not recognize; store/reject based on policy
- no face visible: no face recognition result
- body/gait match to a known person: cross-camera candidate only, not face
  recognition
- similar clothing: never confirmed identity

The UI language should be careful:

- Face-only match: “Recognized as X” if threshold and policy pass.
- Body/gait/Re-ID match: “Possible same person” or “candidate match.”
- Unknown: valid result, not a failure.

Management controls gallery revisions, enrollment approval, deletion, access,
and audit. Deleting a person from the face gallery does not automatically delete
historical biometric observations unless retention policy says so.

### 5.9 Violence Detection

Events:

- `violence_suspected_event`

Payload:

- suspicion confidence
- camera
- start/end time
- subject boxes/tracks if available
- event frame
- short clip reference
- model id/version
- uncertainty wording

Management opens a reviewable urgent incident. The product should say suspected
aggression/violence, not proven violence. Operator can escalate, correct
classification, close as false alert, or record reviewed uncertainty.

### 5.10 Health And Evidence Events

Events:

- `camera_online`
- `camera_offline`
- `stream_degraded`
- `visibility_lost`
- `evidence_unavailable`
- `storage_pressure`
- `artifact_upload_failed`
- `artifact_upload_retried`

Management uses these for health dashboard, recheck outcomes, evidence gaps,
pilot reporting, and trustworthy user messages.

## 6. Recheck

Recheck is a Management workflow. Edge should not own the business meaning of a
recheck.

### 6.1 Passive Recheck: V1 Recommendation

Edge continuously emits lifecycle and health events:

- `object_presence_update`
- `object_exited_zone`
- `object_visibility_lost`
- `camera_offline`
- `camera_online`
- `evidence_unavailable`

Management schedules recheck internally. At the due time, it reads latest
state/events for:

```text
presence_id + camera_id + zone_id + object_type
```

Possible recheck results:

- `still_present`
- `departed`
- `visibility_lost`
- `camera_offline`
- `evidence_unavailable`
- `stale_no_recent_event`
- `unknown`

Only `departed` means confirmed departure. `visibility_lost` and
`stale_no_recent_event` remain uncertain.

Example:

```text
10:00:00 edge emits object_entered_zone
10:02:00 edge emits object_presence_update duration=120
10:02:00 Management opens parking incident
10:02:30 Management recheck due
10:02:31 latest edge state says duration=151
10:02:31 Management records recheck result still_present
```

### 6.2 Active Recheck: Later Option

If needed later, Management can send a one-time command:

```json
{
  "command_type": "recheck_presence",
  "command_id": "cmd-778",
  "camera_id": "rear_gate",
  "zone_id": "gate_no_parking",
  "presence_id": "rear_gate:gate_no_parking:presence-abc123",
  "requested_at": "2026-09-24T10:02:30Z"
}
```

Edge responds:

```json
{
  "event_type": "recheck_result",
  "timestamp": "2026-09-24T10:02:31Z",
  "camera_id": "rear_gate",
  "payload": {
    "command_id": "cmd-778",
    "presence_id": "rear_gate:gate_no_parking:presence-abc123",
    "zone_id": "gate_no_parking",
    "result": "still_present",
    "duration_seconds": 151,
    "evidence_status": "available"
  }
}
```

Do this only if passive recheck is insufficient.

## 7. Feature Matrix

| PRD Feature | Desired State Edge Needs | Edge Events / Records | Evidence | Management Handling / Output |
|---|---|---|---|---|
| F01 ANPR | ANPR zone, min confidence, event frame/crop settings | `plate_read`, `unknown_plate_vehicle_seen` | event frame, vehicle crop, plate crop, OCR confidence | plate search, watchlist, plates of interest, vehicle history, alert rules |
| F02 Vehicle counting | vehicle count zones | `vehicle_count_per_frame` | frame, object boxes, zone | current occupancy, threshold rules, dashboard count |
| F03 Vehicle dwell | waiting zones, zone lifecycle enabled | `object_entered_zone`, `object_presence_update`, `object_exited_zone`, `object_visibility_lost` for vehicles | entry/update/exit frame, crop, optional plate | current wait, completed wait average, missing observation handling |
| F04 Prohibited parking | no-parking zones | same vehicle zone lifecycle events | frame, vehicle crop, optional plate | grace period, incident, passive recheck, call/escalation |
| F05 People counting | people count zones and optional lines | `people_count_event`, `person_crossing` | frame, person boxes | current people count, entry/exit totals, threshold rules |
| F06 After-hours activity | armed area geometry only | `object_seen`, `object_entered_zone`, presence updates for person/vehicle | frame, crop, track/presence id | schedule match, incident, alert/call, recheck, escalation |
| F07 Perimeter/loitering | restricted zones, lines, loiter zones | `object_entered_zone`, `line_crossing_event`, `object_presence_update`, `object_exited_zone`, `visibility_lost` | frame, crop, line/zone metadata | direction checks, loiter threshold, authorized pause, incident workflow |
| F08 Person Identity & Cross-Camera Matching | face/reid enabled, embedding spaces, face-only gallery revision | `face_seen`, durable face sample, `reid_observation` body/face/gait records | face crop, person crop, embeddings, event frame | face-only recognition, candidate matching, person history, review/correction |
| F09 Frequent visitors | no extra edge config beyond ANPR/ReID samples | repeated `plate_read`, `appearance_sample_created`/`reid_observation` | plate/person evidence over time | visit grouping, return monitoring, frequent visitor insights |
| F10 Violence detection | violence app enabled, zones optional, confidence setting | `violence_suspected_event` | clip, key frame, subjects, confidence | urgent review incident, escalation, false-alert correction |
| F11 Agent / Ask Guard | no special detector; needs searchable evidence and embeddings | existing events/records | frames, crops, clips, embeddings, event history | semantic search, investigation, findings, gaps, partial states |
| F12 Rules/workflows | edge does not receive rules, except observation geometry | observations only | evidence attached to observations | rules, active hours, schedules, workflow, rechecks, escalation |
| F13 Events/alerts/incidents | edge emits events, not incidents | all events above plus health | exportable evidence | incident queue, acknowledgment, assignment, resolution, export |
| F14 Setup/site health | camera source, apps, zones, lines, evidence settings | health/status events | diagnostics, missing artifacts | setup UI, camera health, sound/call/agent status, retention visibility |

## 8. Management State Derived From Edge

Management should maintain these product states:

- current zone occupancy by camera/zone/object type
- active presences by `presence_id`
- latest state per `presence_id`
- current tracks and candidate associations
- durable crossing totals by site timezone
- plate history
- person/vehicle visit history
- vector indexes per modality/model
- identity graph with confidence and review state
- incident table
- workflow execution table
- notification attempts
- recheck schedule and recheck results
- evidence catalog and artifact status
- camera health and storage pressure

Incident grouping keys:

- zone stay incident: `presence_id`
- line crossing incident: crossing `event_id` or rule-specific episode key
- plate incident: plate event plus rule episode window
- violence incident: violence event id plus clip interval
- repeated observations: attach to existing incident while episode is active

## 9. Management Output Examples

### 9.1 Prohibited Parking

Desired state:

```text
no-parking zone
vehicle detection/tracking
presence update interval
evidence capture
```

Edge:

```text
vehicle entered zone
vehicle presence update duration=120
vehicle presence update duration=150
vehicle exited zone or visibility_lost
```

Management:

```text
parking rule sees duration >= grace period
opens one incident for presence_id
alerts dashboard
calls guard if configured
schedules passive recheck
records still_present/departed/uncertain
resolves only when reviewed outcome exists
```

### 9.2 After-Hours Person

Edge:

```text
person entered rear_gate_restricted
presence updates
face/reid samples if quality allows
```

Management:

```text
checks active hours
opens after-hours incident
dashboard sound + voice call
recheck due
optional agent objective: establish what happened near gate
```

### 9.3 Person Search

Edge:

```text
appearance samples with body/face/gait embeddings
face_seen events
person crops and face crops
```

Management:

```text
stores vectors in separate indexes
answers "show earlier blue-shirt person"
returns candidate sightings with evidence
does not call body/gait match a confirmed identity
```

### 9.4 Face Recognition

Edge:

```text
detects face
quality selects best sample
sends face crop + 512D face embedding through durable path
optionally matches against local face gallery
```

Management:

```text
uses only face embedding/gallery for recognized identity
uses body/gait only for candidate association/search
records unknown when no face match passes threshold
controls enrollment and gallery revision
```

## 10. Edge Cases To Handle

### 10.1 Track ID Changes

- Continue same `presence_id` only with high-confidence reassociation.
- Emit `object_reassociated_in_zone` with previous/current track ids.
- If uncertain, emit `object_visibility_lost` and start a new presence.
- Management groups by `presence_id`, not `track_id`.

### 10.2 Occlusion And Disappearance

- Do not emit exit unless departure is observed.
- Emit `object_visibility_lost` when the object disappears behind obstruction,
  due to camera blockage, or due to tracker loss.
- Management must not resolve incident as departed from visibility loss.

### 10.3 Camera Offline During Incident

- Edge emits `camera_offline`.
- Management records recheck result `camera_offline`.
- Counts become unavailable, not zero.
- Incident remains open or reviewed as uncertain.

### 10.4 Evidence Missing

- Event may still be useful, but payload must say `evidence_status:
  evidence_unavailable`.
- Include `missing_artifact_kinds`.
- Management exports should show evidence gaps.

### 10.5 Duplicate Delivery

- Durable records must use stable `event_id`/`record_id`.
- Management deduplicates by `(edge_id, record_id)` or event id.
- Retries must reuse identical ids and bytes when possible.

### 10.6 Delayed Delivery

- Management uses `observed_at`, not received time, for reports and daily totals.
- Old queued events after restart should not become fresh alerts unless still
  relevant under the active rule.

### 10.7 Hot Reload

- Desired-state revision changes should be applied atomically.
- Existing active presences should be closed, migrated, or marked
  `configuration_changed` if zones/lines changed.
- Management should ask for review when a camera moves or zone geometry changes.

### 10.8 Multiple Objects In Same Zone

- Each object gets its own `presence_id`.
- If two objects overlap and tracker swaps are ambiguous, avoid auto-merge.
- Management should not merge separate presences without strong evidence.

### 10.9 Overlapping Cameras

- Edge events stay camera-local.
- Management performs cross-camera association with embeddings and time.
- Site-wide counts require careful Management logic; edge should not invent a
  global count.

### 10.10 Similar Clothing

- Body/gait/appearance can support candidate matching.
- Clothing alone never confirms identity.
- Face-only recognition must stay separate.

### 10.11 Low Quality Face

- Edge can emit `face_quality_rejected` or send no face sample.
- Management should display unknown/unavailable, not false negative identity.

### 10.12 Plate Uncertainty

- Do not fill missing characters.
- Emit confidence and raw OCR text if available.
- Unknown plate vehicles remain searchable as unknown vehicles.

### 10.13 Violence False Positives

- Event wording must be suspected/reviewable.
- Management must allow correction and hard-negative evaluation.

### 10.14 Agent Offline

- Local events/rules/rechecks continue.
- Ask Guard and agent investigation are unavailable.
- Partial findings are retained; retries are explicit.

### 10.15 Recheck Stale State

- If no fresh presence/exit/health event exists after recheck due time, result is
  `stale_no_recent_event`.
- Do not silently reuse an old frame as fresh proof.

## 11. Recommended Next Schema Additions

Add or formalize these contracts beside the V18 schemas:

- `zone-presence-event.schema.json`
- `person-crossing.schema.json`
- `appearance-sample-record.schema.json` or extend `management-record.schema.json`
- `violence-suspected-event.schema.json`
- `health-event.schema.json`
- `sentinel-desired-state.schema.json`

The first practical priority should be `zone-presence-event.schema.json`,
because it unlocks no-parking, dwell, loitering, after-hours activity,
restricted presence, and passive recheck.
