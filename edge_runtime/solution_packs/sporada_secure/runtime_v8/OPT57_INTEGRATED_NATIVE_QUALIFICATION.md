# Sentinel V8 opt57 integrated native qualification

Date: 2026-11-04 (Asia/Kolkata)

Update: subsequent busy-camera testing exposed throughput failures at two cameras
at both 8 and 10 FPS, a scene-inference missing-tensor error, and camera
timeouts. The quiet-scene results below remain workload-specific. See
[OPT57_PIPELINE_TECHNICAL_REPORT.md](OPT57_PIPELINE_TECHNICAL_REPORT.md) for the
current architecture, endpoint corrections, full test matrix and open gaps.

## Decision

`intel-285h-2026.11.04-v8-opt57-zero-term` is the qualified local candidate.
It integrates DL Streamer into the production V8 runtime without replacing the
V8 contract, the camera-local production policy, or adaptive gate ownership.

Local image reference:

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.04-v8-opt57-zero-term
image ID: c029bd7769a93f8b769da77b20c5acbc7a528e8f54a6cde74393a2639658c2a0
local digest: sha256:778703017d9c20515d171928e180e10b5b6f1a6cf7fa8b17590ae82021968691
size: 3,502,155,658 bytes
```

The image has not been pushed by this qualification task. A registry digest
must be recorded after the user explicitly requests a push.

## Runtime architecture

```text
RTSP / RTSPS / HTTP MPEG-TS / TCP MPEG-TS / file Secret reference
  -> camera-local source and reconnect state
  -> VAAPI H.264/H.265 decode
  -> NV12 VAMemory
  -> PTS-preserving configured-FPS drop gate
  -> DL Streamer motion metadata
  -> existing adaptive motion confirmation/release/safety sweep
  -> shared GPU vehicle/person gvadetect (VA-surface sharing, batch 1, nireq 8)
  -> camera-local gvatrack zero-term
  -> camera-local face cascade metadata
  -> selective BGR materialization only when downstream pixels are required
  -> camera-local V8 presence/zone/count/dwell/ANPR/identity/hazard/scene policy
  -> V8 package handoff to the delivery agent
  -> delivery agent /api/v7/ingest/* delivery ownership
```

One parent GStreamer graph contains independent camera bins. Stateless model
instances are shared in the process; tracker state, motion state, presence IDs,
zone episodes, OCR consensus, identity candidate history, gait sequences,
hazard confirmation, scene scheduling, adaptive policy, and event production
remain camera-local.

TCP MPEG-TS uses a permanent bounded `appsrc` graph. A small feeder copies only
compressed transport-stream bytes. Decode and primary detector preprocessing
remain in VA/GPU memory. A source outage therefore does not destroy a shared
GVA model instance used by peer cameras.

## Contract and policy invariants

The optimization preserves:

- the V8 desired-state and observation schemas;
- the V7 wire object formats embedded by V8;
- delivery-agent ownership of `/api/v7/ingest/events`, `/evidence`, and `/embeddings`;
- source-frame `observed_at` and `source_frame_id` provenance;
- camera-local `track_id`, `presence_id`, and zone episode semantics;
- object/zone/count/dwell/line/ANPR/identity/fire/scene gates;
- positive-observation requirements for entry/exit and hazard transitions;
- best-candidate and material-improvement selection for face/body/gait/ANPR;
- complete Event/Evidence/Embedding package handoff and idempotent IDs.

DL Streamer metadata is translated to the stable Sentinel `Detection` boundary.
It does not directly create Management events. Production policy remains the
only authority that promotes observations into durable output packages.

## Corrections made during integration

1. Replaced `gvafpsthrottle` with a PTS-preserving drop gate. The former paced
   every source frame and could make event time lag real time by 50-130 seconds.
2. Prevented the final appsink from dropping frames already admitted by motion
   and temporal policy. Every admission now reaches policy exactly once.
3. Added pending-admission, primary/fire rate-gate, and TCP feeder gauges.
4. Kept the fire model on the qualified bounded 320x320 BGR branch. Concurrent
   primary, face, and fire VA preprocessors caused Intel iHD
   `vaRenderPicture` allocation failures on this host.
5. Added permanent TCP MPEG-TS feeder/reconnect handling so one source outage
   does not remove shared GVA elements or crash peers.
6. Added credential-safe structured TCP connection/disconnection logs with
   camera ID, stream session ID, reason, backoff, and retry count.
7. Selected `gvatrack zero-term` instead of `zero-term-imageless`. On the fixed
   seven-sequence MOT17 evaluation, zero-term scored HOTA 31.44, IDF1 35.18,
   MOTA 26.15 and 463 ID switches, versus 30.56, 33.42, 25.93 and 537 for
   zero-term-imageless. It still consumes only detection-backed frames and does
   not inject predicted boxes into production policy.
8. Moved tracker tuning below the immutable Intel dependency layer in the
   Dockerfile so future policy qualification does not reinstall DL Streamer.

## Detector parity

The static batch-one DL Streamer primary model was generated from the same
dynamic OpenVINO IR used by the old runtime. Old V8 dev3 and opt56/57 detector
probes used the same recorded frames.

| Sequence | Samples | Matched detections | Class mismatches | Minimum IoU | Maximum confidence delta |
|---|---:|---:|---:|---:|---:|
| Camera12 recording | 40 | 0 (no detections in sampled frames) | 0 | n/a | n/a |
| MOT17-02 mapped replay | 30 | 199 | 0 | 1.0 | 0.0 |

Artifact: `/tmp/sentry-v8-opt56-detector-parity/report.json`.

The optimization therefore did not change detector classes, confidence, boxes,
letterboxing, NMS, or source-coordinate conversion for the evaluated frames.

## Final-image qualification results

### Capacity

Twenty independent TCP MPEG-TS replays, 4 configured FPS per camera, full use
case bindings:

- 80.167 / 80 configured aggregate decode FPS;
- 20/20 cameras healthy and recent;
- 27.914 aggregate admitted inference FPS after motion gating;
- 6,420.72 MB container memory;
- 3.44 CPU cores and 72.3% node GPU active residency at the final sample;
- 697 admitted and 697 durable packages;
- 5 bounded backpressure retries, zero exhausted, zero protocol errors;
- no fatal/native crash lines.

Artifact: `/tmp/sentry-v8-capacity-20.bnow569m`.

This qualifies 20 cameras at 4 FPS for the tested Intel 285H host and workload.
The schema accepts 50 cameras, but this report does not claim that 50 cameras
with every use case enabled are hardware-qualified.

### Ten-minute soak

Eight independent cameras, 4 FPS each, full use case bindings:

- final 32.192 / 32 configured aggregate decode FPS;
- all eight cameras healthy and recent throughout;
- memory warmed from about 4.06 GB and stabilized near 4.13 GB;
- final memory 4,135.04 MB;
- 1,673 admitted and 1,673 durable packages;
- zero backpressure, exhausted retries, duplicates, or protocol errors;
- zero native/fatal lines.

Artifact: `/tmp/sentry-v8-capacity-8.ii2dwxl0`.

This is a bounded ten-minute soak, not an eight-hour production soak.

### Camera isolation and reconnect

Five cameras ran in one shared graph. Camera 3's TCP sender was stopped for 12
seconds and restored:

- the target became `unavailable` with last-frame age, decoder error, reconnect,
  and retry status visible in `/healthz`;
- four peer cameras stayed healthy;
- `/healthz` and shared readiness remained active;
- camera 3 recovered in the same worker PID;
- final aggregate decode was 19.653 / 20 FPS;
- 164/164 packages were durable;
- no model-graph teardown or native crash occurred.

Artifact: `/tmp/sentry-v8-capacity-5.cy7oee88`.

### Use-case corpus

| Test | Result | Key outputs |
|---|---|---|
| MOT17 person sequence | PASS | 42 TransReID, 9 AdaFace, 1 GaitBase embedding; all durable |
| ANPR clip | PASS | 31 `plate_read`, 23 explicit `plate_outcome`, 171 evidence objects |
| Fire then normal scene | PASS | 2 suspected hazard episodes and 1 confirmed clear transition; suspected evidence present |
| Live TCP `192.168.1.92:9001` | PASS | 3.969/4 FPS; face/body/scene packages and evidence delivered |
| Supplied RTSPS path | EXTERNAL FAILURE | host/port reachable, but server returned RTSP `404 Not Found` for `/camera01` |

Artifacts:

- MOT/identity: `/tmp/sentry-v8-capacity-1.114i4pq2`
- ANPR: `/tmp/sentry-v8-capacity-1.zagcpcd3`
- hazard transition: `/tmp/sentry-v8-capacity-1.c538xa7l`
- live TCP: `/tmp/sentry-v8-capacity-1.cn09vwqj`

The corpus proves functioning paths and policy transitions. It is not a labelled
precision/recall claim for ANPR, identity, gait, or fire/smoke.

## Health and observability

`/healthz` remains the only health endpoint. It includes per-camera target,
decode, admitted inference, quiet and output rates; last-frame age; camera
state; reconnect/error fields; per-stage latency; output counts by event/use
case; DLS rate-gate and pending-admission gauges; TCP feeder counters; complete
handoff scheduler state; container CPU/RAM/PIDs; and measured node GPU/NPU
telemetry with explicit scope/semantics.

At the end of the final soak, every camera reported:

- `dls_pending_admissions = 0`;
- accepted/dropped PTS rate-gate counters;
- bounded TCP connections/disconnects/bytes;
- source-to-output event latency measurements;
- camera-local event and use-case counters.

The handoff scheduler reported no retained package backlog and no last error.

## Automated regressions

- V8 runtime: 32 passed;
- V7 regression suite: 83 passed;
- V6 regression suite: 78 passed;
- canonical V8 contract validator: passed;
- embedded image-schema validator: passed;
- image build-time model/plugin/schema checks: passed;
- `git diff --check`: passed.

## Remaining limits

1. Twenty cameras at 4 FPS is the measured ceiling in this report, not the
   theoretical maximum and not a guarantee for arbitrary scene density.
2. Zero-term improves MOT17 association quality but still has substantial ID
   switches in crowded/distant scenes. Presence recovery remains necessary.
3. Cross-camera identity remains a Management responsibility using face, body,
   and gait evidence; `presence_id` remains camera/session-local.
4. Scene embeddings are single-frame retrieval evidence and do not prove a
   temporal action.
5. The fire branch intentionally performs one bounded system-memory conversion
   for model stability; primary detection remains VA-surface-sharing.
6. The compressed TCP feeder is a byte-copy boundary, not a decoded-frame copy.
7. A longer site soak and labelled per-camera accuracy set are still required
   before declaring a universal production capacity/accuracy SLA.

