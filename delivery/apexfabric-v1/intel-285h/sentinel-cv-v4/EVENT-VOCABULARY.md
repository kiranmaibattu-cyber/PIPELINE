# Sentinel CV event vocabulary v4 — DRAFT FOR REVIEW

This is a **draft contract ready for CV-team review**, not an approved CV-image conformance claim. The CV producer reports facts; management owns identity, watchlists, rule thresholds, incidents, and decisions. No v4 rule may infer zero occupancy, departure, identity, or safety from an omitted event.

## Shape and shared types

One immutable v4 observation contains one event. Events are **flat objects** with a common envelope and fields selected by the literal `event_type`; they do not use a free-form `attributes` field for rule inputs. The intended step-2 JSON Schema is a closed, `event_type`-discriminated union (`oneOf`), with shared definitions for IDs, timestamps, subject references, and boxes. Unknown event types and unlisted semantic fields are invalid. Optional diagnostic metadata, if later allowed, must not affect rules.

Every event requires these envelope fields:

| Field | Type / units | Meaning and stability |
|---|---|---|
| `schema_version` | Literal string `"4.0"` | Selects this vocabulary; never used to reinterpret v2 payloads. |
| `observation_id` | UUID string | Globally unique event ID, generated once and unchanged on every retry. Each new count/dwell update has a new ID. |
| `deployment_id` | Nonempty string | Management-issued, stable for one CV deployment; the v4 bearer token must be bound to it. Not a subject identity. |
| `camera_id` | Nonempty registered camera key | Management-issued and unique within the site; stable across stream sessions. The token must authorize this camera. |
| `config_revision` | Integer ≥ 1 | Desired-state revision used to interpret zones, lines, and thresholds at event time. |
| `event_type` | One literal name defined below | The schema discriminator; no aliases or inferred mapping. |
| `observed_at` | RFC 3339 UTC timestamp with `Z` | Occurrence/measurement time, never HTTP arrival time. Subsecond precision is allowed. |

All camera-stream events except `camera_health` also require `stream_session_id`: a UUID generated for a single camera stream opening. Its value is globally collision-resistant, but its **semantic scope is one camera stream session**; reconnect or process restart starts a new session. `camera_health` permits `stream_session_id: null` when no stream is open.

A **subject reference** is the typed triple `(camera_id, stream_session_id, track_id)`, serialized using those top-level fields. `track_id` is a nonempty string assigned by the tracker, stable across observations of one continuous subject, and **never reused within that stream session**. It is not stable across sessions or cameras and is never a person ID. Subject events below require both session and track. Optional `presence_id`, available on `object_present`, `dwell`, `line_cross`, `zone_entry`, and `zone_exit`, is camera/session-scoped. It may preserve continuity when the CV edge reassigns a track after a short gap under its own time/body/spatial reassociation policy. This is a CV-side continuity aid, not a control-plane or global identity. The control plane may later associate subjects across cameras using reviewed evidence.

`zone_id` is a management-issued UUID, globally unique and stable as the zone's identifier; the `config_revision` identifies the geometry active when the event occurred. CV must echo this ID from desired state, not invent a detector-local zone name. A line is a zone whose stored kind is `line`. `threshold_ref` is likewise a management-issued UUID. `clip_evidence_id` and other evidence IDs are globally unique UUID strings, generated before the observation is submitted and immutable across retries. Evidence may be **pending** when an observation first arrives; it is not silently treated as present.

`bbox_normalized`, wherever required, is `{x1,y1,x2,y2}` with finite numbers in `[0,1]`, `x1 < x2`, `y1 < y2`. Coordinates refer to the full, unrotated source frame **before** any model resize or crop: origin top-left, x rightward, y downward. This removes the v2 pixel-versus-normalized ambiguity. All scores/confidences are finite numbers in `[0,1]` and are model outputs, **not calibrated probabilities** unless a model profile later says otherwise. Count fields are nonnegative integers. Durations and dwell thresholds are seconds; no milliseconds are implicit.

For fields named `as_of`, `transition_at`, or `crossed_at`, the value is a UTC timestamp representing the same instant as `observed_at`. The explicit name makes the meaning clear to consumers. `started_at ≤ observed_at`; a non-null `ended_at` must satisfy `started_at ≤ ended_at ≤ observed_at`. Out-of-order delivery is permitted; event IDs and occurrence times remain unchanged.

## Event definitions

The tables list fields **in addition to the required envelope**. “Subject ref” means the required top-level `stream_session_id` and `track_id` with the envelope `camera_id` as defined above. Every field listed as nullable is still present with a JSON `null` value. No missing value is interpreted as zero or false.

### `object_present` — baseline person/vehicle detection

This is the v4 equivalent of a v2 observation used for `presence_in_zone`: it keeps v2-like camera, time, object, box, confidence, and first-seen fields, but makes session/track and normalized geometry mandatory. Repeated detections use new `observation_id` values and the same subject reference.

| Required field | Type / units | Meaning |
|---|---|---|
| Subject ref | Session UUID + nonempty track string | Required continuous subject identity within one camera session. |
| `object_type` | Enum `person` or `vehicle` | Canonical subject class; no v4 detector-mapping guess. |
| `vehicle_type` | Enum `car`, `truck`, `van`, `bus`, `motorcycle`, `other`, or null | Required nullable; null for people or an unknown vehicle subtype. |
| `bbox_normalized` | Normalized box | Required valid subject geometry for control-plane zone evaluation. |
| `confidence` | Number `[0,1]` | Detection score. |
| `first_seen_at` | UTC timestamp | First positive sighting of this track in this stream session; unchanged on updates. |
| `zone_id` | UUID or null | Optional positive CV zone assertion; null if no zone applies. Control-plane polygon geometry remains authoritative. |

Optional `quality` is a `[0,1]` subject-crop quality score; optional `presence_id` obeys the session scope above. No absence of `object_present` proves exit.

### `person_count` and `vehicle_count` — measured zone counts

These have the **same fields and validation**, with `count` measured in persons or vehicles according to `event_type`. A count is a measurement, not automatically a reliable occupancy figure.

| Required field | Type / units | Meaning |
|---|---|---|
| `zone_id` | UUID | Management-defined polygon or full-frame counting area. |
| `count` | Integer ≥ 0, persons/vehicles | Measured value at `as_of`; zero is an explicit measurement, not missing data. |
| `as_of` | UTC timestamp | Measurement instant; equals `observed_at`. |
| `count_semantics` | Enum `current_occupancy` or `visible_now` | Occupancy persists across temporarily hidden subjects; visible-now is only an instantaneous frame/tracker estimate. These must never be combined as the same metric. |
| `counting_method` | Enum `tracked_occupancy`, `line_balance`, `frame_snapshot` | Method used to produce `count`. `frame_snapshot` cannot claim `current_occupancy`. |
| `counting_method_note` | Nonempty string, ≤ 256 characters | Human-readable method/assumption note, not a rule input. |
| `coverage_state` | Enum `complete` or `degraded` | Whether the configured area was adequately observed at `as_of`. Degraded counts remain stored but cannot be shown as definitive occupancy. |
| `entries_since_session_start` / `exits_since_session_start` | Integer ≥ 0 or null, subjects | Required nullable cumulative directional totals for this `(camera_id, stream_session_id, zone_id)`; both integers for `current_occupancy`, both null for `visible_now`. Never carry totals across a new session. |

V18.1 emits `visible_now` by default. Store and label it as a current visible count, **not** occupancy. The control plane uses `as_of`, `coverage_state`, `camera_health`, and a freshness policy before displaying any current count. If the stream is unavailable or the last count is stale, the value is **unknown**, not the last count and not zero. Directional totals can be reconciled with `line_cross` events without treating duplicate or restarted sessions as new entries.

### `dwell` — ongoing or completed zone stay

| Required field | Type / units | Meaning |
|---|---|---|
| Subject ref | Session UUID + track string | Same subject ref as its `object_present` events; stable only within the session. |
| `object_type` | Enum `person` or `vehicle` | Subject class. |
| `zone_id` | UUID | Polygon whose dwell is measured. |
| `started_at` | UTC timestamp | First **positive in-zone** observation for this dwell episode; constant across updates. |
| `ended_at` | UTC timestamp or null | Null when ongoing; positive observed exit time when completed. |
| `status` | Enum `ongoing` or `completed` | `ongoing` requires `ended_at: null`; `completed` requires a non-null `ended_at`. |

Elapsed dwell is computed as `observed_at - started_at` while ongoing and `ended_at - started_at` when completed, in seconds. A lost track, stale camera, or timeout must **not** emit `completed`; those cases remain unknown/ongoing with a coverage warning until positive evidence or an operator action resolves them.

### `threshold_exceeded` — advisory configured-bound crossing

| Required field | Type / units | Meaning |
|---|---|---|
| `threshold_ref` | UUID | Management-issued threshold configuration active at `config_revision`. |
| `metric` | Enum `person_count`, `vehicle_count`, `dwell_seconds` | Determines units: persons, vehicles, or seconds. New metrics require a vocabulary/schema update. |
| `zone_id` | UUID | Zone to which the metric and threshold apply. |
| `threshold_value` | Finite number ≥ 0; integer for count metrics | Configured bound in the metric's units. |
| `observed_value` | Finite number ≥ 0; integer for count metrics | Measured value in the same units. |
| `direction` | Enum `over` or `under` | `over` requires observed value > bound; `under` requires observed value < bound. Equality is not exceeded. |
| `as_of` | UTC timestamp | Measurement instant; equals `observed_at`. |

For `dwell_seconds`, subject ref and `object_type` are additionally required; for count metrics, `track_id` is forbidden. CV emits a new event only when a measured value crosses outside the configured bound, not once per frame. This is **telemetry, not permission to open an incident**: the control plane must verify `threshold_ref`, revision, source coverage, and its own active rule before acting. Threshold values are owned by management desired state.

### `plate_read` — visible plate text

| Required field | Type / units | Meaning |
|---|---|---|
| `plate_text` | Nonempty uppercase string, ≤ 32 characters | Readable characters in observed order; preserve leading zeros. No jurisdiction-specific validity claim or implicit watchlist match. |
| `confidence` | Number `[0,1]` | OCR score for this read, not identity probability. |
| `partial` | Boolean | True if any character or section could not be read; false only for a complete visible read. |
| `vehicle_type` | Vehicle-type enum above or null | Null if not known. |
| `bbox_normalized` | Normalized box | Plate region in the full source frame. |

The envelope's required `camera_id` and `observed_at` identify where/when the plate was read. `stream_session_id` is required; `track_id` may be present when the plate is associated with a tracked vehicle, but a plate read must not invent a track. An entirely unreadable plate is **not** a `plate_read`. Watchlist membership, repeat visits, and owner identity are control-plane decisions, not CV fields.

### `line_cross` — positive crossing of a configured line

| Required field | Type / units | Meaning |
|---|---|---|
| Subject ref | Session UUID + track string | The crossing subject; not a global identity. |
| `object_type` | Enum `person` or `vehicle` | Subject class. |
| `zone_id` | UUID | Must refer to a management-defined **line** in `config_revision`. |
| `direction` | Enum `forward` or `reverse` | Direction relative to ordered line endpoints and the side convention in desired state; not implicitly entry/exit. |
| `crossed_at` | UTC timestamp | Positive observed crossing time; equals `observed_at`. |

Emit one event per real crossing. The control plane maps `forward`/`reverse` to entry/exit only if the line configuration explicitly says so; a missing track cannot create a crossing.

### `zone_entry` and `zone_exit` — positive boundary transitions

| Required field | Type / units | Meaning |
|---|---|---|
| Subject ref | Session UUID + track string | Same scope as the subject's presence/dwell. |
| `object_type` | Enum `person` or `vehicle` | Subject class. |
| `zone_id` | UUID | Management-defined polygon. |
| `transition_at` | UTC timestamp | Positive entry or exit observation; equals `observed_at`. |

`zone_exit` is positive departure **from this zone**, not necessarily from the camera or site. Do not emit it merely because tracking stopped, frames were dropped, or the subject was occluded. An unavailable camera produces `camera_health`, not synthetic exits.

### `scene_sample` — scene-scoped sample for future search

| Required field | Type / units | Meaning |
|---|---|---|
| `stream_session_id` | UUID | Camera stream session of the sampled frame. |
| `zone_id` | UUID or null | Sampled polygon; null means the full source frame. |
| `evidence_id` | UUID | Exact JPEG evidence reference, generated before the Event; may be pending until uploaded. |

Optional `embedding_id` is the UUID of a **separately submitted scene embedding** through the normal observation → evidence → embedding sequence. It is omitted when only the frame sample is available; an Event may stand alone without a vector. When present, the embedding must use this Event as its parent and match that ID. No inlined vector/profile/dimension is duplicated in the Event; those fields remain in `embedding.schema.json` and must match the declared scene profile. This Event is stored for future semantic search (F11), not a V1 Guard Rule trigger.

### `fire_smoke_suspected` — typed hazard signal

| Required field | Type / units | Meaning |
|---|---|---|
| `hazard` | Enum `fire`, `smoke`, `both` | What the model suspects. |
| `score` | Number `[0,1]` | Model score, not confirmation. |
| `evidence_id` | UUID string | Frame or clip evidence reference; may initially be pending. |

`camera_id`, `stream_session_id`, and `observed_at` come from the envelope. This is added because the existing CV requirements ledger includes fire/smoke; it is not a substitute for an operator or safety-system confirmation.

### `camera_health` — coverage/freshness signal

| Required field | Type / units | Meaning |
|---|---|---|
| `as_of` | UTC timestamp | Health observation time; equals `observed_at`. |
| `state` | Enum `healthy`, `degraded`, `unavailable` | Whether this camera can currently support count/rule evaluation. |
| `reason` | Enum `none`, `stream_disconnected`, `decode_error`, `stale_frames`, `occluded`, `camera_disabled`, `other` | `none` only with `healthy`; otherwise a non-`none` reason. |

`stream_session_id` is nullable here. A health event cannot overwrite a later event by arrival order; consumers compare `as_of`. Health/freshness policy is management-owned. `unavailable` makes current counts and rechecks **unknown**, never zero or departed.

## Cross-event rules and unresolved decisions

- CV-generated `observation_id` and evidence IDs are immutable idempotency keys. A retry with the same ID and different bytes is a conflict. New measurements and state transitions get new IDs. Ordered delivery remains observation → evidence → embedding/scene; a referenced clip can show `pending` between the first two steps.
- Count/dwell/threshold events are recorded by occurrence time even if delivered late. A late event never rewrites a more recent current count or silently reopens a resolved incident. The control plane still decides incident grouping and late-action suppression.
- A person/vehicle `track_id`, a plate string, and an embedding similarity are **three different things**. None is a globally stable person/vehicle identity. Cross-camera association remains a reviewed management operation.
- V4 Events deliberately do not duplicate embedding dimensions or app/line desired-state configuration. Those are specified separately in the embedding-profile and desired-state schemas. The v4 implementation must preserve the reviewed per-profile fixed-dimension storage pattern.
- Violence detection and frequent-visitor identification/alerting are deferred from this v4 draft. Neither is an event type or CV-runtime responsibility here.

Questions for CV-team review before approval: Can the CV runtime guarantee **positive** `zone_exit` rather than just track disappearance? Does the line endpoint order plus side convention match its tracker geometry? Should CV emit `threshold_exceeded` telemetry at all when the control plane can compute the same bound from typed count/dwell events? These answers may narrow, but should not silently weaken, the required fields above.
