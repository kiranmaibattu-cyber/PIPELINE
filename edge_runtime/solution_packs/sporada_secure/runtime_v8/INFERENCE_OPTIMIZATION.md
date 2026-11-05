# Sentinel V8 inference optimization

This document records the compatibility boundary for optimizing model execution
without changing camera-local tracking, presence, use-case policy, evidence, or
the V8 Management contract.

## Non-negotiable compatibility boundary

Every optimized inference path must return the same logical detector result to
the existing camera worker:

- camera ID and stream-session ID;
- frame ID and source observation timestamp;
- original source-frame dimensions;
- source-coordinate bounding boxes;
- class IDs/names, confidence values, and NMS behavior;
- real-observation versus prediction/skip state;
- per-camera frame order.

The following components remain camera-local and are not owned by a shared
model executor:

- tracker and track IDs;
- presence registry, recovery, and ambiguity decisions;
- zones, counts, dwell, and line crossing;
- ANPR candidate history and best-result selection;
- face/body best-candidate selection and gait sequence collection;
- fire/smoke temporal confirmation;
- scene-sampling schedule and pressure policy;
- event, evidence, embedding, and durable-delivery policy.

An optimization is not enabled by default unless it passes output parity,
ordering, timestamp, evidence, event, and live-load tests.

## Phase 1: vehicle/person detector batching

The shared GPU vehicle/person executor now supports bounded dynamic batches.
The controls are internal runtime controls and are not part of desired state or
the V8 contract:

- `SENTINEL_SHARED_VEHICLE_MAX_BATCH` (default `1`, hard maximum `4`)
- `SENTINEL_SHARED_VEHICLE_BATCH_WAIT_MS` (default `8`, hard maximum `15`)

Batch results are demultiplexed by camera and request ID. Each camera retains a
single outstanding request, preserving per-camera ordering. Old-session or
otherwise stale responses continue to be rejected by the existing request
identity boundary.

Intentional batch-collection delay is measured separately from scheduler
backlog. Adaptive pressure policy therefore does not misinterpret deliberate
batch waiting as GPU congestion.

### Benchmark decision

The same four frames were evaluated on the Intel GPU:

| Execution mode | Mean total | Mean per frame |
| --- | ---: | ---: |
| Batch 1, four asynchronous requests | 18.74 ms | 4.69 ms |
| Batch 2, two requests | 24.08 ms | 6.02 ms |
| Batch 4, one request | 22.06 ms | 5.52 ms |

Dynamic shape switching also caused occasional shape-specialization latency
spikes. Consequently, batch size one remains the production default. The
optional implementation is retained for controlled hardware-specific testing;
it is not evidence that batching should be enabled on this device.

### Validation completed

- Detector output remains `[1, N, 6]` for each camera.
- Source-coordinate detection behavior is unchanged.
- Four-camera batching separated results without cross-camera leakage.
- Batch-one mode uses the historical mmap path without an additional input
  copy and reports zero batch-collection delay.
- Ten TCP cameras were healthy in a live run at 60 aggregate configured FPS.
- Delivery admitted and durably handed off every sampled package with no
  observed backpressure or delivery errors.
- Tracker, presence, zone, dwell, identity, ANPR, scene, evidence, and V8
  contract code were not changed by this phase.

One camera-local startup exit was recovered by the existing supervisor during
the live test. Similar model-initialization exits existed before this change,
so it is tracked separately from batching.

## Phase 2: shared SigLIP2 scene execution (implemented, production default)

SigLIP2 model execution is fleet-shared by default. Source media PTS is now
anchored at decoder ingress, so downstream queue/model latency cannot move an
observation's source time. The camera process
continues to own:

- interval and event-triggered scene scheduling;
- full-frame versus geometry crop selection;
- source frame ID and observation timestamp;
- outbox-pressure sampling suppression;
- JPEG evidence generation;
- scene event and embedding payload construction;
- package priority and durable handoff.

The shared boundary accepts only the canonical float32 `1x3x224x224` tensor
and returns one 768-dimensional model result. Each camera has one private mmap
slot and one outstanding request. Request IDs prevent a restarted camera from
consuming a completion from an older process/session.

The internal runtime control is:

- `SENTINEL_SHARED_SCENE_INFERENCE=true`

The shared request count is bounded from one to four and defaults to two:

- `SENTINEL_SHARED_SCENE_REQUESTS=2`

Health telemetry separates total scene embedding time into
`scene_search.scheduler_queue_wait` and
`scene_search.device_inference` when the shared executor is active.

### Validation completed

- Local and shared GPU vectors for the same frame were exactly equal: cosine
  similarity `1.0` and maximum absolute delta `0.0`.
- Five TCP cameras shared one GPU SigLIP2 encoder and all produced scene
  samples without cross-camera response leakage.
- Fourteen sampled event/evidence/embedding packages were durably acknowledged
  with no backpressure, exhaustion, duplicates, or protocol errors.
- Five-camera container memory was approximately `1.99 GB` with one shared
  scene model versus `4.63 GB` with one model per camera under the comparison
  workload.
- Forced termination of one camera process changed its process and
  stream-session IDs, left all peers healthy, and resumed scene events after
  supervisor recovery. The shared scheduler remained healthy and 36 packages
  were acknowledged without duplicates.
- The V8 contract, event policy, tracker, presence state, gates, evidence
  semantics, and scene sampling rules were not changed.

### Production enablement decision

Complete-package replay compared immutable dev3 and optimized images across
ANPR, fire/smoke, crowd, and traffic recordings. Raw vehicle/person outputs
were exact for the same sampled pixels: class agreement, confidence delta
`0.0`, and bounding-box IoU `1.0`. Fire/smoke facts and ANPR read counts also
matched.

The replay additionally exposed an existing runtime timing sensitivity. The
camera worker timestamps raw FFmpeg frames when the synchronous processing
loop reads them. Scene execution time can therefore shift periodic scene
selection and adaptive face/body sampling by one or more processed frames.
Repeating the unchanged dev3 image against itself reproduced the same class of
count and identity-sample variation, so this is not a vector-computation error
in the shared executor. It does prevent a strict claim that shared execution
cannot influence policy output under load.

The decoder/timestamp phase now anchors source PTS before model execution, and
shared execution is the production default. Complete-package replay still
shows small frame-selection differences at asynchronous interval boundaries;
therefore package bytes are not claimed to be deterministic across separate
wall-clock replays. Event fields, source-coordinate detector results, and V8
wire validation remain the acceptance boundary.

## Phase 3: authoritative DL Streamer VA path

The `2026.11.03-v8-opt19-dlstreamer` image replaces the detector shadow with an
authoritative DL Streamer 2026.2 graph:

```text
camera input
  -> vah264dec/vah265dec
  -> NV12 VAMemory
  -> PTS-preserving drop gate at configured camera FPS
  -> gvamotiondetect
  -> Sentinel 4/14 confirmation, six-negative release and five-second sweep
  -> gvadetect (GPU, va-surface-sharing)
  -> gvatrack (zero-term)
  -> one selected BGR conversion
  -> existing camera-local V8 production policy
```

The vehicle/person detector no longer receives a host BGR tensor. Decode,
preprocessing and detector input remain in VA/GPU memory. Quiet buffers are
dropped before `gvadetect`, `gvatrack` and BGR conversion. The old native
RemoteTensor shadow remains in the repository as a qualification tool but is
disabled in this image.

`gvatrack` is authoritative for the local `track_id`. A metadata boundary
translates GVA ROIs to Sentinel detections and adds track hit/age fields without
performing a second association. Sentinel still owns presence confirmation and
recovery, zones, counts, dwell, ANPR history, identity sample selection,
fire/smoke confirmation, scene scheduling, evidence and V8 handoff.

After tracking, one BGR frame is produced only for admitted frames because the
remaining models and original-frame JPEG/crop generation still require pixels.
This is detector zero-copy and selective conversion; it is not a claim that
every downstream model is zero-copy.

The path is required by default:

- `SENTINEL_DLSTREAMER_ENABLED=true`
- `SENTINEL_DLSTREAMER_REQUIRED=true`
- `SENTINEL_GVA_TRACKER_TYPE=zero-term`
- `SENTINEL_ZERO_COPY_SHADOW=false`
- `SENTINEL_ZERO_COPY_VEHICLE=false`

Missing DL Streamer components or a failed graph now fail camera initialization
instead of silently changing detector/tracker behavior.

### V8 and production-policy boundary

No V8 desired-state, observation, evidence, embedding or acknowledgement
schema was changed. The embedded production-policy hash remains
`95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03`.
Collection, confirmation, emission, count debounce, presence stability, zone,
dwell, ANPR, identity, scene, fire/smoke and delivery policy remain in their
existing camera-local stages.

Changing from Sentinel centroid association to `gvatrack` can legitimately
change when a stable track first reaches downstream gates. Therefore a
separately timed old/new replay is not expected to have identical track IDs or
event frame numbers. Acceptance requires valid source-coordinate detections,
stable camera-local GVA IDs, unchanged downstream gate values and valid V8
packages—not byte-identical event timelines from two independent live replays.

### Qualification completed

- Image build validates `gvadetect`, `gvatrack`, `gvamotiondetect`, `gstgva`,
  the V8 schemas and the production-policy artifact.
- Unit tests: 23 V8 tests and 78 inherited production-policy tests passed.
- Recorded TCP replay: healthy at approximately 5 FPS; 61 decoded frames,
  45 inference frames and 16 quiet frames; 14 packages included linked JPEG
  evidence. The same GVA track ID reached presence and identity outputs.
- Live `tcp://192.168.1.92:9001`: healthy at approximately 5 FPS with DL
  Streamer VA decode/detection and GVA tracking.
- Live 1280x720 RTSPS: the DL Streamer path remained authoritative; a 107-frame
  run completed with zero decoder errors and zero reconnects.
- Quiet decode progress is processed independently of inference output. A
  quiet camera therefore keeps `/healthz`, lifecycle timers and last-frame age
  current without forcing inference or being mistaken for a decoder outage.
- Complete event packages retained evidence and embeddings through the existing
  bounded V8 handoff.

The current validation is one-camera functional qualification. Multi-camera
capacity and long-duration tracker/event accuracy still require the established
camera corpus and load tests before this tag is promoted as a release.

## Deterministic parity harness

The repository includes:

- `scripts/v8_detector_probe.py` for detector output on fixed recorded pixels;
- `scripts/v8_parity_harness.py` for baseline/candidate detector comparison and
  optional complete V8 package replay through a mock Unix-socket delivery
  agent.

The package harness retains the original manifests and JPEG bytes and compares:

- class, confidence, and source-coordinate bounding boxes;
- event type/count/timeline and policy fields;
- embedding cosine similarity by profile;
- evidence SHA-256 digests;
- durable observation/evidence/embedding acknowledgement chains.

The local `parity_reports/` directory is intentionally ignored by Git because
full evidence captures are large. The opt4 image-level detector corpus covered
ANPR, fire/smoke, crowd, Camera10, and Camera12 recordings. Across 333 matched
vehicle/person detections, baseline and opt4 had no missing or additional
detections, class mismatches, confidence changes, or bounding-box changes.

Representative invocation:

```bash
python3 scripts/v8_parity_harness.py \
  --baseline-image <immutable-baseline> \
  --candidate-image <candidate> \
  --video /recordings/camera-a.mp4 \
  --video /recordings/camera-b.mp4 \
  --output parity_reports/candidate-name \
  --detector-samples 40 --detector-sample-every 5 \
  --runtime --runtime-frames 150 --fps 5 --frame-tolerance 3
```

Strict full-runtime parity still requires a test-only finite-frame boundary or
source timestamps carried independently of processing. The current live replay
can process several additional frames between a `/healthz` poll and container
shutdown. Periodic scene samples are also wall-time scheduled. Reports must
therefore show raw frame totals and distinguish end-of-run/interval variation
from differences on matched source frames.

## Camera-local policy and evidence hardening

The responsibilities intentionally retained in each camera worker are active
implementation boundaries, not unfinished shared-service work. Regression
coverage now verifies that geometry crop selection, source timestamps, source
frame IDs, JPEG evidence, embedding-to-evidence linkage, and package creation
remain camera-local when the shared SigLIP2 executor is active.

Two local retention issues were also corrected without changing output:

- A delayed best-face candidate now copies only its expanded original-pixel
  face crop. A NumPy view no longer retains the complete source frame for the
  face-selection window.
- All ANPR bindings and candidates produced from one camera observation reuse
  one immutable original-frame snapshot instead of copying the same full frame
  repeatedly. Plate scoring, consensus, timestamps, bounding boxes, and emitted
  full-frame evidence remain unchanged.

JPEG encoding still occurs only when the production policy emits evidence.
Scene pressure decisions, event priority, delivery acknowledgement, and all
tracking/presence gates remain unchanged.

## Process-local DL Streamer sharing across camera groups

The opt27 runtime removes the remaining per-camera duplication of DL Streamer
model instances by placing a bounded group of camera pipelines in one process.
The production default is five cameras per process:

```text
fleet supervisor
  +-- camera group process 0
  |     +-- camera 01 GStreamer graph --+
  |     +-- camera 02 GStreamer graph --+-- shared model-instance IDs
  |     +-- ...                         |   for each stateless model kind
  |     +-- camera 05 GStreamer graph --+
  |
  +-- camera group process 1
        +-- camera 06 ... camera 10
```

DL Streamer `model-instance-id` sharing is process-local. Merely assigning the
same ID in ten separate camera processes does not share the compiled model or
request pool. The grouped architecture is therefore required to obtain real
cross-camera sharing without replacing the production GStreamer cascade.

Only stateless model execution is shared. Each camera still owns separate
pipeline objects for:

- `gvatrack` and its camera-local track IDs;
- presence recovery, zones, counts, dwell, and line crossing;
- ANPR candidate history and best-result policy;
- face/body candidate selection and gait sequence collection;
- scene scheduling and fire/smoke temporal confirmation;
- event/evidence/embedding admission and V8 handoff.

Structured logging context is thread-local so one camera cannot overwrite a
peer's camera or stream-session identity. Health aggregation deduplicates
resource accounting by PID; all cameras in one group report the same process
PID and identify the process as shared. Camera startup is staggered inside a
group to avoid simultaneous GPU/NPU model allocation spikes.

This changes the native-crash fault boundary from one camera to a bounded
camera group. A Python exception in one camera thread is surfaced and causes
the supervisor to restart that group. A native crash can therefore restart up
to five cameras, while other groups remain independent. The group size is an
internal runtime control, not a desired-state or V8 contract field.

### Opt27 measured result

Ten independent TCP inputs were split into two processes of five. All ten
cameras finished healthy at approximately `59.97/60` aggregate configured
decode FPS. The two unique camera processes used approximately `3.07 GB` RSS
and `2.68 GB` PSS in the final health sample. The run produced 121 V8-valid
packages with no Intel graphics-allocation abort, process crash, schema
rejection, or cross-camera model-output contamination.

The comparison opt26 per-camera-process run reached approximately `54.33/60`
decode FPS, reported approximately `9.8 GB` aggregate camera-process RSS, and
experienced one Intel GPU allocation crash. The accounting methods are not a
pod-cgroup memory comparison, but the unique-PID opt27 measurement removes the
known double counting that occurs when one shared process is reported once per
camera.

Two cameras with all use cases enabled also shared one process successfully.
ANPR/OCR, count, metadata bridge, event policy, fire/smoke, line crossing,
presence, body, face-pose, gait and scene stages executed. Face and gait
embeddings were not emitted during that short run because their unchanged
production gates did not select a qualified sample.

The V8 contract and production-policy hash were not changed. This is a runtime
resource-ownership optimization, not a change to event semantics or gates.
The completed tests are bounded functional/capacity tests, not an overnight
soak or proof of 50-camera capacity.

Subsequent capacity qualification established a measured curve through 64
aggregate configured FPS. Sixteen higher-motion cameras sustained 4 FPS decode
and inference each. Quiet, motion-gated tests passed at 21x3 FPS,
32x2 FPS and through 49x1 FPS. A 50x1 FPS cold start failed in Intel
`multi_graphics_allocation.cpp` and restarted multiple five-camera groups.
Accordingly, 49 is only the maximum demonstrated low-motion count; 50 is not
supported by this image. Exact results and workload limitations are recorded
in `OPT27_CAPACITY_REPORT.md`.

The subsequent opt28 dynamic-FPS correction removed the stale fixed 64-FPS
desired-state rejection. Camera FPS is optional and resolves to 8 when absent;
configured aggregate FPS is the sum of all resolved camera values and is
reported in `/healthz`. Nine cameras with FPS omitted resolved to 72 aggregate
FPS and sustained approximately 71.96 decode FPS, proving that the old guard
was not merely hidden in documentation.
## Production DL Streamer cascade

The authoritative camera graph now keeps decoded NV12 frames in VA memory for
motion admission, primary vehicle/person detection, GVA tracking and SCRFD
face detection. The fused face-alignment/AdaFace model runs on the NPU. Its
512D tensor is copied at the `gvaclassify` source pad before `vapostproc`
converts the selected source frame to BGR and drops raw tensor metadata.

An independent fire/smoke branch samples the same VA-memory decode at 2 FPS,
runs `gvadetect` on the GPU and converts only those sampled frames to BGR so
hazard evidence retains the exact source frame and PTS. Hazard observations
advance only the camera-local fire/smoke temporal policy.

The secondary analytics retain Sentinel's existing camera-local scheduling and
policy, while their model calls run through persistent DL Streamer
`appsrc -> gvainference -> appsink` adapters:

- original-frame vehicle crop -> plate detector on GPU;
- original-frame plate crop -> bounded asynchronous OCR on `MULTI:GPU,NPU`;
- original-frame person crop -> TransReID on NPU;
- qualified silhouette sequence -> lossless 5x6 montage wrapper -> GaitBase on
  NPU;
- policy-selected full frame/geometry crop -> static-shape SigLIP2 on GPU;
- presence, zones, counts, dwell, evidence and V8 handoff.

The adapters preserve the old model-facing interfaces (`detect`, `submit`,
`embed`, and `collect`). Therefore plate consensus, body/face best-candidate
selection, gait sequence qualification, scene cadence/backpressure, dedup and
emission remain unchanged. `SENTINEL_DLSTREAMER_SECONDARIES=true` enables this
path. The former fleet-shared SigLIP scheduler is not started while this path
is active, because the camera-local DL Streamer scene adapter owns that model
call.

Each secondary `gvainference` element has a distinct `model-instance-id`.
This is required by DL Streamer 2026.2: leaving the ID implicit allowed a later
element to reuse the first secondary model and expose the wrong output tensor.
The explicit IDs prevent plate, body, gait, scene and OCR tensors from
contaminating each other.

These secondary adapters currently accept policy-selected BGR crops. They use
DL Streamer for model execution but do not claim VA-surface zero-copy. The
primary detector, face cascade and fire/smoke branch remain the native
VA-memory paths. This split preserves original-frame crop/evidence semantics
and avoids moving camera-local temporal state into GStreamer.

The benchmark `gvapython` secondary branch is not copied into production. It
serializes all cameras behind one lock, downsizes source evidence to 640x360
and bypasses the production event policy. The production graph uses the same
models while preserving the V8 contract and policy boundaries.

### Full-cascade qualification

- All secondary adapters initialized together without cross-model tensor
  reuse: plate, OCR, body, gait and scene.
- TransReID output matched the direct OpenVINO path exactly on the parity
  input (cosine `1.0`, maximum absolute delta `0.0`).
- OCR logits matched exactly; the lossless gait wrapper matched the original
  GaitBase output exactly.
- Plate detection matched the direct path on 20 vehicle crops: 20/20 threshold
  outcomes, source boxes and confidences within `0.01`.
- One-camera MPEG-TS replay produced V8-valid scene, face, body and gait
  packages with linked JPEG evidence. Device placement reported the expected
  DL Streamer GPU/NPU paths.
- Against the prior `opt25-cascade` image, the same replay retained the same
  event families and evidence/embedding profiles. Throughput and memory stayed
  in the same range; scene embedding lifetime latency improved from about
  `20.3 ms` to `16.5 ms` in the sampled runs.
- Normal container stop completed with exit code 0 and no OpenVINO longjmp or
  Python resource-tracker warning.
- V8 contract/schema files were unchanged. The 23 V8 tests and 78 inherited
  production-policy tests passed when run in their isolated test suites.

