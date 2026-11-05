# V8 opt38 bounded native graph qualification

> Audit correction (2026-11-04): the16-camera8FPS raw report has `full_usecases=false`, only1.024 aggregate admitted inferenceFPS, and zero evidence/embeddings. The127.922FPS result below is not full-use-case capacity. See OPTIMIZATION_HANDOFF.md section27 and archived raw report for details. Other historical statements below have not all been revalidated.

## Candidate

- Image: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.03-v8-opt38-shared-secondaries`
- Local image ID: `90fab432b900882e79dd4cad5bc18c2756dd76dc77bdd231635389caf7bd5e24`
- V8 contract: `8.0`
- Production-policy digest: `95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03`
- Status: built and locally qualified; not pushed by this qualification.

## Implemented architecture

The runtime partitions the desired-state camera list into bounded workers of at
most five cameras. Each worker process owns exactly one parent `GstPipeline` and
adds one independent camera bin per camera.

Within a worker:

- camera bins share the process-local DL Streamer vehicle/person detector;
- camera bins retain independent VAAPI decoders, motion state and `gvatrack`;
- camera-local tracker output continues into the existing presence, zone, count,
  dwell, ANPR, identity, fire/smoke and scene production policy;
- stateless plate, OCR, body, gait and scene GVA executors are process-local
  shared engines with reference-counted camera handles;
- ANPR consensus, identity best-candidate state, gait sequences, presence IDs,
  timestamps, gates and deduplication remain camera-local;
- a camera reconnect removes and recreates only its camera bin.

A 50-camera desired state is therefore execution-partitioned rather than placed
under one Python interpreter. The current validated process bound is five.

The primary detector path remains in VA memory through DL Streamer inference and
tracking. The current production path still converts each admitted tracked frame
to BGR after `gvatrack` for policy, crop and evidence consumers. It is not yet a
selective evidence-only conversion path.

The detector uses batch 1 with the shared GVA request pool. Earlier batch-4
experiments did not improve this workload and were not retained in the candidate.

## Contract and policy invariants

The optimization does not change:

- V8 desired-state or observation schemas;
- `/api/v6/ingest/*` delivery-agent ownership;
- event, evidence or embedding identifiers;
- source-frame `observed_at` semantics;
- detector confidence, class mapping, NMS or source-coordinate boxes;
- camera-local tracker association;
- presence, zone, count, dwell, ANPR, identity, hazard or scene gates;
- durable handoff ordering or acknowledgement behavior.

## Qualification results

### Automated validation

| Check | Result |
|---|---:|
| V8 runtime tests | 29 passed |
| V7 regression tests | 83 passed |
| V6 regression tests | 78 passed |
| Source and embedded V8 contract validation | passed |
| Python compilation and `git diff --check` | passed |

### Exact-frame parity against opt28

Report: `parity_reports/v8-opt38-vs-opt28/report.json`

| Comparison | Result |
|---|---:|
| Detector strict parity | passed |
| Runtime strict parity | passed |
| Baseline/candidate decoded frames | 327 / 327 |
| Baseline/candidate inference frames | 14 / 14 |
| Baseline/candidate quiet frames | 313 / 313 |
| Baseline/candidate event packages | 17 / 17 |
| Missing/extra events | 0 / 0 |
| Field mismatches | 0 |
| Maximum frame delta | 0 |
| Scene embedding cosine minimum | 0.9999999999999998 |
| Exact evidence matches | 9 / 9 |

The sampled detector frames contained no detections, so this replay proves empty
detection parity plus exact runtime/event/evidence/scene parity. Positive object
execution was separately exercised by the live full-use-case tests below.

### Ten-camera direct comparison

Both images used ten independent local MPEG-TS/TCP replays at 8 FPS per camera.

| Metric | opt28 baseline | opt38 candidate |
|---|---:|---:|
| Final aggregate decode | 79.896 / 80 FPS | 79.971 / 80 FPS |
| Minimum final-three ratio | 0.9987 | 0.9983 |
| Healthy/recent cameras | 10 / 10 | 10 / 10 |
| Worker processes | 2 | 2 |
| Container memory | 3584.97 MB | 3421.60 MB |
| GPU utilization at final sample | 25.45% | 29.80% |
| Protocol errors / exhausted packages | 0 / 0 | 0 / 0 |

The candidate preserved throughput and reduced measured pod memory by about
163 MB (4.6%) in this workload.

### Sixteen-camera capacity

Artifact: `/tmp/sentry-v8-capacity-16.52krxuzp`

- 16 independent MPEG-TS/TCP sources at 8 FPS;
- four bounded native graph workers;
- final aggregate decode: 127.922 / 128 FPS;
- minimum final-three ratio: 0.9986;
- 16/16 cameras healthy and recent;
- container memory: 5725.36 MB;
- GPU utilization: 33.55%;
- 48 packages durable, with zero protocol errors, backpressure or exhaustion;
- no native crash or worker restart.

### Camera fault isolation

Artifact: `/tmp/sentry-v8-capacity-5.tjomnjmz`

Camera 3's MPEG-TS source was stopped and restored while all five cameras shared
one parent graph.

- camera 3 recorded one decoder error and one reconnect;
- camera 3 recovered under the same worker PID;
- cameras 1, 2, 4 and 5 remained healthy;
- the parent worker did not restart;
- final aggregate decode recovered to 39.930 / 40 FPS;
- delivery recorded no protocol error or exhausted package.

### Full-use-case live TCP

Artifact: `/tmp/sentry-v8-capacity-1.1q7f97ey`

- source: live MPEG-TS/TCP camera;
- final decode: 8.098 / 8 FPS;
- 40 handoff packages admitted and durable;
- package log contained 15 scene samples with frame evidence and SigLIP2
  embeddings, five object evidence packages, five TransReID embeddings and one
  GaitBase embedding;
- one real source interruption reconnected in the same camera worker;
- zero protocol errors, backpressure or exhausted packages.

### Full-use-case live RTSPS

Artifact: `/tmp/sentry-v8-capacity-1.6emk_vc9`

- final decode: 7.948 / 8 FPS;
- 25 handoff packages admitted and durable;
- scene evidence/SigLIP2, object evidence, TransReID and GaitBase outputs were
  delivered;
- GPU and NPU health telemetry remained available;
- zero protocol errors, backpressure or exhausted packages.

## Remaining qualification boundaries

- Sixteen cameras at 8 FPS is the highest load measured in this qualification.
  Schema acceptance for 50 cameras remains tested, but 50 simultaneous live
  camera capacity is not claimed by this report.
- The available live scenes produced body, gait and scene embeddings but no
  qualified AdaFace sample, confirmed plate read or positive fire/smoke
  transition. Those positive-case accuracy tests still require suitable labelled
  footage; absence of such events is not a model failure.
- A longer soak than the short capacity and reconnect runs is still required
  before a production release claim.

