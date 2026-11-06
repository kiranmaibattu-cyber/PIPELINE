# sentry_cv

## Current multi-box branch: V8 recovery repair

On `sentry_cv_multi`, the latest published candidate is:

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:v8-r2
registry digest: sha256:e8baec1ad054312f0081deb4bf6d34c7c8cc3bfb38516a3582d3d2fed32b4629
```

This release repairs native source reconnection and adds verified recovery, external worker supervision and stage diagnostics. See the [V8 r2 release record](releases/v8-r2/RELEASE.md) and [diagnosis/log guide](MANAGEMENT_BLR_DECODER_STALL.md). Models and production gates are unchanged.

RTSPS defaults to the user-selected legacy-compatible behavior, matching dev3: encrypted connections without server-certificate verification. Strict mode remains available. See the [RTSPS repair and deployment instructions](model_updates/2026-11-05-rtsps-compatibility-fix/DEPLOYMENT.md) and [repair tests](model_updates/2026-11-05-rtsps-compatibility-fix/REPAIR_REPORT.md).

This is a tested development candidate with unresolved numerical fidelity,
absolute timestamp, thermal and memory findings. Read the
[validation report](model_updates/2026-11-05-qaft-int8/VALIDATION_REPORT.md) and
[release/source/model instructions](model_updates/2026-11-05-qaft-int8/RELEASE.md).
The latter identifies the exact source extracted from the immutable image,
the separate development experiments, and complete downloadable model and
validation archives. The V8 desired-state/local agent contract uses its
documented V7 remote wire format. The V6 image and delivery descriptions below
are historical single-box documentation, not this branch's current image.

Forward regression and edge-case coverage: [test plan and 282-case catalog](PIPELINE_TEST_PLAN.md), [machine-readable matrix](PIPELINE_TEST_MATRIX.json), and [execution ledger](PIPELINE_TEST_LEDGER.csv). The catalog is not a claim that every case passed. Executed coverage and incomplete/failed cases are recorded in [the execution coverage](REMOTE64_TEST_COVERAGE.md) and [the provisional isolated-host report](REMOTE64_TLSFIX2_VALIDATION_REPORT.md); that campaign remains deferred after the host freeze. Dedicated [5/8/10 FPS capacity and use-case protocol](CAPACITY_TEST_PROTOCOL.md), [profile matrix](CAPACITY_TEST_MATRIX.json) and [empty result table](CAPACITY_RESULTS_TEMPLATE.csv) keep scene enabled on every camera.

## Historical V6 snapshot

Sentinel CV V6 edge runtime snapshot for the Intel Core Ultra 9 285H
deployment profile.

This repository contains the present working CV image source, the V6
management contract/schema package, the related design notes, and the model
artifacts needed to build the runtime image without downloading models at
container startup.

## Current V6 Image Candidate

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.30-v6-dev39
image ID: d43d03ad200391a80f7f5038aca5fe0fed4481d2805bef8f610896b2e64a0cf6
local digest: sha256:aae3a9ed26a83b9e93958b74dcd02dde7a232ff85d1301f99014243dda9b0a29
publication status: published to personal GHCR
registry digest: sha256:aae3a9ed26a83b9e93958b74dcd02dde7a232ff85d1301f99014243dda9b0a29
```

This is the first candidate in this repository with a true V6 wire boundary.
Desired state, observations, evidence metadata, embeddings and acknowledgements
use schema `6.0`, delivery targets `/api/v6/ingest/...`, and new durable records
are stored under `/state/sentinel_v6`.

The image is built from:

- `docker/Dockerfile.sentinel-v6`
- `edge_runtime/solution_packs/sporada_secure/runtime_v6`
- `newdetails/sentry-v6`
- baked OpenVINO/ONNX model files under `models/`

Dev39 corrects timestamp provenance, records the exact parent runtime image,
hot-reloads geometry without restarting camera sessions, and prevents motion
quiet frames or tracker-retained predictions from creating false lifecycle
transitions. Small/distant subjects receive bounded additional recovery
hysteresis. Live qualification now covers face samples, line crossing, and a
real fire/smoke transition. Dev29 adds continuous evaluated-negative hazard clearing to the bounded policy
implemented in dev28. Dev28 added the adaptive production-policy resolver, time-based presence
recovery, positive zone/count debounce, conservative ANPR candidate selection,
spatially consistent hazard confirmation, and effective-policy telemetry in
`/healthz`. It preserves the strict V6.0 wire schemas. Dev27 replaces
manager-proxy detector tensor copies and one locked GPU request
with private per-camera mmap slots and a fair four-request OpenVINO scheduler.
Tracking, production policy, original-frame evidence, and V6 delivery remain
camera-local and ordered exactly as before. See
`SENTINEL_V6_DEV27_SHARED_INFERENCE_ARCHITECTURE.md` for the implemented and
deferred optimization boundaries. Scheduler admission is explicitly
non-blocking so a full request queue cannot delay completion callbacks. Dev25 synchronizes the canonical embedded desired-state example and removes
inactive V4 scene/ReID publishers so only the V6 event pipeline owns durable
Management records. Dev24 added MPEG-TS over TCP through the existing VAAPI pipeline and shares one
compiled vehicle/person GPU model across camera processes. Five camera IDs
consuming the same 1920x1080 TCP source held approximately 5 FPS each without
GPU resource rejection, native crash, or decoder reconnect. Camera-local
decode, motion gating, tracking, production policy and V6 delivery remain
isolated. Dev22 raised the V6 desired-state/configuration limit to 50 cameras, adds
credential-safe structured decoder/process logs, fault-aware per-camera health,
and durable delivery/outbox telemetry for observations, evidence, and
embeddings. It preserves schema 6.0 and `/api/v6/ingest/*`. The 50-camera value
is a configuration/supervision limit; throughput and memory still require a
declared resolution, FPS, and enabled-app workload.

Dev20 uses the dynamic-batch nine-class vehicle IR introduced in dev19 and
removes the unused `vehicle_yolo26n_fp16_dynamic.runtime.json` manifest. The
runtime loads only the model XML/BIN and keeps the class mapping at the tested
detector boundary. Dev19 replaced the static 80-class vehicle IR with the supplied dynamic-batch
nine-class vehicle IR. The current per-camera detector fixes batch 1 before GPU
compilation and uses the GPU's FP16 execution path without an explicit `f32`
hint. A future batching worker must compile a separate fixed-size model instance
for its selected batch. Dev18 remains the preceding published build. Both carry
`org.opencontainers.image.source` pointing at
`https://github.com/kiranmaibattu-cyber/sentry_cv`.

## Runtime Contract

Management mounts the desired state and camera URL secrets into the container:

| Path | Purpose |
|---|---|
| `/configs/desired_state.json` | V6 desired state from Management |
| `/run/secrets/sentinel/cameras/<camera-id>.url` | Camera source URL secret |
| `/state` | Persistent event/evidence/outbox state |
| `/tmp/sentinel` | Generated runtime files and temporary state |

Before upgrading from a 5.2-wire image, drain `/state/sentinel_v5_2`. A
partially acknowledged 5.2 record cannot safely be relabelled as V6.

The runtime exposes:

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Process liveness plus per-camera health, FPS, use-case latency/output rates, CPU/RAM, device placement, and accelerator workload telemetry |
| `GET /readyz` | Worker/config/model readiness |
| `GET /metrics` | Prometheus runtime, camera, use-case, resource, and emission metrics |

`/healthz` remains a liveness endpoint and returns HTTP 200 while the HTTP
process is alive; `ready` and `worker_running` inside its JSON describe runtime
readiness. FPS and rates cover the latest completed two-second window. Model
invocation wall time is explicitly separate from hardware utilization. NPU
utilization comes from the node's cumulative busy counter. GPU utilization is
left `null` when the node provides no compatible busy counter. CPU is reported
as cores consumed, with camera-process and container-cgroup scope separated;
container memory is not inferred by summing process RSS.

## Hardware Policy

This image is intended for Intel 285H edge nodes and expects accelerator device
access from the container runtime:

| Stage | Device |
|---|---|
| Decode | Intel iGPU / VAAPI |
| YOLO person/vehicle detection | Intel GPU |
| Fire/smoke detection | Intel NPU |
| Face detection | Intel GPU |
| SixDRepNet head-pose quality gate | Intel NPU |
| Face embedding | Intel NPU |
| Body ReID embedding | Intel NPU |
| Gait embedding | Intel NPU |
| Scene embedding | Intel GPU |
| Plate/OCR models | OpenVINO traffic model stack |

The intended deployment policy is no CPU fallback for decode or model
inference.

Face inference uses a CV-owned adaptive lifecycle. New and recovered tracks are
sampled rapidly, acquired tracks are checked every 3 seconds, and tracks with a
quality score of at least 0.75 are checked every 15 seconds. Event emission
still requires the fixed recognition-quality and material-improvement gates.

## Build

The build expects the Intel traffic runtime base image to exist locally:

```text
localhost/apexfabric-intel-traffic-runtime-base:intel-285h-2026.09.18-v2
```

Build the GHCR image:

```bash
SENTINEL_IMAGE_REPOSITORY=ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime \
SENTINEL_IMAGE_VERSION=2026.10.30-v6-dev39 \
./scripts/build_sentinel_v6_image.sh
```

Push it:

```bash
podman push ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.30-v6-dev39
```

## Contract And Design Docs

- `newdetails/sentry-v6/`: active V6 schemas, examples and contract.
- `Sentry_Edge_Inference_Design.md`: event production policy/design guidance.
- `V4_1_EVENT_PRODUCTION_POLICY_WORKING.md`: working policy notes used during
  the V6.0 alignment.
- `SENTINEL_V6_DEV_IMAGE.md`: image-specific implementation notes.
- `SENTINEL_EDGE_MANAGEMENT_CONTRACT.md`: management/edge contract notes.
- `SENTINEL_V18_1_ARCHITECTURE_AND_CONTRACT_DECISIONS.md`: history from the
  V18.1/V4/V5 design discussion.
- `SESSION_CONTINUITY_RECORD.md`: detailed chronology, current implementation
  truth, test evidence, repository rules, and unresolved work from the ongoing
  design and validation sessions.


Native reconnect repair: the published short tag is `v8-r2`, pointing to the tested `intel-285h-2026.11.06-v8-qaft-int8-reconnect2` image. The frozen-base build is [scripts/build_sentinel_v8_reconnect_image.sh](scripts/build_sentinel_v8_reconnect_image.sh); choose a new unused `SENTINEL_IMAGE_VERSION` when rebuilding. Recovery behavior, diagnostic event names, exact test scope and current publication status are recorded in [MANAGEMENT_BLR_DECODER_STALL.md](MANAGEMENT_BLR_DECODER_STALL.md).
