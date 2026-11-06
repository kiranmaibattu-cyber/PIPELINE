# Sentry optimization handoff — read before continuing

**Current state (2026-11-05): RTSPS compatibility repair complete. User explicitly chose legacy-compatible TLS by default, matching dev3 (encrypted, no server certificate verification); strict mode remains available. New image v8-qaft-int8-tlsfix2 / dbae... published to personal GHCR. 96 regression tests and 15 native TLS cases passed, plus full VAAPI/DLStreamer execution through an untrusted live TLS relay. Models, V8 contract and analytics gates unchanged. Prior QAFT fidelity/UTC/memory/capacity limitations remain. Read model_updates/2026-11-05-rtsps-compatibility-fix/DEPLOYMENT.md and REPAIR_REPORT.md.**


Forward testing: PIPELINE_TEST_PLAN.md / PIPELINE_TEST_MATRIX.json / PIPELINE_TEST_LEDGER.csv now define282scenarios across28areas, all fresh execution statuses NOT_RUN. This planning update does not change the published image or close known failures.

Updated: 2026-11-05, Asia/Kolkata. Current branch: `sentry_cv_multi`.

**Historical Opt79 checkpoint (section32; newer results below): User-authorized shared foreground background update implemented in local opt79 (source build default79). Four1080p cameras at8FPS passed a short test;1080p10FPS had window dips and1440p10FPS improved to7.3–7.5FPS but still failed. Opt76 remains frozen comparison image; opt78 gait worker remains OFF. This deliberately changes gait background learning/masks; do not reject or present it as legacy mask parity. No registry push.**

This is the continuation entry point for the current optimization work. It records the goal, decisions, implemented work, failures, evidence, current status and unfinished checks. Update this file as work progresses instead of creating another competing status narrative.

## 1. Goal and user requirements

Build a reliable, efficient Intel edge CV image using the useful DL Streamer components and architecture already available locally. It must process real camera workloads at the desired FPS, preserve useful tracking/identity continuity, produce correctly associated events/evidence/embeddings, and satisfy the V8 contract and the agreed production policy.

The user is explicitly concerned that optimization changes have drifted from intended behavior and that repeated architecture changes/builds have created confusion. Do not resume with another broad redesign, another list of already completed work, or another unsupported capacity claim. Diagnose the current failures against the frozen candidate and make small, evidence-backed changes.

Requirements retained from the conversation:

- V8 is the current contract. Do not revert to V5.2/V6/V7 runtime semantics or change schemas merely to simplify an internal optimization.
- The production policy determines quality gates, confirmation, association, deduplication, best candidates, event frequency and evidence selection. Management owns business rules, global/cross-camera identity and presentation.
- Preserve separate collection, confirmation and emission decisions. A model output is not automatically a Management event.
- Motion gating is built into the CV runtime, not a new desired-state feature toggle. Active scenes target configured scene cadence; quiet safety sweeps are approximately five seconds.
- Adaptive behavior matters across camera views. Verify that a resolved adaptive parameter reaches its actual consumer; reporting a value is not proof it controls native tracking/motion.
- Keep hardware decode and model device placement explicit. Do not silently fall back to CPU decode/inference to obtain a passing run. CPU tracking, crop/JPEG work and policy are expected.
- Count is visible-now occupancy, scoped to configured geometry or full frame as supported by bindings. Keep cumulative crossings separate.
- Track loss does not prove exit. Presence recovery must reject ambiguous/conflicting associations. Dwell depends on zone episodes and positive observations, with gaps retained as uncertainty.
- Face/body/gait samples belong to the same local presence when association is supported. Body/gait remain useful without a visible face; enrolled recognition is face-only.
- Use original source-frame pixels and correct coordinate transforms for face/person/vehicle/plate evidence. Contextual face evidence is wider than the actual face box.
- ANPR uses source-frame vehicle/plate crops, best candidates and guarded updates, plus explicit unsuccessful-read outcomes. Do not emit every OCR guess.
- Preserve source observation time and evidence/frame association; processing/upload time must not replace observation time.
- `/healthz` is the health endpoint; do not reintroduce `/healthy`.
- Work on the multi-box branch without disturbing single-box work. Do not push to the Apexflo organization unless explicitly asked. A later generic Git push follows the user's personal-repository preference; verify actual remotes/account before writing.
- Handoff creation and the subsequent upstream-repository review made documentation changes only. No runtime fix, build, commit or push was performed in those steps.

## 2. Current state — verified at handoff

Continuation update: section 14 records the subsequent upstream queue experiment and diagnostics. Opt57 remains the reference; opt58 failed qualification and its queue changes were removed. Opt59 is a diagnostic image, not a promoted replacement.

Latest continuation: section 15 records bounded plate lanes and shared-host tests. Opt60/61 are experimental only. Two lanes are **not enabled by default** and did not qualify higher end-to-end capacity.

Current diagnostic continuation: section 16 records native progress traces, a blocked native completion callback, bounded output admission and non-dropping TCP transport trials. Opt62–65 are local experiments, not published releases. Read the final results there before choosing an image.

| Item | State |
|---|---|
| Working repository | `/home/admin1/Documents/sentry_cv` |
| Branch | `sentry_cv_multi` |
| Candidate | `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.04-v8-opt57-zero-term` |
| Local image ID | `c029bd7769a93f8b769da77b20c5acbc7a528e8f54a6cde74393a2639658c2a0` |
| Local manifest digest | `sha256:778703017d9c20515d171928e180e10b5b6f1a6cf7fa8b17590ae82021968691` |
| Image size | 3,502,155,658 bytes |
| Overall status | Integrated candidate; unresolved busy multi-camera failures; not generally production-qualified |
| Source/model state | Many modified and untracked files; optimization work has not been committed/pushed in this task |
| Container state | `podman ps` showed no running containers at handoff; test containers were removed by the harness |
| Storage | Root filesystem had only approximately 1.6 GB available; check before another build |
| Hardware | Intel Core Ultra 9 285H, 16 logical CPUs, approximately 31.6 GB RAM, GPU/NPU |
| Publication | Local build only for opt57; a GHCR-looking local tag does not prove remote availability |

Do not run Git reset/checkout over the dirty worktree. Untracked runtime modules, model IRs, native libraries and test scripts are part of the working implementation; committing only tracked modifications would omit necessary files.

## 3. Contract and policy facts to retain

Canonical contract: `newdetails/sentry-v8/`. Embedded copies: `edge_runtime/solution_packs/sporada_secure/runtime_v8/image_schema/` and the image's `/opt/traffic-pilot/contracts/sentry-v8`.

- Desired-state contract/schema: `sentry-v8` / `8.0`.
- V8 retains wire schema `7.0` and uses local delivery-agent protocol `1.0`.
- CV submits a complete multipart package to `POST /v1/packages` over `/run/sentinel-delivery/agent.sock`.
- The agent owns durable SQLite storage, credentials, sequence allocation and uploads to `/api/v7/ingest/{events,evidence,embeddings,skip}` plus `GET /api/v7/ingest/stream`.
- CV memory queues are not a durable outbox. Only the agent's durable/duplicate acknowledgement establishes accepted production.
- The old `/api/v6/ingest/*` statement in earlier optimization reports was stale. It was corrected in the opt57 qualification document. Historical reports may still contain it.
- The production-policy specification is `SENTINEL_V6_PRODUCTION_POLICY.md`; its historical filename does not select contract V6.
- Policy file SHA-256: `95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03`.
- Profile names such as `transreid-ssl-v18.1` identify embedding spaces, not container/contract versions.
- Schema maximum 50 cameras and native grouping maximum 20 cameras/process are configuration/execution bounds, not measured hardware capacity.
- Existing schema/validator files differ from Git HEAD. Do not claim the whole optimization diff left schemas untouched without auditing those differences. Current intended baseline is the existing checked V8 files.

An unchanged policy-document hash does not prove complete behavioral equivalence. The tracker changed, execution cadence changed, and native adaptive connections still have gaps.

## 4. What is implemented now

```text
Per-camera input
  -> VAAPI decode -> NV12 VAMemory
  -> source tee
       -> PTS-based desired-FPS selection
       -> native motion votes + Sentinel temporal motion switch
       -> shared GPU gvadetect, batch 1 / nireq 8
       -> camera-local gvatrack zero-term
       -> SCRFD GPU / fused alignment-AdaFace NPU cascade
       -> metadata adapter / selective original-frame BGR access
       -> camera-local adaptive and production policy
       -> shared persistent plate/OCR/body/gait/scene executors as needed
       -> complete output package -> fleet handoff -> delivery agent

Source tee also feeds independent 2-FPS fire/smoke inference and local hazard policy.
```

Sharing: one parent GStreamer graph per worker, independent camera bins, shared GVA model instances and stateless secondary engines. Camera-local state includes motion, tracking, presence, zones/dwell, OCR history, face candidates, gait sequences, hazard confirmation and event selection.

Device/model placement in the candidate:

| Model/stage | Device / output |
|---|---|
| Decode/primary detector | VAAPI/GPU; static batch-one YOLO derived from dynamic IR |
| Tracker | CPU `gvatrack zero-term` |
| SCRFD | GPU; native eligible-frame interval 3, threshold .55, nireq 4 |
| Fused aligned AdaFace | NPU; 512D; nireq 4 |
| SixDRepNet | NPU direct OpenVINO; pose gate on selected face candidates |
| Plate detector | GPU shared `gvainference` |
| OCR | `MULTI:GPU,NPU` shared `gvainference` with async facade |
| TransReID | NPU; 384D |
| GaitBase | NPU; background-subtraction silhouette sequence; 4096D |
| SigLIP2 | GPU; 768D |
| Fire/smoke | GPU, 320x320 BGR branch, nireq 2 |

Each secondary engine has a worker thread, round-robin camera-owner queues, default one pending job per owner, `nireq=1` and a 30-second wait. Most calls block the camera loop waiting for results. This is a central area for diagnosis.

Primary uses real VA surface sharing; secondary appsrc BGR copies and fire's BGR branch still exist. Batch 1 plus eight requests is concurrency, not a batch of eight. The old RemoteTensor shadow and shared dynamic-batch experiments are not the authoritative primary path.

## 5. Work already done and failures encountered along the way

| Stage/change | Outcome / lesson to preserve |
|---|---|
| Per-camera model/context architecture, opt26 | Ten-camera test had approximately 9.8 GB aggregate RSS and an Intel graphics allocation crash. Source/process isolation alone did not solve resource duplication. |
| Shared native parent / bounded grouping | Integrated independent camera bins and shared model ownership. Earlier worker bounds were five; opt57 currently uses maximum 20. Do not use old opt38 bounds as current state. |
| Shared secondary executors | Reduced duplicate model instances; introduced/retained blocking and native-result error risks. Sharing alone is not proven busy-workload optimization. |
| Primary static batch-one model | Built from current dynamic IR. Direct old/candidate probes matched 199 MOT17 detections exactly. This was not a full native-path event-parity qualification. |
| `gvafpsthrottle` | Discovered to pace surplus source frames, causing stale processing and approximately 50-130-second event-time lag. Replaced with source-PTS drop selection. Do not reintroduce that pacing behavior. |
| Dropping final appsink | Could discard already-admitted frames after native motion/tracker state advanced. Changed to bounded backpressure with `drop=false`; added pending-admission gauges. |
| Fire VA preprocessing | Concurrent primary/face/fire VA preprocessing caused iHD resource failures. Retained bounded 320x320 BGR fire path for stability. |
| Camera-bin teardown on TCP outage | Could crash shared native model ownership or fail dynamic-pad recovery. Added permanent compressed MPEG-TS feeder/appsrc; reconnect preserves the shared graph. |
| TCP replay pauses | Read timeout raised to 30 seconds; decoder freshness still degrades sooner. Socket connection is not sufficient camera health. |
| Converter lifetime on reconnect | Fixed a retained-converter cleanup path that produced a `NoneType.convert` failure. |
| Structured logs | Added safe TCP connect/disconnect reasons, camera/session IDs and counters; credentials must remain redacted. |
| Tracker choice | opt57 changed zero-term-imageless to zero-term after historical MOT17 comparison. This is an actual association change, not transparent execution-only optimization. |
| Dependency-layer rebuild | Changing an early Docker ENV invalidated a large SDK layer and ran out of disk. Later override avoids reinstalling the dependency layer for tracker tuning. |
| Test/report claims | Quiet-scene capacity and basic output success were overstated as broad qualification. Later busy tests invalidate that broad conclusion. Preserve narrower evidence below. |

Intermediate local opt51 and opt54 image tags/layers were removed during earlier storage cleanup; source and test artifacts were retained. Do not assume every intermediate tag still exists. Inspect the inventory before selecting an old image.

Historical opt26 labelled ANPR evaluation reported 8 exact reads out of 71 plates (11.3% exact recall), and many cases with no terminal output. Later unlabelled opt57 plate-read counts do not establish that this accuracy/coverage failure was fixed.

## 6. Current measured evidence — keep passing and failing workloads separate

### Passing opt57 runs

| Workload | Result | Artifact directory |
|---|---|---|
| 20 quiet 1080p replay cameras at 4 configured FPS, 70-second steady test | 80.167 selected FPS, only 27.914 admitted inference FPS; 6.42 GB container RAM; all healthy | `/tmp/sentry-v8-capacity-20.bnow569m` |
| 8 quiet replay cameras at 4 FPS, ten-minute steady soak | 32.192 selected / 9.659 inference FPS; memory stable about 4.14 GB; 1673/1673 durable at sampled endpoint | `/tmp/sentry-v8-capacity-8.ii2dwxl0` |
| 5 cameras, one TCP sender stopped 12 seconds | Target became unavailable and recovered, peers healthy, same worker PID | `/tmp/sentry-v8-capacity-5.cy7oee88` |
| 1 live TCP camera at 4 FPS | 3.969/4 FPS; face/body/scene and evidence output | `/tmp/sentry-v8-capacity-1.cn09vwqj` |
| MOT17 identity replay at 4 FPS | 42 body, 9 face, 1 qualified gait embedding | `/tmp/sentry-v8-capacity-1.114i4pq2` |
| Unlabelled ANPR clip at 4 FPS | 31 reads, 23 outcomes, 171 evidence objects | `/tmp/sentry-v8-capacity-1.zagcpcd3` |
| Fire followed by normal footage | 2 suspected observations and 1 clear transition | `/tmp/sentry-v8-capacity-1.c538xa7l` |

These prove the exercised runtime paths/workloads. They do not prove all-camera continuous inference, labelled accuracy, full policy equivalence or overnight stability.

### Busy-camera tests — the current blocking evidence

Source: `/home/admin1/Downloads/Usecase-Catalog-main/videos/ANPR.mp4`, 1280x720, 10 source FPS, 22.3 seconds, looped through independent TCP replay senders. Full-use-case bindings were enabled. Camera/FPS settings changed; image and gates did not.

| Cameras x requested FPS | Final selected / inferred aggregate FPS | Status | Artifact directory |
|---|---:|---|---|
| 1 x 8 | 8.423 / 7.927 | Short test passed | `/tmp/sentry-v8-capacity-1.ozhimkhm` |
| 2 x 8 | 12.558 / 14.020 against 16 target | Failed; throughput dips, surface decoder timeout, one camera unavailable | `/tmp/sentry-v8-capacity-2.f70tlcem` |
| 1 x 10 | 10.473 / 10.473 | Short test passed | `/tmp/sentry-v8-capacity-1.z_054tpp` |
| 2 x 10 | 9.324 / 8.858 against 20 target | Failed; scene returned no tensor, branch restarted | `/tmp/sentry-v8-capacity-2.tosbn088` |
| 8 x 10 | 17.984 / 19.339 against 80 target | Failed; selected throughput approximately 18-42 FPS | `/tmp/sentry-v8-capacity-8.8yxhe3cl` |

The short runs used a 90% target threshold in the last three samples, not exact sustained-FPS acceptance. Rolling-window selected/inference counters can cross due to pipeline lag and sampling. Inspect source age and totals as well as the final FPS number.

**Current conclusion:** one busy camera passed short 8/10-FPS tests; two did not. This is a software/workload qualification gap, not proof the Intel host can only handle one camera. Do not extrapolate “80 FPS / requested FPS = supported cameras.”

## 7. Exact failure evidence and diagnosis starting point

Two-camera 10-FPS log: `/tmp/sentry-v8-capacity-2.tosbn088/container.log`.

Observed errors:

```text
CL_OUT_OF_RESOURCES
surface source interrupted; reconnecting: surface decoder timeout
camera_thread_unhandled_exception
RuntimeError: DL Streamer secondary produced no output tensor
multi_camera_branch_restart
```

Missing-tensor stack:

```text
surface_stream.run_surface_camera
  -> V8EventPipeline.process
  -> _scenes -> _scene
  -> DlsSceneExtractor.embed
  -> shared engine infer
  -> DlsRawImageInferencer._run -> _infer_direct
  -> frame.tensors() empty -> RuntimeError
```

Two-camera 8-FPS log records a surface decoder timeout. A camera loop blocked in secondary inference may stop consuming native output; therefore that timeout does not by itself prove the compressed source or VA decoder failed. A restarted camera can disconnect the harness's single-client FFmpeg sender, which then exits; later connection-refused messages can be a consequence rather than the initiating fault.

**Not yet established:** whether the initiating cause is GVA tensor metadata loss, concurrent native resource pressure, blocking/queue behavior, executor lifetime, or a combination. GPU active residency below saturation does not rule out resource allocation limits. Keep these as hypotheses until reproduced/instrumented.

## 8. Other known gaps — not fixed by writing reports

- HTTP/HTTPS URLs are accepted by validation but rejected by the required native `_probe_source()` path.
- Native motion properties are literal graph constants. Do not claim all are adaptively learned per camera.
- `gvatrack` does not receive the legacy centroid adaptive distance/disappearance parameters. Adaptive presence/recovery policy exists separately.
- Adaptive face candidate/pose scheduling does not suppress all native SCRFD/AdaFace computations; native cascade interval is separately fixed at 3 eligible frames.
- The adapter's `FaceSample.chip` is a resized face-box crop, not the exact internal aligned input tensor from the fused AdaFace model.
- Polygon scene crops are bounding rectangles, not polygon masks.
- Quiet scene samples follow available approximately five-second safety-sweep frames; active two-second scene cadence is not a quiet two-second guarantee.
- Source PTS is anchored to edge receive wall time. It is not necessarily burned-in camera time; cross-camera clock agreement remains unproven.
- Tracker identity switches/fragmentation remain. Historical zero-term MOT17 improvement is not full opt57 event/dwell/identity accuracy validation.
- The capacity harness uses a local capture/ack agent, not a complete production delivery agent plus remote Management service. It cannot establish remote durable-ingest behavior.
- Dependency packages are not all pinned. Rebuilding the same tag later may change image bytes; use immutable new candidate tags and record dependency/model identity.

## 9. Next work — ordered, concrete and bounded

1. **Freeze and preserve:** keep opt57 and current source/models as the reference. Check dirty/untracked files and free space before any operation. Preserve small failing reports/log excerpts if `/tmp` cleanup is planned.
2. **Reproduce the smallest busy failure:** use the exact two-camera 8/10-FPS configuration below. Record per-camera selected/admitted/completed counters, source age, pending admissions, model owner/job IDs, enqueue/start/finish times, output tensor presence and GStreamer bus errors.
3. **Locate the first blocking/error boundary:** distinguish healthy incoming compressed bytes/decode from a camera loop waiting on the shared secondary executor. Check result-to-request correlation, tensor lifetime, timeouts, shared engine references and native initialization/resource errors. Do not assume “more batching” or “bigger queues” is the fix.
4. **Make the minimum demonstrated fix:** preserve output formats, observation time, crop coordinates, model preprocessing and production gates. If async execution is needed, reject stale session/frame results and return results to their owning camera in order. A failed optional model must have explicit handling; do not silently turn it into a positive or negative observation.
5. **Verify the fix before scaling:** repeat the identical failing workload and compare error count, per-camera throughput/source age, queue delay, evidence association and event semantics. Build the next immutable image only around a concrete code change.
6. **Then establish busy capacity:** 1 -> 2 -> 4 -> 8 cameras at 8 and 10 FPS as results justify. Use measured per-camera rates and diverse clips; state resolution, active fraction, enabled profiles, secondary cadence and actual accepted inference FPS. Stop the sweep at demonstrated failures.
7. **Close behavioral gaps:** HTTP support, native adaptive tracker/motion wiring, face cadence/provenance, labelled ANPR coverage, count/dwell/recovery and identity associations. Each requires code-to-policy mapping and targeted evidence, not a schema-only pass.
8. **Release checks:** busy-scene soak, camera fault/restart, evidence/embedding completeness, actual agent/network restart behavior, canonical/embedded schema match, regression tests and archived results. Confirm the tested source/models are what is baked into the final image before publishing.

Checks still outstanding:

- [ ] Busy two-camera failure understood and fixed.
- [ ] No missing/crossed/stale secondary results or unbounded blocking.
- [ ] Per-camera 8/10-FPS capacity measured after correction.
- [ ] Actual native preprocessing/detection parity, beyond direct model probes.
- [ ] Positive zone/count/line/dwell/recovery timelines compared on the same source frames.
- [ ] Face/body/gait association and exact evidence frame/crop provenance checked under restart/load.
- [ ] Labelled ANPR precision, recall and terminal-outcome coverage rerun on latest candidate.
- [ ] Native adaptive controls verified at consumers.
- [ ] HTTP accepted inputs work end to end.
- [ ] Busy long soak and production delivery-agent/remote ingest tests.
- [ ] Commit/push completeness audited, including new model/wrapper/config files; publication only when requested.

## 10. Reproduction and regression commands

Busy failure reproduction:

```bash
cd /home/admin1/Documents/sentry_cv
python3 scripts/v8_capacity_probe.py \
  --image ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.04-v8-opt57-zero-term \
  --cameras 2 --fps 10 --port 18080 --startup-timeout 150 \
  --warmup-seconds 10 --steady-seconds 45 --sample-seconds 10 \
  --minimum-decode-ratio 0.90 --full-usecases \
  --recording /home/admin1/Downloads/Usecase-Catalog-main/videos/ANPR.mp4
```

Use `--fps 8` for the other failing case. Inspect `container.log` even when the harness labels a run PASS: its current fatal-pattern list does not cover every camera-thread/model exception.

Last regression results: V8 32 passed; V7 83 passed; V6 78 passed; canonical/embedded contract validators passed. These do not certify busy performance.

```bash
python3 -m pytest -q edge_runtime/solution_packs/sporada_secure/runtime_v8/tests/test_v8_runtime.py
python3 -m pytest -q edge_runtime/solution_packs/sporada_secure/runtime_v7/tests
python3 -m pytest -q edge_runtime/solution_packs/sporada_secure/runtime_v6/tests
python3 newdetails/sentry-v8/validate_contract.py
python3 edge_runtime/solution_packs/sporada_secure/runtime_v8/image_schema/validate_contract.py
git diff --check
```

Run only relevant checks while diagnosing; repeat broad suites when changes justify them. Avoid simultaneously running competing hardware capacity probes on the same GPU/NPU.

## 11. Code/document map and inputs

Runtime root: `edge_runtime/solution_packs/sporada_secure/runtime_v8/`.

| Location | Read/use for |
|---|---|
| `services/worker/pipeline/dls_secondary.py` | Shared executor queues, synchronous wait, GVA tensor extraction, scene missing-tensor stack |
| `services/worker/pipeline/surface_stream.py` | Native graph, source timeout, metadata consumption, selective conversion and camera policy loop |
| `services/worker/pipeline/dlstreamer_bridge.py` | TCP feeder, PTS/motion gates, face/GVA tracker metadata adapter |
| `services/worker/pipeline/multi_camera_gst.py` | Parent pipeline and camera-bin startup/restart/bus routing |
| `services/worker/stream_fleet_openvino.py` | Worker grouping and camera/thread restarts |
| `services/worker/pipeline/v8_events.py` | Presence/zone/count/dwell, ANPR/identity candidate selection, scene/hazard output |
| `services/worker/pipeline/adaptive_policy.py` | Resolved adaptive gates and pressure modes |
| `services/worker/pipeline/face_samples.py` | Face candidate scheduling and contextual evidence |
| `services/worker/pipeline/sentinel_v8_handoff.py` | Package admission, priority, counters, acknowledgement semantics |
| `docker/Dockerfile.sentinel-v8` | Image dependencies, models, effective ENV and build checks |
| `scripts/v8_capacity_probe.py` | Workload configuration/replay, sampled health, local capture acknowledgements |
| `scripts/v8_parity_harness.py` | Direct model and optional runtime comparisons; inspect actual enabled paths before claiming parity |

Detailed current report: [OPT57_PIPELINE_TECHNICAL_REPORT.md](edge_runtime/solution_packs/sporada_secure/runtime_v8/OPT57_PIPELINE_TECHNICAL_REPORT.md). Earlier [qualification report](edge_runtime/solution_packs/sporada_secure/runtime_v8/OPT57_INTEGRATED_NATIVE_QUALIFICATION.md) now links forward to its busy-test correction. opt26/27/38 reports are historical evidence, not current deployment instructions.

Reference report supplied by user: `/home/admin1/Downloads/dlstreamer-pipeline-technical-report.md`. It reports a 20-camera 10-FPS native benchmark with different face/secondary cadence and output work; it was not rerun during this report task.

`SENTINEL_MODEL_AND_PIPELINE_IMPROVEMENT_SYSTEM.md` is a proposed improvement-data architecture, not a completed optimization implementation. Do not start that separate feature while the current execution failures are unresolved.

Available recordings: `/home/admin1/Downloads/Usecase-Catalog-main/videos/`; quiet Camera12 recording: `/home/admin1/Downloads/camera_recordings_2026-10-12/Camera12_192.168.1.13_2min_h264.mp4`; MOT17 replay: `/home/admin1/Documents/IMAGE_TESTS/datasets/generated/mot17-e2e-4fps/MOT17-02-SDP-mapped-4fps.mp4`.

Previously supplied live inputs: `tcp://192.168.1.92:9001` through `:9010` (camera labels 01-08, 10, 12); `rtsps://gpu-box.burmilla-bass.ts.net:443/camera01`; older `rtsp://192.168.1.95:8554/{traffic1,traffic2,ch9}`. Recheck current availability. The last RTSPS attempt returned 404; do not cite it as a passing opt57 stream test.

## 12. Resume discipline

Read this handoff, inspect current Git/image/process state, then open the two-camera failing artifact and the executor stack. The next useful result is a demonstrated cause and bounded fix, not another image with a different architecture name. After each meaningful change, append the changed files, new image ID if built, reproduced failure, measured result and still-open checks here. Preserve distinctions between implemented code, a proposal, a schema pass, a runtime path pass, an accuracy pass and measured capacity.

## 13. Official DL Streamer repository review — 2026-11-04

User requested review of `https://github.com/open-edge-platform/dlstreamer` for useful optimizations. Review completed against the installed release `v2026.2.0`, resolved to commit `ad3f68259b650f1c1aaec694d80cdfcd1055045a`. Main was also inspected for discovery; it is an active development branch, not an instruction to upgrade the image. No runtime changes or performance retests were made in this review.

### Findings tied to our failures

1. **Queue placement is a concrete first experiment.** The official [multi-stream sample](https://github.com/open-edge-platform/dlstreamer/blob/ad3f68259b650f1c1aaec694d80cdfcd1055045a/samples/gstreamer/gst_launch/multi_stream/README.md) recommends queues after inference stages. Opt57 queues before primary inference, but connects primary -> tracker -> face detection -> face embedding -> appsink without equivalent intervening queue boundaries. Audit the secondary engine's inference -> appsink boundary too. Test bounded, non-leaky queues after inference elements and measure callback stalls/queue occupancy. Queues can decouple threads; they do not remove overload or justify dropping admitted frames.

2. **An output buffer is not proof of successful model execution.** Release [inference_impl.cpp](https://github.com/open-edge-platform/dlstreamer/blob/ad3f68259b650f1c1aaec694d80cdfcd1055045a/src/monolithic/gst/inference_elements/base/inference_impl.cpp) registers `PushFramesIfInferenceFailed` as an error callback; it forwards the affected buffer downstream. `PushOutput` also uses shared output synchronization. This provides a plausible mechanism for our missing-tensor result after a native error. Correlate model/job/PTS and upstream native logs before blaming Python metadata extraction. It does not yet prove which error caused our failure.

3. **Current secondary submission defeats request concurrency.** Local `_run()` submits one job and waits for its result; the camera caller also waits. Increasing `nireq` alone cannot overlap jobs that the wrapper never submits concurrently. A later executor change needs bounded outstanding jobs, result identity, timeout cancellation and camera/session ownership. The native source's completion path is useful implementation evidence; camera-local event decisions must retain their existing association and ordering guarantees.

4. **Preprocessing has an iGPU-specific tuning option.** The official [performance guide](https://github.com/open-edge-platform/dlstreamer/blob/ad3f68259b650f1c1aaec694d80cdfcd1055045a/docs/user-guide/dev_guide/performance_guide.md) identifies `va` as a useful integrated-GPU backend and explains the compute cost of surface-sharing preprocessing. Compare `va` against our `va-surface-sharing` using identical pixels/letterboxing/model output. This is an A/B candidate, not proof it is faster or that our earlier fire-resource issue is fixed.

5. **Batch waiting requires version-specific verification.** The installed [gvainference properties](https://github.com/open-edge-platform/dlstreamer/blob/ad3f68259b650f1c1aaec694d80cdfcd1055045a/docs/user-guide/elements/gvainference.md) say `batch-timeout` is unsupported with VA backends. Do not promise bounded VA batching by merely setting that property. `no-block` skips inference and is deprecated; it cannot transparently preserve production observation coverage. Inspect `scheduling-policy` separately from device/request-count tuning.

6. **Mux/demux are already available, but parked for now.** `gst-inspect` in opt57 confirmed both elements. The release [mux documentation](https://github.com/open-edge-platform/dlstreamer/blob/ad3f68259b650f1c1aaec694d80cdfcd1055045a/docs/user-guide/elements/gvastreammux.md) describes source tags, bounded queues and partial scheduling. Passthrough requires identical caps, can drop late buffers, and PTS-normalization modes can rewrite timing. It is not equivalent to tensor batching. Any trial must retain original event time, demux before camera-local tracking, and handle quiet/stalled cameras. Adding it is not necessary for diagnosing the current two-camera secondary failure.

7. **Native profiling can locate the missing time.** Use the official [latency tracer guide](https://github.com/open-edge-platform/dlstreamer/blob/ad3f68259b650f1c1aaec694d80cdfcd1055045a/docs/user-guide/dev_guide/latency_tracer.md) with explicit capability checks. Opt57's registry advertised GStreamer's core `latency` tracer, `gvafpscounter` and `gvametaaggregate`, but not the named DLS `latency_tracer`. Native traces must be correlated with Python queue/stage times. Sanitize source URLs and limit tracing to the smallest reproduction.

### Next sequence refined by this review

Keep the two-camera failing workload as the acceptance case. First capture native errors and per-stage/queue timing. Then A/B bounded post-inference queues as one controlled change. If camera waits persist, trace and repair secondary submission/completion/error handling. Evaluate preprocessing and batching only after the failure path is understood. Keep mux/demux as a separate optional experiment. None of these proposals is implemented yet, and none establishes new camera capacity.

## 14. Authorized continuation: queue A/B and secondary diagnostics

User said “ok so proceed” following the upstream review. Ran three sequential two-camera tests using the same busy ANPR recording, 10 requested FPS/camera, 10-second warmup, 45-second steady period, 10-second samples and 90% acceptance threshold. No competing capacity tests were run. No contract, production gates, model files, preprocessing, devices, tracker or motion settings were changed in this continuation.

| Run | Artifact suffix under `/tmp/sentry-v8-capacity-2.*` | Result |
|---|---|---|
| Frozen opt57 rerun | `v0_8nznf` | Failed sustained throughput; final selected/inferred 19.551/20.026 aggregate FPS, but an acceptance window fell to 11.135 selected FPS. Both cameras healthy at the end. Native missing-tensor crash did not recur. |
| opt58 bounded queue experiment | `p6kfmmdf` | Failed; final selected/inferred 18.794/15.799 FPS. Added non-leaky two-buffer queues after primary, face detection, face embedding and secondary inference. Final pending admissions 21/13 versus baseline 9/11. No demonstrated improvement; queues removed from working source. |
| opt59 diagnostic run | `why5tnh1` | Failed throughput; captured secondary owner/job timing. No missing-tensor/native crash in this run. Logging itself adds overhead; this is diagnostic evidence, not a speed comparison. |

Local-only experimental images, built as tiny overlays on the exact opt57 image to avoid dependency/model drift and unnecessary disk usage:

- `localhost/sentry-v8-opt58-queue-experiment`, image ID `00a2caf5f27da4edbd6aba399463681caa69eea94db4bf9d8ee23761bc2db44e`. Rejected experiment, not a release.
- `localhost/sentry-v8-opt59-secondary-trace`, image ID `84f3e12920bb058b90ef67dad34b476c418eaa38b916076f3c59433a15e4a3ed`. Diagnostic only. Test additionally supplied `--container-env SENTINEL_DLS_TRACE_JOBS=1`.

Retained code changes:

- `dls_secondary.py`: optional structured per-job diagnostic records with camera owner, model, executor sequence, queue wait, execution wall time and success/error type. Trace disabled by default; failures logged. Executor sequence is diagnostic identity, not source-frame ID. No inference scheduling change yet.
- `surface_stream.py`: separates plate scheduler wait from executor wall time in existing health stage metrics. These wall times do not claim pure GPU kernel duration. The working source subsequently tightened the diagnostic guard to `dls_secondaries`; archived opt59 contains the prior equivalent `hasattr(plate, "engine")` guard. Do not claim source and experimental image are byte-identical.
- `v8_capacity_probe.py`: fatal scan now also catches `CL_OUT_OF_RESOURCES`, camera-thread exceptions and missing secondary tensors; previous scan could overlook these errors.

Trace findings, excluding each model's first two submitted jobs (this is not a universal warmup guarantee):

| Model | Jobs | Mean queue wait | Mean executor wall time | Executor p95 |
|---|---:|---:|---:|---:|
| Plate detector | 4773 | 3.818 ms | 8.744 ms | 28.944 ms |
| OCR | 1536 | 2.303 ms | 23.421 ms | 43.420 ms |
| Scene | 55 | 0.099 ms | 23.173 ms | 36.220 ms |
| Body | 47 | 0.079 ms | 14.552 ms | 16.332 ms |

Baseline cameras each performed approximately 2,535 plate calls across 448/491 processed frames; their mean full frame-processing spans were 104.5/95.5 ms. Sequential per-vehicle calls therefore consume a substantial fraction of the 100-ms budget. This supports investigating plate execution overlap before a broad graph rewrite. It does **not** prove the initiating cause of the earlier OpenCL/missing-tensor failure. Scene queue contention was negligible in this diagnostic run; do not describe it as the demonstrated bottleneck.

Validation: 32 V8 runtime tests passed; canonical and embedded contract validators passed; `git diff --check` passed. Policy SHA remains the section-3 value. These checks do not establish complete event parity or busy production readiness. Captured outputs passed the harness's minimum use-case checks, which are weaker than labelled accuracy/evidence-provenance qualification.

Persistent evidence is now in `optimization_evidence/2026-11-04-upstream-queue-trial/`: report JSON, final health, compressed logs/package summaries, aggregate timing comparison and exact experimental build contexts. This avoids relying solely on `/tmp`. Builds were not published or committed.

**Next bounded task:** investigate overlapping the existing plate model jobs with a bounded executor while retaining per-camera frame/result correlation, original pixels and all candidate gates. First specify and test cancellation, timeout, stale-session and failed-tensor behavior; current single-submit/single-wait executor must not be made concurrent without these protections. Compare against the archived opt57 run on identical input and also test the native error path. Do not reduce ANPR collection cadence or acceptance gates to manufacture higher FPS. No capacity increase, crash fix or general production qualification is claimed by this continuation.

## 15. Plate concurrency trial on the shared host

User authorized continuation and explicitly clarified that other programs are running: test with currently available resources, without assuming an isolated machine. No unrelated program was stopped or reconfigured. All CV tests ran sequentially. Passive host monitoring recorded roughly 16.5–20.0 GiB available RAM, approximately 13.7% mean host CPU and a 41.3% peak over the observation window, including gaps between tests. Swap already held approximately 5.5 GiB before these runs. These are host observations, not reserved CV resources. GPU/NPU readings remain node-wide. External accelerator contention and native allocation limits are not excluded by spare RAM or modest overall CPU.

### Implemented experiment and safeguards

- Added internal `SENTINEL_DLS_PLATE_LANES`, bounded to 1–2 and **default 1**. Only plate inference can use two lanes; the other secondary models retain one. This is not a desired-state field or contract change.
- Each lane has a persistent appsrc/gvainference/appsink pipeline and its own output sink. Both use the same plate model-instance ID and matching input caps to share the GVA model. `nireq` matches the lane count. Camera-local collection/selection/policy stays synchronous and unchanged; no temporal history is moved into the shared executor.
- Engine-wide unique sequence numbers are carried in internal PTS. Returned PTS must match the request; older timed-out outputs are drained, future/missing/incorrect-sized outputs fail explicitly. Internal executor PTS does not replace source observation time.
- Caller timeouts mark jobs cancelled. Cancelled queued jobs are skipped; completed cancelled work is not returned. A retired camera handle rejects completion. This prevents assigning an old crop result to a newer request; it does not guarantee native driver recovery after a hang.
- Added optional rate-gate logging (`SENTINEL_DLS_TRACE_RATE_GATE=1`) without altering selection decisions. This trace is disabled by default.
- Capacity reports now retain per-camera rates, completion totals and pending admissions. They also retain the last health summary on startup failure and flag structured failed secondary jobs. These are test-harness improvements, not wire-schema changes.

### Images and exact source evidence

Both builds are small local overlays; model/dependency layers are the frozen opt57 layers. Neither was published or promoted.

| Image | Image ID | Role |
|---|---|---|
| `localhost/sentry-v8-opt60-plate-lanes` | `db6c7d964a1e61f52c7b0953999cada362668dfb7ae2cf39b833d954a9e1d783` | Bounded lane implementation, default one lane |
| `localhost/sentry-v8-opt61-rate-trace` | `1e21074d6cd420124e4703be9a0f93c9c6dbaac2cf02985474c0e3ca0584d6c4` | Opt60 plus optional FPS-gate diagnostics |

Build contexts, reports, compressed logs, package summaries, host samples and native parity fixtures are archived in `optimization_evidence/2026-11-04-plate-lanes/`. The exact source in each build context is retained. The first native probe command accidentally replaced the image's PYTHONPATH and could not import `gstgva`; the corrected probe preserves the image environment and adds only the worker path. That harness error is not a production-image failure.

### Validation results

The native plate probe ran 24 identical original-recording crops through one and two lanes, with two concurrent owners. Input arrays and every output value matched exactly (maximum absolute difference 0). The fixture had real detector outputs, with maximum plate confidence approximately .86. This verifies the tested plate tensors/ownership only, not full event/tracker parity or labelled ANPR accuracy. Script: `scripts/v8_plate_lane_probe.py`.

Forty tests passed: 32 existing V8 runtime tests plus eight new concurrency/timeout/ownership cases in `tests/test_dls_secondary_concurrency.py`. Both canonical and embedded V8 validators and `git diff --check` passed.

Full-pipeline replay used the existing busy ANPR recording, all bindings enabled, two cameras, 10-second warmup and 60-second steady observation. Each row below is a separate run; changing background workloads mean differences are not isolated performance estimates.

| Run | Artifact suffix | Result |
|---|---|---|
| Opt60, one lane, 10 FPS/camera | `db2xkf3h` | Failed to reach healthy startup; plate job 272 timed out after 30 seconds. Captured health became stale. Not explained by RAM exhaustion. This new-code run must remain recorded as a failure; do not attribute it automatically to background programs. |
| Unmodified opt57 control, 10 FPS/camera | `zjj1szsc` | Failed sustained target. Mean sampled selected/inference aggregates 15.47/16.14 FPS against 20 target. No decoder errors at the final snapshot. |
| Opt60, two lanes, 10 FPS/camera | `wps32sn0` | Failed. Mean sampled selected/inference aggregates 15.52/16.83 FPS; one camera reported two decoder timeouts. Plate queue means approximately .097/.107 ms; full-frame means 74.5/96.4 ms versus control 97.6/100.0 ms. Local timing improvement did not establish reliable end-to-end gain. |
| Opt61, two lanes, 8 FPS/camera, rate trace enabled | `9rd1mjn1` | Failed sustained target. Mean sampled selected/inference aggregates 14.50/14.27 FPS against 16 target; final selected 12.087 FPS. Both cameras ended healthy with zero decoder errors. Sampled primary PTS selection showed the expected approximately 8-of-10 frame admission; it did not reproduce the earlier large 10-FPS drop anomaly. |

Artifact suffixes refer to `/tmp/sentry-v8-capacity-2.<suffix>`; persistent copies are in the archive above. Sample means are rolling-rate summaries, not exact time-weighted capacity. The early-failed one-lane report has an empty `final` because it predates the harness fix; its `healthz-final.json` retains the stale-camera evidence. The mock delivery agent remains a local capture/ack fixture, not proof of real remote durability.

### Decision and remaining work

Keep the lane experiment available for diagnosis, with default one lane. Do not publish it as a production optimization or infer a hardware camera maximum. Model execution overlap is technically possible and can reduce waiting, but the full pipeline still has stalls/backpressure and variable processing time. No gate, sampling interval, ANPR acceptance criterion, tracker setting, model, decoder device or contract was relaxed to improve the numbers.

Do **not** repeat the blanket post-inference-queue experiment or merely increase request counts. The next diagnosis must distinguish pre-gate decoded progress, native primary/face progress, retained-surface consumption and blocked secondary work at the exact stall. Capture stage progress and source-age/backpressure together; the current decoder-timeout label alone does not locate the fault. Investigate bounded overlap of per-frame crop work only after those boundaries and failure recovery are measured. Preserve same-frame results and all production gates. A safe native-error recovery path and stable busy replay remain required before enabling concurrency by default or making a capacity claim.

All test containers were stopped at the end of this continuation. No commit, registry push or organization push was performed.


## 16. Native stall localization and compressed-transport correction

User authorization: continue optimization on the shared host, without stopping other programs, changing V8 or relaxing production/adaptive gates. The previous section remains historical evidence; the new work follows its requirement to locate the stall rather than repeatedly increase queues or model requests.

### What was established

- Added optional `SENTINEL_DLS_TRACE_PROGRESS=1` independent stage reporting. It records parser, decoder, FPS gate, primary/face inference and sink progress even when camera policy is blocked. Opt62 attached the FPS probe after selection; opt63 and later attach it before selection. Do not compare the old and new FPS-probe counters as equivalent.
- Opt62 reproduced decoded-buffer progress with a constant output PTS while the parser/source continued. The FPS gate correctly rejected duplicate timestamps. The generic “surface decoder timeout” did not identify the actual failure boundary.
- Tested `tsdemux skew-corrections=false` in opt63. One run did not reproduce the timestamp freeze, but opt64 did. **This was not a sufficient fix; opt65 restores the demux default.** Do not repeat the claim that skew correction alone caused the stalls.
- Captured GDB stacks from our own opt63 CV worker (host PID 2500944). A native completion thread was blocked in `gst_app_sink_render_common -> InferenceImpl::PushBufferToSrcPad -> PushOutput`; other inference threads waited on locks/requests. This supports downstream blocking inside completion, not a fully proven model-specific deadlock cycle. The instrumented eight-FPS trial is not a clean capacity measurement because attaching GDB briefly pauses the process.
- GStreamer 1.28.2 decoder source clamps backward PTS to its previous output timestamp. Opt64 decoder debug logs contain decreasing-timestamp and decode-error warnings. Preserve the distinction between source/parser timing, decoder output and inference admission.
- Found that the TCP appsrc was configured to discard arbitrary compressed chunks (`leaky-type=downstream`, one MiB). This is not a safe decoded-frame selection mechanism. It can cut PES/H.264 data and timing information. Opt65 changes it to a non-leaking queue and waits for capacity before reading the next <=64 KiB chunk. One producer bounds the queue to max-bytes plus one chunk; the wait is stop-aware, and push-buffer remains non-blocking. Network/socket buffers are additional bounded buffering and can introduce latency; this is not a freshness guarantee under overload.

### Runtime changes under test

1. Native output reservation: at most two admitted primary frames per camera, including native in-flight frames, with two non-dropping appsink slots. Admission waits **before** inference; taking a completed sample releases its reservation. Reset generations reject waiting old-epoch admissions, and close wakes waiting producers. This targets callback/sink blocking without adding arbitrary post-inference queues or dropping already admitted results. Native process failures still require separate recovery validation.
2. TCP transport backpressure as described above; only the authoritative TCP MPEG-TS input changes. RTSP/RTSPS source construction, VA decode, models, model devices, classes, confidence thresholds, NMS, motion decisions, tracker mode and all use-case quality/emission rules remain as before these trials.
3. Diagnostic trace includes appsrc queued bytes and dropped/in/out counters when the installed GStreamer exposes them. No new desired-state fields or wire protocol changes.
4. Capacity harness now checks final end-of-run health as well as rolling samples, preventing a late stall from being hidden by an earlier healthy sample.

### Images and preserved evidence

All are small local overlays of the same model/dependency base; none has been published or promoted.

| Tag suffix (`localhost/sentry-v8-...`) | Image ID | Purpose |
|---|---|---|
| opt62-progress | `0c1b292f236c6ff1ccc0a310dbcf41ec1949774a3b604113589fdc2fb0119c33` | Independent native progress |
| opt63-tcp-timing | `80dcc930950d42d4b19bc5f52f1495c82f5eb4cecbc431b53c1878d4bdfbc501` | Demux skew experiment; not sufficient |
| opt64-reserved-output | `5c68b45b81cccb20346c47700db421cf321761b4f2121f8a803676f811fa76a0` | Output reservation, still leaking TCP input |
| opt65-transport | `186813967b6c57f7f7be4cdd632c7da08d96bcd4508630f065f1a4bcc509f1a0` | Reservation plus bounded non-dropping TCP, demux default restored |

Archive: `optimization_evidence/2026-11-04-native-stalls/`. Includes build contexts, reports, compressed logs and output packages, configs, source logs, the GDB stack/trigger, and the inspected GStreamer decoder source. Packet/evidence capture uses a mock local acknowledgement agent; it is not remote Management durability qualification.

| Trial | Artifact suffix | Result |
|---|---|---|
| Opt62 2 x 10 FPS | `cl9gl2fo` | Failed: timestamp stall and secondary timeout |
| Opt63 2 x 10 FPS | `xcq69693` | Failed sustained FPS; no fatal line in captured log |
| Opt63 2 x 8 FPS, GDB instrumented | `xmv4_bii` | Failed sustained FPS; useful native stack evidence only |
| Opt64 2 x 10 FPS, 120s steady | `fcoclsz2` | Failed: mean sampled selected 15.47/20 FPS; timestamp stall remains; no fatal inference line |
| Opt64 2 x 10 FPS, decoder debug | `emgmhszw` | Failed: decoder backward-timestamp/decode warnings reproduced |

The means above summarize rolling windows, not exact time-weighted capacity. Opt64's end snapshot can be healthy despite an earlier timestamp stall. Neither that snapshot nor an empty fatal-lines list proves a passing run.

Upstream references: [appsrc bounded queues and push semantics](https://gstreamer.freedesktop.org/documentation/app/appsrc.html), [GStreamer 1.28.2 decoder timestamp handling](https://raw.githubusercontent.com/GStreamer/gstreamer/1.28.2/subprojects/gst-plugins-base/gst-libs/gst/video/gstvideodecoder.c). These explain mechanisms; local traces establish what was observed here.


### Opt65 busy replay result — do not promote

Two cameras at 10 FPS, all use cases, 15s warmup and 120s steady observation (`emfoly58`) **failed**. TCP dropped-buffer counters stayed zero; sampled decoder PTS advanced without repeated-PTS windows; the debug log had zero decreasing-timestamp or decode-error warnings. However, both cameras later blocked in `plate_detection` for approximately 28 seconds. Final frame ages were 29.1/28.1 seconds, both cameras degraded, pending admissions remained two each. Thus output reservation and transport backpressure do **not** resolve the shared inference stall.

The run captured 1,674 packages, 701 evidence objects and 104 embeddings (96 scene, six face, two body) before stopping. These establish output execution, not accuracy, losslessness or parity. No gait result occurred in this trial. A mean of the reported rolling selected rates is 17.63 FPS against target 20, but the health rates stayed frozen/non-zero during the late stall; that mean must **not** be presented as achieved sustained throughput. The harness correctly failed on freshness despite `ready=true` and an empty fatal-lines list. Record these observability limitations for correction rather than treating a healthy process or empty error list as healthy analytics.

Validation: 46 focused tests passed (32 V8 runtime, eight secondary concurrency, three output admission, three TCP capacity/cancellation). Canonical and embedded V8 validators passed. Production-policy SHA-256 remains `95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03`. No policy/schema changes were made for opt62–65. Existing dirty changes from prior work remain untouched.

Reproduce the busy test:

```bash
python3 scripts/v8_capacity_probe.py \
  --image localhost/sentry-v8-opt65-transport \
  --cameras 2 --fps 10 --port 18080 --startup-timeout 90 \
  --warmup-seconds 15 --steady-seconds 120 --sample-seconds 10 \
  --minimum-decode-ratio .90 --full-usecases \
  --container-env SENTINEL_DLS_TRACE_PROGRESS=1 \
  --container-env GST_DEBUG=videodecoder:4 --container-env GST_DEBUG_NO_COLOR=1 \
  --recording /home/admin1/Downloads/Usecase-Catalog-main/videos/ANPR.mp4
```

Host remained shared: other programs were not stopped; a sampled reading showed about 17.3 GiB available RAM and pre-existing swap use. GPU metrics remain node-scoped. No maximum camera count can be inferred from these tests. Root filesystem has approximately 1.3 GiB free; continue with small overlays only and do not delete others' files/images.

### Decision and work still required

- Keep opt57 as the named reference and opt65 as an **unqualified local diagnostic candidate**, not a production replacement. No broad redesign, new gates, threshold weakening or parallel camera-capacity claims are justified.
- Preserve compressed TCP bytes through bounded backpressure; prove latency bounds and recovery across genuine disconnects, timestamp resets, corrupt input and sustained overload before release. The current feeder can shift backlog into socket/server buffers; it does not solve overload by itself.
- Next native investigation must capture the exact plate engine request/response and native callback state **after output reservation is active**. The old stack identifies a possible blocking mechanism but is not proof that the later stall has the same cause. Do not blindly increase plate lanes or queue sizes again.
- Camera loops still synchronously wait on shared secondary calls. Bound failed-call recovery and keep health reporting fresh without attributing a late result to another frame/session. Native shutdown may itself block; Python timeout alone is insufficient evidence of recovery.
- Repair stale rolling-rate reporting and distinguish fresh decoded input, timestamp progress, selected inference and latest output age. Do not change the V8 wire contract merely to correct internal telemetry.
- Full detector/native preprocessing parity, tracker/count/dwell timelines, selected evidence association, real agent durability, long busy soak and multi-camera capacity remain unqualified. The passing unit/schema checks do not replace these.


### Final lower-load isolation result

Opt65 two cameras x 4 FPS, busy ANPR recording, all bindings, 10s warmup and 90s steady: **passed the short harness check** (`sswonpxi`). Camera 2's sender was stopped for 12 seconds after 20 seconds. At the 10-second outage sample it was unavailable (frame age 10.323s, one reconnect); camera 1 remained healthy. At the 20-second recovery sample camera 2 was healthy again, same worker PID 47, frame age .125s. All later samples showed the peer healthy and the target recovered. This was an observed interruption, not merely buffered playback masking the outage.

Final rolling selected/inference aggregate was 8.435/8.435 FPS for target 8, container RAM 3410.61 MB, container CPU 1.185 cores, node GPU activity 32%. These are final snapshots on a shared host, not a capacity/latency guarantee. Capture ended with 1,382 packages, 634 evidence objects and 113 embeddings (108 scene, three face, two body). The slightly earlier health snapshot reports 1,369 admitted/durable; these endpoints must not be subtracted as a loss estimate. No positive gait/fire or labelled accuracy qualification follows from this run.

Reproduce with the busy command above, changing `--fps 4 --warmup-seconds 10 --steady-seconds 90`, adding `--outage-camera 2 --outage-after-seconds 20 --outage-seconds 12`, and omitting the two GST debug environment arguments. Keep the progress trace enabled.

Final decision: keep the bounded transport/admission work and diagnostic evidence available for further development, **do not promote or publish opt65**. Busy two-camera 10-FPS execution remains failed due to secondary inference stalling. The low-load recovery pass does not override that failure. No Git commit/push, registry push or organization push was performed. All test containers and replay senders started for these trials were stopped; other host programs were untouched.


## 17. Reassessment requested by user — preserve the DL Streamer goal

User explicitly asked to proceed, reflect on the repeated experiments, keep every change here, and report only supported facts. Goal remains efficient DL Streamer execution WITH V8 outputs and existing production/adaptive policy, not simply a running detector benchmark.

Reflection: the reference DL Streamer benchmark and this full-output pipeline do different amounts of work; its reported camera count cannot be adopted as our capacity. Prior trials changed request/queue settings before isolating the blocking boundary. Some apparent improvements did not survive repeat testing. Progress telemetry and final freshness checks now prevent calling those runs successful. The outstanding plate-stage wait is an observation, not proof of a faulty plate model or insufficient hardware. Do not attribute it to other host programs without evidence.

Plan for this continuation: freeze opt65 and reproduce with secondary-job trace plus a native stack captured DURING the new stall. Identify whether the wait is input submission, native inference completion, output handoff or Python policy. Change only the demonstrated boundary, then test exact output association and replay the same workload. No policy/contract/model changes or default lane increase. Correctness before a capacity claim. Any failure must remain in this section.

Started a two-camera x10 FPS busy ANPR replay on opt65, 10s warmup/180s steady, with `SENTINEL_DLS_TRACE_PROGRESS=1` and `SENTINEL_DLS_TRACE_JOBS=1`. This is a diagnostic run: a GDB attachment to our own test worker, if triggered, prevents treating its rates as clean performance measurements. Other programs remain untouched. Initial available RAM about20.6GiB; disk about1.48GiB free. Evidence destination: `optimization_evidence/2026-11-04-stall-reassessment/`.


Reassessment result: the opt65 180-second diagnostic run (`yw_tqpke`) did not reproduce a plate phase longer than four seconds, so no new native stack was captured. The legacy harness printed PASS based on final windows; earlier rolling windows fell to roughly9–14 FPS aggregate. This result must not be cited as sustained20 FPS qualification or resolution of the intermittent stall. Tracing itself changes workload overhead, and background load is not controlled.

Changes made next are measurement corrections only: (1) CameraTelemetry publishes periodically from an independent thread, so blocked model calls do not freeze a non-zero rate; serialization covers counter reset and atomic file replacement to avoid concurrent flush races. No analytic output/gate decision is made by this thread. (2) Capacity harness records monotonic timestamps and cumulative counters at steady samples, requires each camera's whole-interval selected throughput to meet the threshold, and distinguishes an isolation run from a capacity qualification. Three good ending windows no longer suffice. These changes address demonstrated measurement defects, not a claimed native stall fix. Added tests cover idle publication, concurrent flush, hidden early throughput deficits, unequal camera rates and counter reset.

Opt66 is a small telemetry-only overlay of opt65; scheduling, models, production gates and V8 remain unchanged. It will be checked on the same workload with the corrected harness. No native-execution tuning is justified until the failure is localized.


Opt66 reproduced the stall (`kr88sbf5`); independent telemetry correctly reached zero FPS. A GDB stack captured at >4s plate-phase age does NOT show the old `gst_app_sink_render_common` callback blockage. It contains an appsink consumer waiting for output and a source-thread stack inside Intel OpenCL/OpenVINO GPU plugin; many other threads are idle/waiting. Symbols/16-frame depth are insufficient to name the driver operation or prove a driver defect. A later deeper attachment failed because the process had already exited; keep that failed attempt recorded. The plate-detector job13902 timed out at30,000.857ms; camera frame ages later exceeded60s. Thus Python timeout did not restore execution. Whole-interval selected FPS was5.80/5.76 per camera, but GDB paused the worker briefly: this is failure diagnosis, not clean performance benchmarking. Models/gates were unchanged.

Next controlled experiment: compare the SAME plate model through GVA on GPU vs NPU using identical24 fixture crops, before any full-pipeline device trial. Motivation: current blocked path includes GPU execution, and the reference architecture used NPU for plate work. This is a hypothesis test, not a proven root-cause fix or an approved default-device change. Added `--device GPU|NPU` only to the probe script; default staysGPU. No CPU model fallback permitted. The first idle-telemetry image check failed to start because the host lacks slirp4netns; rerunning with `--network=none` succeeded, giving zeroFPS with advancing timestamp and retained counters. This was a test-launch issue, not an image failure.


GPU/NPU plate fixture comparison:24 identical preprocessed crops, same model/threshold. All confidence>=.35 detection counts/classes matched; minimum matched bboxIoU .9986945; maximum matched confidence difference .0012207. Full raw tensors are NOT identical (low-ranked output ordering/values differ; raw maximum absolute difference222.03125). This small fixture supports a diagnostic device trial, not global output parity or a safe default switch near thresholds. Probe wall times1.57sGPU/.93sNPU include startup; do not treat them as sustained throughput benchmarks. Fixture arrays and comparison are archived.

Started opt66 with ONLY `PLATE_DEVICE=NPU` changed for the same2x10FPS/180s workload, all use cases and original policy. This uses existing runtime device selection, not new desired-state fields. No defaults changed. Outcome pending below.


Opt66 exact image ID: `e75702a9a15bc1eaa99565e38513d5ab1a22460a05cba16440fa91883241e189`, local tag `localhost/sentry-v8-opt66-telemetry`. Only runtime_telemetry.py differs from opt65 in this image. Default plate device is STILL GPU; NPU is a test override. 52 focused tests and canonical/embedded validators passed. Image idle-publication check also passed: after no camera processing calls, FPS becomes0, total stays5, publication timestamp advances. The main runtime graph, model files and V8 policy have not been altered in this continuation.

The opt66 captured stack is evidence of the failure location, not a diagnosis of a specific Intel driver function. The stack is only16 frames deep with stripped GPU-plugin symbols; an active GPU-path thread could belong to another shared model. Do not state “plate GPU driver deadlock proven”. Observable facts are plate result timeout, peer camera progress stopping, and no full-appsink completion stack in this capture. The capture happened AFTER the trace detected a stall, so it did not initiate the observed stall; its pause still invalidates clean capacity comparison.


First NPU plate full-pipeline trial (`u86ldszs`) PASSED the corrected short-replay check: cumulative selected rates9.993 and10.199FPS/camera across170.119 measured seconds, desired10each. All bindings remained enabled. No fatal inference lines. Final snapshot RAM3557.24MB, CPU2.261cores, nodeGPU58.12%, nodeNPU42.74%. Captured3571packages/1426evidence/210embeddings (202scene,6face,2body); no positive gait output or hazard accuracy claim. Mean plate executor4.493ms, mean queue1.553ms across25127calls versus prior GPU repeat7.708/4.268ms. These runs had uncontrolled shared-host load and include startup model calls; comparisons are evidence for further testing, not an isolated speedup estimate.

Started an exact second NPU replay (same image, same override, same180s duration, same tracing) to test repeatability. No new image or scheduling change. The NPU result narrows a useful optimization direction: reduce the GPU plate workload while preserving the GVA adapter and camera-local policy. It does NOT establish the root cause of the GPU stall or permanent freedom from it.

Additional source-only edit: corrected CameraTelemetry's introductory comment to say camera workers may share a native graph process. Opt66 build context precedes this comment-only edit; executable behavior is identical. No release/tag was overwritten.


Second NPU180s replay (`a422uox3`) also PASSED the corrected criterion without a captured fatal inference error. Decision: bake tested `PLATE_DEVICE=NPU` into a LOCAL opt67 candidate, not a production release. Updated docker/Dockerfile.sentinel-v8 ENV and default IMAGE_VERSION; runtime model-selection code still honors explicit device environment. No model weights or production thresholds changed. Build is a small overlay on exact opt66, with the comment-corrected telemetry file; no apt/pip upgrades. This avoids rebuilding dependency layers on the shared/disk-constrained host. A default-configuration smoke test will verify that the candidate actually uses NPU without an override. Broader device parity, dense-person/NPU-contention testing and long busy soak remain necessary before promotion.


### Final opt67 candidate and checks

- Local image: `localhost/sentry-v8-opt67-npu-plate-candidate`.
- Image ID: `b8081f56830160c948eedd9f1c9a6bb0b8d14bab83ac6aca362a7d159c4e5397`.
- LOCAL manifest digest: `sha256:7616f9102ad83b624696a1e600b7efbdf016fb500fa4ca4aa59207687ad0006a`. This is not a published registry digest.
- Image ENV inspection confirms `PLATE_DEVICE=NPU`. Default-configuration test (no device override) passed at2x10FPS, 10s warmup/60s steady (`uqbf1ec0`). This is an artifact/configuration smoke check; the two preceding180s tests provide the longer short-replay evidence.
- Repeat180s NPU cumulative selected rates:9.9937/10.1524FPS across170.1068measured seconds. First run:9.9930/10.1987. Both meet the existing90% per-camera criterion; occasional rolling windows above10 represent catch-up/window effects, not camera capacity beyond configuredFPS.
- 52 focused tests and both contract validators passed; diff whitespace check passed. V8 and production-policy hash unchanged. Same model files, thresholds, association policy, evidence construction and delivery protocol. Device precision is not bit-identical:24-crop accepted detection comparison is the available bounded evidence, not proof of universal event parity.
- Archive under `optimization_evidence/2026-11-04-stall-reassessment/` contains every new build context, diagnostic stack/trigger, failed and passing reports, compressed package/log captures, fixtures/comparison, tests and updated harness. Native-source inspection from upstream main is mechanism context, not a verified match to every installed binary symbol. Download handoff mirror updated.

Decision: opt67 is the next LOCAL candidate for broader validation. It retains DL Streamer, VA decode and shared GVA execution; only plate device placement changes from opt65, alongside the telemetry fix. Do not silently revert to GPU plate defaults, increase lanes, rewrite gates or cite the old three-final-window PASS as sustained capacity. No publication or Git commit/push performed. Test containers/replay senders are stopped; unrelated host workloads remain untouched.

Next checks, in order:
1. Broaden GPU/NPU plate parity with original vehicle crops, threshold-adjacent cases and labelled OCR/evidence outcomes. Small floating-point changes could alter borderline acceptance even though the gates are unchanged.
2. Test dense people/identity/gait workloads for NPU contention with plate detection; the current ANPR recording is not representative of every use case.
3. Repeat longer busy soak, reconnect and actual delivery-agent fault tests on opt67, then increase camera count at fixed requestedFPS using whole-interval/per-camera/freshness checks. No maximum-camera claim yet.
4. Continue the unresolved GPU native-stall investigation if GPU plate operation is required. Opt66 proves a result timeout and an active GPU/OpenCL path, not a specific driver defect. Opt67 short passes do not prove the failure impossible elsewhere.
5. Validate full event/tracker/count/dwell and original-frame evidence parity, plus native adaptive-tracker integration and other previously documented opt57 gaps. Do not treat this placement improvement as completion of those requirements.


## 18. Opt67 production-readiness audit requested by user

User asked to check and explain what prevents production readiness. Keep opt67 frozen; distinguish current defects from missing qualification. No publication or threshold changes authorized/needed for this audit. Running2x10FPS ANPR replay with15s camera2 outage,180s observation, then a separate people-heavy workload on the same image. Other host programs untouched; initial availableRAM20.8GiB/disk1.25GiB. Test artifacts will be archived here.

Code checks confirm: desired-state acceptsHTTP/HTTPS but required surface path rejects them; gvatrack selects its fixed mode without consuming legacy adaptive distance/disappearance values; secondary calls can wait30s and shutdown calls native set_state(NULL) synchronously. The historical GPU timeout did not recover, but this is not yet evidence of that failure on opt67's default NPU plate placement. Capacity harness uses a MOCK capture/ack agent and its `full_usecases` output check only requires health/count/scene/evidence; it does not prove every use case correct. These scope limitations must be explicit.

Fixture check: people_counting.mp4 is unreadable (missing moov atom), so it is excluded as a bad input fixture, not counted as an image failure. crowd.mp4 probes asH264/1280x720/~29.97FPS/14.85s and will be inspected before the next sequential test.


Opt67 default2x10FPS ANPR replay with15s source2 interruption PASSED (`n5prphxa`): target degraded at10s outage sample, recovered at20s sample, then stayed healthy; peer healthy at EVERY sampled outage/recovery checkpoint (manually checked beyond harness any-peer check). No fatal lines.3357packages,1360evidence,202embeddings. This closes the short TCP source-outage check for opt67; it does not prove recovery from native GPU/NPU hangs or durability across process/node restarts.

ConfirmedHTTP mismatch by running `_probe_source` inside opt67 with `http://example.invalid/video`: validator scheme allowsHTTP; native path immediately raisesUnsupportedSurfaceSource before network access. Saved result. Crowd fixture visually inspected: people-heavy street scene, but has pre-existing drawn boxes/text. Use for operational load only, NOT labelled accuracy/count ground truth. Same2x10FPS/180s test started sequentially on that~29.97FPS source. Different source FPS/compression/person load mean this is not an isolated plate-device speed comparison.


### People-heavy result and readiness assessment

Opt67 default2x10FPS crowd replay PASSED (`o4lh3bld`),180s configured observation/~170.096s between cumulative samples. Selected rates9.9767/9.9649FPS, all sampled cameras healthy, largest sampled frame age1.156s, no captured fatal lines.3059packages,1013evidence,591embeddings:198scene,303body,59face,31gait. Final containerRAM2956.73MB/CPU1.604cores; nodeGPU54.64%/NPU33.25%. These show modality execution under this operational load, not identity/gait/count accuracy. Crowd fixture is already annotated and looped; no trusted ground-truth claim. The host was shared, so these are observed workload results, not isolated hardware limits.

What is now demonstrated on exact opt67: short ANPR workload throughput,15s TCP source interruption/recovery with sampled peer isolation, and short people-heavy throughput including all three identity modalities. No runtime/image/policy/schema change made during this audit. No new image built or pushed.

What still prevents a general production-readiness claim:

| Area | Evidence/status | Required resolution |
|---|---|---|
| HTTP/HTTPS | Confirmed image defect: validator permits schemes rejected by native source probe | Implement/test native supported input path or accurately restrict advertised support; not a blocker for a deliberately TCP-only pilot |
| Adaptive native tracking | Native graph selects gvatrack mode but does not map our legacy adaptive association-distance/disappearance settings | Establish an explicit supported native adaptation strategy and validate continuity; adaptive presence/quality gates are separate and remain present |
| Native model-hang recovery | GPU plate predecessor timed out without restoring processing; no proof of robust native-hang recovery on opt67 | Controlled inference/worker-fault tests and bounded recovery; passing camera-source reconnect is not this test |
| Delivery durability | Mock verifies schemas, IDs, evidence size/SHA and embedding/evidence links; it stores acknowledgement state in memory | Test real delivery-agent + Management under restart/network/outbox pressure, including retrieving uploaded JPEGs/vectors |
| Accuracy and semantic preservation | Small24-crop plate device comparison; output execution confirmed | Broader threshold-adjacent device parity and labelled ANPR, tracker/identity, count/dwell, hazard and evidence provenance tests |
| Duration/camera capacity | Short3min cases at2cameras; older quiet workloads are not opt67 qualification | Hours-long representative busy soak, then staged camera increases with per-camera latency/freshness/memory/output checks |

Do not misstate missing validation as a confirmed failure. Do not describe opt67 as universally broken: the tested narrow paths passed. Conversely `full_usecases` enabled is not proof all outputs were semantically correct. /healthz is process/HTTP liveness underV8; it must not be treated as proof of healthy analytics or remote delivery. A source-outage isolated in one graph does not prove a native-process crash is camera-isolated.

Recommended order: complete native-hang recovery and real-agent delivery tests, broaden labelled/parity checks, then sustained representative soak/capacity qualification. Resolve HTTP/native adaptation before claiming those supported behaviors; scope any pilot explicitly to tested inputs/hardware/workloads. Archive `optimization_evidence/2026-11-04-production-readiness/` has both new reports/logs/output summaries and the confirmed HTTP check. All test containers/senders stopped. No commits/pushes; unrelated host processes untouched. Download handoff mirror refreshed.


## 19. ROI conversion optimization requested by user

User asked to proceed with remaining optimization and suggested converting only eachbbox. Goal remains DL Streamer optimization with identical policy/contract semantics. Initial consumer audit: current primary VA detection is already surface-based, but admitted frames needing any pixels usually materialize full BGR. Object/face/body/gait paths largely slice regions; full frames still serve scheduled scene/adaptive image-quality observations, ANPR candidate frame snapshots and full-frame event evidence. Do not remove these requirements or substitute resized detector inputs. Start with a same-surface pixel/timing comparison before integration. Upstream vapostproc supports GstVideoCropMeta; videocrop can negotiate metadata cropping, but odd NV12 offsets need care. Probe uses even-aligned expandedROI with4pxhalo then exactCPUtrim, checks output geometry andPTS, and checks VA memory at postprocessor input. No runtime behavior changed yet.

New qualification script: scripts/v8_roi_conversion_probe.py. Uses hardware H264 decode, retains a source sample, compares directROI conversion to existing full-BGR-then-slice on identical frames, including odd origins, edges, small16pxROI and full-frame requests. Tests are about pixel equivalence/resource cost, not detector accuracy. References: https://gstreamer.freedesktop.org/documentation/va/vapostproc.html and https://gstreamer.freedesktop.org/documentation/videocrop/videocrop.html .

### ROI experiment results and promotion decision

Three sequential hardware probes ran on unchanged local opt67. Source: Usecase-Catalog-main/videos/ANPR.mp4, 1280x720 H264. No other host programs stopped. Script retained each source VA sample and checked PTS/output geometry; five rectangles included odd origins, edges,16x16 and full frame. VAMemory caps at postprocessor input are verified, not proof of absence of internal driver copies.

| Probe | Frames / conversions | Result |
|---|---:|---|
| One dynamically reconfigured crop converter |20 /100| Pixel parity failed: maximum channel difference249/255; even full-frame requests differed by up to25 |
| Separate fixed converter per rectangle, repeated full-reference control |10 /50| Full-reference repeat and fixed full-frame crop were pixel-exact; smaller crop differences remained, maximum40/255 |
| Fixed converters plus explicit source colorimetry inferred from VideoInfo |10 /50| Still failed; maximum40/255. Not resolved by explicit colour metadata |

PTS/shape checks passed in all completed probes. Differences are not evidence of a changed model or production gate: this is a standalone conversion experiment. Root cause is unresolved. Dynamic crop renegotiation is implicated by the differing dynamic/full-frame versus fixed/full-frame result, but not proven as the sole cause; fixed crops still differ. Do not assert chroma alignment or colourimetry alone explains it. Halo/even alignment did not establish equivalence.

Performance is not a proven win: original dynamic median small-ROI conversions were ~4.3–5.2ms, but one corner path had ~486ms outliers. Fixed/control runs also had outliers and widely varying timings. Repeated controls and multiple converter graphs add work, and host is shared. Do not extrapolate these numbers to capacity. Several ROI calls can cost more than one full conversion. No new model accuracy evaluation was attempted: pixel equivalence protects existing gate inputs.

**No runtime ROI integration, new image, commit or publication was performed. Opt67 remains the unchanged candidate.** The new script is qualification tooling only. Artifacts are archived under optimization_evidence/2026-11-04-roi-conversion/ (not only /tmp).

Reproduction: mount scripts/v8_roi_conversion_probe.py as /probe.py, ANPR.mp4 as /fixture.mp4, writable output directory as /results into opt67; run with --network=none --device /dev/dri --group-add keep-groups --entrypoint python. Command inside container: /probe.py /fixture.mp4 --output /results/roi.json (20 frames); fixed control adds --frames 10 --fixed-converters; colour control additionally adds --explicit-color. All probe containers exited and were removed.

### Next bounded optimization steps (do not restart the architecture)

1. Keep existing production full-frame converter as reference. Resolve/replace the failed VA region converter before rollout. A possible alternative is full-surface VA colour conversion with region-only CPU mapping/copy; that saves CPU transfer/copy only if measured, and must not be called region-only GPU conversion. Neither alternative is implemented yet.
2. Introduce explicit frame/region access only after parity: crop requests retain camera/session/frame ID, PTS, source geometry and expanded face evidence bounds. One full-frame materialization must be reused if requested, rather than converted again for each consumer. Retain the source surface until all bounded consumers finish. Avoid passing a lazy Python proxy to OpenCV or fake full-frame pixels to quality gates.
3. Preserve scheduled full-frame adaptive quality sampling, scene sampling and any ANPR/zone/line/hazard evidence requirements. Audit ANPR full-frame candidate snapshots before claiming broad ROI savings. Multiple/overlapping region requests need measured crossover to a shared full frame; do not lower quality gates or emission cadence to achieve speed.
4. Remaining independent bottleneck: synchronous shared-secondary waits can block camera policy. Any future asynchronous work must preserve per-camera ordering, session invalidation, timestamps and candidate associations; no new unbounded queue or silent required-output drop. Profile before restructuring.
5. After each accepted change, repeat same-input output/pixel checks and short busy multicamera throughput/freshness measurements against opt67; then increase duration/cameras. Do not call missing qualification a proved failure or an experimental path integrated.


## 20. Bounded work reuse and earlier gait inference admission

User authorized further optimizations preserving V8/policy. Changes are incremental; failedROI path remains disabled. Profile from opt69 crowd run shows camera1 lifetime wall averages: full-frame conversion12.198ms, body21.56ms/call, gait processing27.576ms/call, scene17.521ms/call, JPEG1.497ms/call. Stage spans overlap; do not sum these to infer GPU usage. Queue waits for plate/scene were small in this2-camera workload; a blanket async rewrite is not justified by these numbers alone.

Implemented frame_work_cache.py and V8 integration: a camera-local8-entry/8MiB LRU holds encoded JPEG bytes and scene vectors, never raw VA surfaces/frames. Keys include source frameID,shape,dtype,SHA256(pixelbytes); scene key also identifies the extractor. Modified arrays cannot reuse old outputs. Each binding still generates its own observation/evidence/embedding IDs, trigger metadata and timestamps. Sameframe content hashing is only activated for scene scopes shared by multiple bindings. Singleton scene/ordinary evidence retains original model/encoding work; counters expose computed/reused work.

Rejected broad JPEG caching: measured one-off JPEG1.429ms original versus2.018ms hash/cache, two identical requests2.848 vs2.596ms, four5.688 vs3.755ms. These are local CPU microbenchmarks, not end-to-end throughput. Opt68 included blanketcache and passed2x10FPS70s replay, but was superseded to remove singleton overhead. Opt69 narrowed cache and also passed2x10FPS70s. Shared-scene opt69 probe1x10FPS60s passed with39computed+39reusedscene calls and39reusedJPEGs. Each scene binding still produced outputs; no policy interval change. Tests verify distinctIDs/linkage and identicalbytes/vectors, plus mutation/eviction/isolation cases.

Correction: fresh Python inopt67 reports16OpenCVthreads, but importing runtime decode.py sets1 via existing SENTINEL_OPENCV_THREADS. This is ALREADY implemented, not a newly found runtime optimization. Opt69's explicitOPENCV_FOR_THREADS_NUM=1 was redundant; opt70 omits it. Do not attribute performance changes to thread tuning.20-frame/120array OpenCV thread-parity check was exact but does not prove a new improvement. Pixel microchecks ran during initial replay startup/warmup; shared host otherprograms remainuntouched.

Gait optimization: collect() now accepts optional accept_sequence callback after motion/silhouette/history update, before stateless model inference. V8 callback uses the SAME min-silhouette/cooldown/quality-improvement gate as final emission, factored into _feature_due with unchanged thresholds. This avoids inference only when vector values cannot change rejection. History, MOG updates, temporal sampling, motion checks, maxsequence truncation and timestamps remain unchanged. cached() has no runtime consumers outside the extractor; skipped rejected results no longer update that unused vector cache. Do not apply this skipping to an implementation that consumes those intermediate vectors for tracking. Deterministic80-observation test preserves both emittedvectors/timestamps/sequencehistory while executing2 instead of60model calls. Real camera output parity/longsoak remains distinct from this regression.

Opt70 built as small overlay of immutable localopt67, replacingonly frame_work_cache.py,v8_events.py,person_reid.py. Localtag localhost/sentry-v8-opt70-policy-work-reuse; ID e23bbbed91161a5c8df483388ec06476e2cb53192c165747007ea386f0bc97e2. No models/devices/decoder/tracker/gates/schema changes. Two-camera busycrowd120s replay currently running. No publication/commit. More results to append below.


Opt70 replay FAILED due to my refactor: removed local `previous` was still used by identity `sample_reason` (NameError). Bothcamera branches restarted; local single-connection replay senders then exited, causing subsequent ConnectionRefused. Rootcause is implementationbug, NOT external camera/network. Interruptedfailedprobe withSIGINT; harnesscleanup removedcontainer/senders. Archivedfailedcontainerlog. Restoredprevious assignment and added actualidentity event-builder regression coveringfirst_qualified/quality_improved andevidence linkage. Earlier controlledgait test alone did not cover that builder.57focused tests now pass. New immutablecandidate opt71 is built; no retaggingfailedopt70. ID652c66cbc9ef89de78b5f8f01b9e81173207cc678ade30b2422c21102a0dc6e5. Busy2x10FPS120s rerun underway.


### Remaining optimization inventory (preserve architecture, do not tune gates for FPS)

| Opportunity | Evidence / intended change | Guardrail / status |
|---|---|---|
| Reuse scene/JPEG work | Overlapping scene bindings share exact source pixels;39calls reused in probe | Implemented bounded cache; events remain independent. No benefit claimed for single-scene binding |
| Avoid rejected gait inference | Sequence quality is count-derived; cooldown/improvement rejection known before embedding | Implemented after collecting identical history. Controlled emission/input parity passed |
| Gait foreground preprocessing | _features calls collect once per person; _silhouettes applies full-frame MOG each call | Significant CPU candidate, NOT safely coalesced yet: current repeatedMOG updates are stateful and single-update masks can differ. Needs identical-input/history comparison and explicit review of resulting behavior |
| Native face scheduling | SCRFD/AdaFace runs every3eligibleframes even when downstream candidate scheduler defers | Candidate for policy-driven admission before nativecascade; must retain exactlydue personROIs, candidate-window timing, per-camera associations and output order. Not implemented |
| Secondary overlap | Shared executor waits are synchronous; activepolicy35ms and wholeframe57ms average in opt69camera1 | Profile higherloads first. Small measuredqueue waits (~.3ms) here do not establish schedulerbottleneck. Anyfutureprefetch needs boundedjobs, orderedcameraresults and stale-session rejection |
| Source conversion / crop lifetime | Full conversion~12ms/call in sampledcase | ROI experiment failedparity; keepdisabled. Region-onlyCPUcopy from fullVAcolouroutput remainsunimplemented alternative, not trueall-modelzero-copy |
| Request pools/batching | Primary alreadyshared,staticbatch1,nireq8 | Benchmarkonlywithidenticalmodelpreprocessing/outputs; do not changebatch/requestcount blindly or claimbatchingalwaysbetter |
| Memory/copies | ANPR selectedframes and identitycrops need retention | Auditlifetimes before removingcopies; async consumers must not readreleased/reusedbuffers. Cache bounded8MiB/camera;50configuredcameras meansupperbound400MiBencodedcache, not50cameraqualification |
| OpenCV threads | Existingdecode.py alreadysets1 | Closed asalreadyimplemented; no newbenefitclaim |
| Delivery/JPEG pressure | V8agentdurability and boundedhandoff mustremain | Do not droprequiredimages to claimthroughput; singletonJPEGcache rejected as overhead |

Keep pending reliability work separate from speed claims: nativehang recovery, realagentdurability, HTTPnativepath mismatch and longbusysoak remain open. Existingshorttests do not establishmaximumcapacity or productionreadiness. No architecture rewrite or modelquantization was done in this pass.


Opt71 correctedbusy2x10FPS120s PASS: /tmp/sentry-v8-capacity-2.x4mdpp3f; cumulative110.062s rates9.9853/9.9762FPS. Finaltelemetry85gaitinferencesavoided+10eligible percamera (170avoided/20eligible total). Capture2052packages/716evidence/413embeddings:38face216body20gait139scene. Allcapturedfatalchecks empty. These are output execution/association checks, not labelledaccuracy or proofeverygatecorrect. Archiveopt71-crowd-2x10. Localmanifest sha256:3b9c8e3b46b1b828464aec6e0f12107b02e4cab1794850047958c33553e53fbd (NOTregistrydigest). SourceDockerdefaultversion nowopt71; originalbase/models/policy/contract unchanged. Anunchangedopt67same120s/2x10crowdreplay isnowrunning forcomparison. No highercapacityclaim.


### Final status of this optimization pass

Final candidate: localhost/sentry-v8-opt71-policy-work-reuse, ID652c66cbc9ef89de78b5f8f01b9e81173207cc678ade30b2422c21102a0dc6e5. Localmanifest sha256:3b9c8e3b46b1b828464aec6e0f12107b02e4cab1794850047958c33553e53fbd. No registry publication/commit/push.57focusedtests pass;5newregressionchecks also executed inside exactimage. CanonicalandembeddedV8validators pass;source/imageSHA256 match forall3changedruntimefiles. ProductionpolicySHA unchanged95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03.

Opt71 passed2x10FPSbusy120s replay (9.985/9.976FPS cumulative), avoided170gaitcalls with20eligible/20capturedgaitembeddings. Total2052packages716evidence413embeddings. Sharedscene reuse measuredonopt69 beforegaitchange:39compute+39reuse and39JPEG reuses. Opt71 carries identicalscene reusecode;integrationregressions raninsideopt71. Finalopt71capacityconfiguration hasone scene binding percamera, so it doesnotexercise cachehits. Do not claim those39hitscamefromfinal2camerarun.

Unchangedopt67same120scomparison hitnativeworkerexit-11 duringstartup. Stopped aftercapturingfailure instead oflooping retries;archiveopt67-baseline-native-crash. No cleanbaselinecomparison fromthisrun, henceNO overallFPS/CPUcapacityspeedupclaim. Nativecrashrootcause remainsunresolved and candidatepassdoesnotproveitfixed. Thetwoacceptedoptimizationssaveworkinidentifiedcases; allremainingarchitecture/reliability/qualificationitemsabove remainopen. Opt70refactorbugwasfixedandfailedtagnotpromoted. BroadJPEGcacheopt68superseded;redundantthreadenvopt69notretainedinopt71. Alltestcontainers/replayprocesses cleanedup;otherhostworkloads untouched.


## 21. Five supplied live RTSPS sources — short operational test

User rejectedone-hourtest and suppliedch9/ch10/ch16/traffic1/traffic2 atgpu-box.burmilla-bass.ts.net:443. Startedunchangedopt71,all5distinctsources,8FPS/camera(40aggregate),allusecases,150sstartupallowance,10swarmup,300smeasurement,15ssampling. Purpose: operationalFPS/freshness/stagewaits/memory/outputpackages, notrecognitionaccuracy/longsoak/realManagementdurability. Wrapper /tmp/sentry-live-five.py sets capacityharnessSOURCE_URLS inthatorder;capacity-01=ch9,-02=ch10,-03=ch16,-04=traffic1,-05=traffic2. No runtime/policy/schema change. Localmockcaptureagent verifiespackages;otherhostworkloads untouched. AvailablehostRAM~15.6GiBatstartup,disk1.1GiB. Resultpending; no capacityclaim beforeper-cameraresults.


### Completed live five-camera result — FAILED throughput target

Opt71unchanged;five-minuteconfiguredsteadytest after25.3sstartup+10swarmup. Cumulativewindowbetweenfirst/lastsamples285.224s. Allfiveconnected;no capturedfatalerror, no decodererrors/reconnects in finalhealth. Nonetheless everycamera missedthe90%targetcriterion; totalaverageselectedFPS15.98/40. This isNOT a five-camera8FPS pass, even though healthstates remainedhealthy.

| Source | Average selected FPS (target8) | Whole-frame processing lifetime avg ms | Gait processing lifetime avg ms/call |
|---|---:|---:|---:|
| ch9 | 1.51 | 625.7 | 141.2 |
| ch10 | 1.92 | 496.2 | 122.8 |
| ch16 | 7.00 | 106.3 | 128.1 |
| traffic1 | 1.88 | 482.7 | 83.3 |
| traffic2 | 3.66 | 260.7 | 86.7 |

Sourceprobes:ch9/ch10/ch16HEVC2560x1440, advertised25FPS;traffic1H2641920x1080(avg18FPS,r_frame_rate36),traffic2HEVC1920x1080(r_frame_rate25,avgunknown). These differ materiallyfromearlier720pcrowdreplays. Metadataframerates are not measurednetworkarrivalrates. No resolutiondownscale/thresholdchange attempted.

Memoryfirst/finalsteadysample4939.04→7103.96MB,max7111.61MB. Growth isobserved;five minutesdoesnotestablishleakvswarming/highwatermark. FinalcontainerCPU4.555cores;GPU46.51%/NPU47.24%areNODEscopedincludingotherprograms, notattributedcomputeutilization. Finalcamera last-frameages1.076–4.256s; freshnesslabelalone hidesslowprocessing. SharedprocessRSS mustnotbesummedfive times.

Captured3928packages/1947evidence/625embeddings:79face139body75gait332scene;13plate_read and418plate_outcome. Theseestablishoutputexecution toMOCKagent, notrecognitionaccuracy oractualManagementsnapshotretrieval. Sampledhealth3920durable versusfinalcapture3928 reflectscontinuingcaptureuntilshutdown; not an8-packagelossclaim. Handoffbackpressure9,exhausted0,protocolerrors0.

Gaitcounterstotal1649inferencesavoided and75eligible. Yetgaitprocessing~83–141ms/call remains expensive: includesCPUfull-frameMOG/morphology/sequencecollection, notjustNPU. Existingmeasurementlabels attributeallthisstagewalltimetoNPU, so do NOTreadthat ashardwarebusy or actualgaitmodelcallcount. _features invokescollectonceperperson; full-frameMOG isreapplied percall. This supportsprioritizingpreprocessing/collectionprofile, but a once-per-frameMOG rewritechangesstate evolution and needsparity review. Othercontributors include~27–38msfullframeconversion andserialper-objectbody/plate work. Do not inferGPU/NPUdevicefault or blanketasyncbenefitfrom theseaveragesalone.

Nextboundedwork: splitgaitpreprocessing/model/queue timings accurately; investigateper-personfullframework andretainedmemory; thenoptimizewithoutchangingcollectionhistory/gates. Nativefaceadmissionandsecondaryoverlapremaincandidates. No runtime/image/contract/policy changes inthislive-testturn. No pushes. Testcontainerandagentcleanedup. Archiveoptimization_evidence/2026-11-04-live-five containsconfiguration,report,health,sourceprobes,compressedlogs/packages andwrapper. Sourceartifact/tmp/sentry-v8-capacity-5.m6ozh82b. No one-hoursoak and no claimofmaximumhardwarecapacity.


## 22. Distinguish input/decode throughput from analytics backpressure

User asked whetherlowFPSmeans decoderfailure orframesnotarriving;authorizedcontinueddiagnosis. Existingruntime counterdecoded_fps increments inDlsMotionPolicyGate AFTERPTSselectionandadmissionreservation; it isNOT rawdecoderoutputFPS. Keptwirefieldsunchanged; donotinterpretprevious1–7FPS tableasrawsourcefps.

Unchangedopt71ranfive-source90s diagnostic withSENTINEL_DLS_TRACE_PROGRESS=1. Existingprobes readdecoder.src/fpsgate.sink/primarydet.src/vasink.sink. Fullpipeline indeedslowsrawdecoderoutput too (~5FPSbusy,~21FPSch16 inoneshortwindow), consistentwithboundeddownstreamreservationbackpressure. Thenranisolatedsameimage5simultaneousnativeVAhardwaredecode-onlygraphs,noanalytics. Firstattemptfailedresourceerror;archivedfirstfailure. Secondattemptretainedper-sourceerrorreporting:all5decoded,errorlistempty.20.03smeasurementgavech9/ch10/ch16=26.26FPS,traffic1=18.72FPS,traffic2=18.92FPS. Shortwindowratesmayincludebuffering/bursts;notclaiming26FPSsustainedfrom25FPSsources. This showsinput/hardwarecanexceed8FPSwithanalyticsremoved; notproofnetworkneverstalls. Originalfullpipelinefailedthroughputagain. NoCPUdecodefallback inprobe:explicitvah264dec/vah265dec.

BuiltLOCALdiagnosticopt72overlayofopt71: splitsgaitsilhouettepreprocessing(noacceleratorattribution),actualmodelwalltime,sharedschedulerqueuewaitandexecutorwalltime. OutercollectionspanisnolongerlabelledNPUmodelwork. Gates/history/vectors unchanged. Addedgaugesforretainedgaitpresencekeys/silhouettecount/payloadbytes/trackcenters at5sinterval;payload excludesnativeMOGandPythonoverhead,NOTRSS. Purposequantifypreprocessingandretentionbeforeoptimization. ID632eeec76e968acc8f10d947e5f938f33e696ddb908de0fb4b4ce421d71cf38e.90sliveprofilecurrentlyrunning. No source/decoder/contractchangeandnopush. ProductionDockerdefaultremainsopt71 untildiagnosticcandidatequalificationcomplete. Existingdecoded_fpsnameisstillacompatibilitymisnomer;rawdecodeprogresscurrentlyavailablethroughdiagnosticlogs,notnewpublichealthfield.


### Diagnostic result and direct answer to decode question

Decode-only all5nativeVAdecoders passed20smeasurementwithnoerrors onsecondattempt:ch9/ch10/ch16~26.26FPS,traffic1~18.72,traffic2~18.92. Therefore input/decodecanexceed8FPSunderthisisolation; fullpipelinebackpressureismajorcauseofslowflow, not evidenceofframesneverarriving. Fullpipeline rawdecoder countsalsoslowbecauseboundeddownstreamqueuesstallupstream. Previouslyreporteddecoded_fpsispostselection/admission, NOTrawnetwork/decodeFPS. Logsseparatelymeasuredecoder.src; somewindowsincludebacklogcatchup(e.g.ch16>advertisedrate), so donotclaimthose sustainableFPS.

Opt72diagnosticlive5x8FPS90s completedwithoutcapturedfatalerrors butFAILEDthroughput as expected(nooptimizationclaimed):selectedaverages1.41/2.28/8.46/1.91/3.50.75.065scumulativewindow;shortburstcanexceedtarget. Captured1306packages746evidence289embeddings incl25gait. NewCPUgaitsilhouettepreprocessinglifetimemeans:ch9142.518ms,ch10123.159ms,ch16124.247ms,traffic172.505ms,traffic284.233ms. Observedgaitqueuewaitmeans0.06–0.246mswheremodelsran. Thispointsatpreprocessingratherthanqueuewaitforthisstage/workload. DoNOTsumoverlappingtimingsorrepresentmodelwalltimeashardwareutilization.

Retainedsilhouettepayloadtotal3,198,976bytes(~3.05MiB)atfinalsample; doesnotexplainmultiGiBprocessgrowth. NativeMOGstate,fullframeevidence/candidates,VA/modelpoolsandotherallocationsremainunmeasuredretentioncontributors. Noassertionofmemoryleakrootcause. Codeauditnotesendedpresencegaitdictionarieslackexplicitcleanup,butthatboundedpayloadsampledoesnotjustifyblamingtheobservedGiBgrowthonit. Do notdeletehistoryduringrecoverableocclusion.

40focusedtests passincludinginstrumentationregression:80collectioncallsreported80preprocessingcallsandonly2modelcalls,withnoNPUattributionforCPUpreprocessing. Instrumentationdoesnotchangegates/sampling/frames/modelinputs. Localopt72isdiagnostic,notthroughputfix/release;sourceDockerdefaultremainsopt71. No contractchange,nopush. Alltestcontainersstopped. Nextworktargetsrepeatedfull-frameMOG/morphologyperperson, withstate/pixelparitychecksbeforecoalescing; larger-memoryretentionmustbeinvestigatedseparately. Noone-hourtestwasrun.


## 23. Check silhouette preprocessing alternatives without changing history

Userrequestedcheck. Controlledprobe inopt72OpenCV:720random/solid/structuredmaskcropcases inclclipped/odd/edge/tiny/fullbounds plus90actualMOGupdates oncrowdrecordingresizedto1440p. ROI-onlymorphology uses6pxhalo (openingradius2+closingiterations2radius4), exactsourcecroprestored. Allrawcroppedmasks/normalizedsilhouettes matched. MeasuredmedianMOG58.10ms/fullmorph1.687ms/regionmorph0.177ms. This isSMALL~1.5ms/callsaving, notsolutiontomain58mscost; recordingresizedforCPUcosttest, notliveperformanceclaim.

Integratednewgait_morphology.py into_silhouettes: fullMOGapplicationsequenceanddefaultlearningpreserved; onlystatelessmorphologylocalized.42focusedtests passed inclstatefulbackgroundimage equality aftereachframe andnormalizedsequenceequivalence. Newlocalopt73overlayofopt72 replacesperson_reid.py/addsgait_morphology.py; ID8100d29187501ac0ccf69cee5eb428c5674d84913349f20f44ba8b266055891b, localmanifestsha256:2a317becfdce967ffdeffeb3e758f0bff08534e4f63eeedd4ec6967d046ff2de. No model/contract/gate/decoder/trackerchange,nopush.

Five-concurrentMOGmodels threadprobe: exactmaskhashes for1/2/4OpenCVthreads;wall6.54/6.39/7.39s. Modestdifference1vs2notvalidatedasmaterialgain;4worse. Keep existing1thread. DoNOTclaimthreadtuningimprovedlivepipeline.

Isolatedmemoryprobe5backgroundmodels at3x1440p+2x1080p:RSSafterallocatedinputframes102,129,664bytes;afterfirstMOG1,640,865,792;after11updates1,642,639,360. Incrementoverinputframes1,540,509,696bytes (~1.44GiB). EachmodelgetNMixtures=5. EstablishessizablefixednativeMOGallocation;doesNOTproveentireliveRSSgrowthcauseornomemoryleak.

Rejectedonce-per-frameMOGcoalescing:30recordedframesx5constructedregions,149/150normalizedmasksdifferedversuscurrentper-personupdates. Thischangestemporalmodellearning, nottransparentreuse. Notintegrated;thresholds/embeddingspaceunchanged. Largeraccelerationrequiresmaintainingstateupdateequivalence or explicitlyrevisitingpreprocessingbehavior,notpretendinggatesaloneguaranteeidenticaloutputs.

Opt73live5sourceattempttimedoutatFFprobe(35s) beforeinference;stoppedfailureandarchivedlogs. Laterindependenthostch9probeSUCCESSHEVC2560x1440,so doNOTclaimpersistentnetworkoutageorprovenexternalrootcause. No livequalificationfromfailedattempt. Switchedtorecorded2x10FPS60sfunctionalreplay(nowrunning); donotcallthat5cameraliveperformancequalification. OriginaldefaultDockeropt71keptuntilqualificationdecision.


Opt73recorded2x10FPS60s PASS, nofatalcaptured. Per-camera cumulative rates: {"capacity-01": {"selected_fps": 9.774823819862112, "ratio": 0.9774823819862112}, "capacity-02": {"selected_fps": 9.794813234626657, "ratio": 0.9794813234626657}}. Capturedoutputs: {"package_count": 1018, "event_types": {"camera_health": 6, "scene_sample": 78, "object_present": 162, "zone_count": 42, "person_feature_sample": 161, "track_lost": 285, "plate_outcome": 22, "presence_recovered": 197, "presence_ended_unknown": 63, "zone_entry": 2}, "evidence_objects": 417, "embedding_objects": 239, "embedding_profiles": {"siglip2-base-v1": 78, "transreid-ssl-v18.1": 126, "adaface-ir101-v18.1": 22, "gaitbase-v18.1": 13}}. No overallthroughputgainclaimvsopt72 or5cameralivequalification. SourceDockerbuilddefaultnowopt73toavoidbuildingchangedcodeunderoldopt71tag;localcandidateonly,nopublish. Artifactsarchivedoptimization_evidence/2026-11-04-gait-morphology/. Tests/sourceprobes/stateparityandfailurelogs preserved. Alltestcontainersstopped. MainMOGcostnotresolved.


## 24. User authorized remaining measurement, bounded async work and short live comparison

Extendedbodyandplatetimings inseparateopt74profileimage:preprocessing,modelwall,queuewait,executorwall,postprocessing. Models/gates/sourceframes unchanged. Five-live-stream120sruncompleted,butfailed40FPStarget(asbaseline);artifact/tmp/sentry-v8-capacity-5.mri1kyid archived. Duringrunplatequeue~4–9ms,executor6–8ms,prep1–2ms;bodyqueue~0.1–1.5ms,executor10–13ms,prep~2–5ms. Thesearewalltimings,notpuredevicecycles. GaitstilldominantCPUcost.

Initialidea wasplateprefetch;refinedselectiontobodyprep/inferenceoverlap:bodyprepislarger,andmoreplatequeueingdoesnotremoveitscapacitywait. No plateasyncchangeimplemented. DlsRawImageInferencer split boundedsubmit/wait while retainingexistingblockinginferAPI. _DlsTicket bindsresulttoexactcamera-sessionhandle,checksclosedbefore/afterwait,cancelsunneededjobs,usesjob-specificmetricsratherthanlatestownerstats. Existingper-ownerqueuelimitandroundrobinretained. Timeoutusesremainingtimefromsubmission. No newnativeinferencelanes/batching/modelinstances.

DlsBodyExtractor optionalinternalSENTINEL_DLS_BODY_PREFETCH=1 pipelinestheCURRENTframe'scrops:oneoutstandingmodeljob+onepreparedcrop,prepare(next)whilemodel(current)runs,consumeinoriginalorder,submitnextbeforecurrentvectornormalization. Finallycancelonfailure. FullorderedvectorbatchreturnsBEFOREpresence/reassociationadvances. No nextframe/crosscamerastatework,no rawVAsurfaceescaped,nounboundedqueue. This isboundedwithin-frameoverlap,NOTfullynonblockingcameraanalytics;docmustnotclaimotherwise. ExistingPillowresize/color/L2same. GaitMOGlearning/selection unchanged;no once-per-framecoalescing.

Opt75localcandidateoverlayofopt74containsasyncadapter;defaultswitchstill0pendinglivequalification. RealNPU/GVAprobe8alternatingpairs of6samecrops gaveallvectorsbit-exact;postwarmupmedianssync48.111ms,prefetch45.536ms(~5.4%batchreduction) onsharedhost,notoverallpipelinecapacitygain.6async-regressiontests coverFIFO,boundedoneoutstanding,actualprep/modeloverlap,cancelonerror,oldhandle,remainderdeadline/jobmetrics. Combined46relevanttests passed. Livefive-source120srunwithprefetch1currentlyrunning. No pushes/contract/gate/modelupdates.


### Completed results: opt75 live and opt76 default-enabled candidate

Five-source opt75 live test (120 seconds, all use cases, requested 8 FPS per camera) completed with no captured fatal errors but FAILED capacity. Cumulative selected FPS: ch9 1.266, ch10 2.655, ch16 7.870, traffic1 1.846, traffic2 4.273. Capture recorded 1,718 packages, 924 evidence objects and 342 embeddings (178 scene, 80 body, 46 face, 38 gait). Live baseline and candidate observe different moments and shared host load; no causal throughput improvement or accuracy comparison is claimed. Mock capture acknowledgements do not establish real Management durability.

Built local opt76 with the same tested body-prefetch path enabled by default; internal rollback switch remains available. Source Docker version default updated to avoid rebuilding changed code under an old tag. Image localhost/sentry-v8-opt76-body-prefetch; ID d9b15e09c45cc3175e6af7e1770104b116ac952e5ca45da9e4920b76af9fc413; local manifest sha256:931eafa6e6e2204fa2200fe117ae594b59648da8a6544db50c17fd7e8102e113. Not a registry publication.

Final opt76 two-camera crowd replay, 10 FPS requested each, 60 seconds steady: healthy cameras, no captured fatal errors, cumulative selected FPS 9.795 and 10.155. Overall capacity test FAILED because one required sampled window reached only 0.8799 of target (minimum 0.90). Do not hide this failure behind the passing cumulative average. Captured 1,028 packages, 417 evidence objects, 238 embeddings (78 scene, 126 body, 21 face, 13 gait); minimum output checks passed. This establishes short functional execution, not sustained capacity or recognition accuracy. Final memory approximately 3,100 MB. Health counters and final package capture have different sampling endpoints; do not subtract them as delivery losses.

66 relevant regression tests passed; canonical and embedded V8 validators passed. Evidence, reports, compressed logs/packages, build overlay recipes and model probe are archived under optimization_evidence/2026-11-04-secondary-scheduling/. All test containers are stopped. No commit or push performed. V8 schemas, model weights, gates, tracker association and gait background-update sequence were not changed by this work.

Remaining work: main full-frame gait MOG preprocessing cost; unresolved five-camera busy throughput; native stall/crash recovery; memory retention attribution; real delivery-agent validation. Body prefetch overlaps crop preparation with inference only within one frame; camera policy still waits for its ordered result batch. A larger asynchronous policy change is NOT implemented or qualified. Once-per-frame MOG reuse remains rejected due to measured silhouette differences (section 23). Do not repeat it as a transparent optimization. HTTP/native-path and native adaptive-tracker integration gaps remain separate unresolved items. No claim of production readiness or maximum camera capacity.


## 25. Gait allocation experiment and measured substages (opt77 diagnostic)

User authorized next gait optimization investigation while preserving V8 and production behavior. Tested OpenCV MOG output-buffer reuse in exact opt76 container OpenCV4.14, one thread, identical resized crowd frames and alternating baseline/candidate order. 120 updates produced identical foreground masks and background images; output storage reused120/120. Median baseline53.2118ms versus reuse53.1882ms; p95 baseline55.8730 versus reuse55.9901ms. No meaningful improvement; buffer reuse NOT integrated. This was a CPU microbenchmark, not a live capacity test.

Added CPU wall-time substages to existing telemetry: person_identity.gait_background_update, gait_region_morphology, gait_silhouette_normalization. Existing total silhouette preprocessing span remains a parent span; do not sum parent and children. No accelerator attribution for these stages. Background call count/order/learning, morphology and normalization values unchanged. Tests verify background/silhouette equality, independent camera models, CPU-only metrics, identity emission admission and body-prefetch regressions:12passed.

Built diagnostic local localhost/sentry-v8-opt77-gait-profile on opt76, replacing only person_reid.py. ID df96e5af6055259fe942ea0c23e13c1e91953e7452cf274d913b9732a98dc78c; local manifest sha256:69bd5a0809eea99948854d4b680e72cbe788b5673897e56766a8544e00931bfa. Docker default remains opt76; opt77 is instrumentation, NOT a faster release. No push/commit. One initial container invocation failed because default rootless networking lacked slirp4netns; reran with host networking as used by existing test harness, no runtime/network settings changed in image.

Five distinct supplied live streams at requested8FPS, all use cases,120seconds steady are being measured. Interim readings confirm background update dominates preprocessing; timings are wall-time including contention on shared host, not pure CPU compute. Nothing disabled to improve reported FPS. Further results below when complete. Evidence in optimization_evidence/2026-11-04-gait-allocation/.


Completed opt77 live result: FAILED requested5x8FPS target, no captured fatal errors. Cumulative per-camera selected FPS ch9/ch10/ch16/traffic1/traffic2:2.960/5.329/8.003/2.417/3.873. All five healthy at end. Capture1,422packages750evidence279embeddings (18face37body26gait198scene),4plate_reads. Different live contents/host load than prior runs; no speedup claim. Final per-camera lifetime preprocessing wall-ms: [{"camera_id": "capacity-01", "background_update": 122.767, "region_morphology": 3.148, "silhouette_normalization": 1.605}, {"camera_id": "capacity-02", "background_update": 107.408, "region_morphology": 2.723, "silhouette_normalization": 1.795}, {"camera_id": "capacity-03", "background_update": 116.022, "region_morphology": 2.925, "silhouette_normalization": 2.078}, {"camera_id": "capacity-04", "background_update": 67.005, "region_morphology": 1.551, "silhouette_normalization": 0.854}, {"camera_id": "capacity-05", "background_update": 63.21, "region_morphology": 0.856, "silhouette_normalization": 0.691}].
The dominant measured stage is full-frame background updating, not output-buffer allocation or silhouette morphology. Opt77 remains diagnostic, source Docker defaultopt76 unchanged. Next substantial work is a bounded ordered gait collection worker with retained original frames and stale-session rejection, designed to preserve all MOG update/order, motion/history/admission decisions. This worker is NOT implemented in this turn; asynchronous execution cannot eliminate total compute cost and must not silently drop updates to make FPS look better. Once-per-frame background updates remain rejected, and no CPU/GPU inference fallback was introduced. Test artifacts archived, no push.


## 26. Ordered gait worker candidate (opt78)

User authorized proceeding with bounded gait work. Implemented an internal opt-in SENTINEL_GAIT_WORKER=1; default remains0 pending qualification. One ThreadPoolExecutor thread per camera, at most one submitted/unconsumed gait job. Every new gait submission first waits for and emits the previous result, then snapshots current identity gates and last-feature state. Camera thread proceeds with face/body and other policy work while gait runs. A frame-end barrier joins the final job before processing the next frame. This is within-frame overlap, not an asynchronous cross-frame backlog and not a reduction in MOG updates.

Worker owns the existing sequential gait collect call: same full original frame, detection order, MOG learning, masks, motion checks, sequence timestamps, qualification gates and inference. Packet/frame is retained only until frame-end join; no new frame copy. Immutable detection/identity-policy snapshots isolate inputs. Event creation/evidence encoding/persistence and last-feature updates occur on the original camera thread. Completed results from a retired session are not emitted. Failed frames wait for outstanding work and discard its output before model teardown. Native hung inference remains an unresolved risk: no claim of bounded shutdown latency for a hung native call. No output drops added to obtain FPS.

Refactored existing gait collection and feature emission into helpers; synchronous path retained as reference. Tests compare100 generated frames with two people: background image, silhouette buffers, cached vectors, sequence times, event source times/associations and exact JPEG evidence match. Additional tests check actual overlap, stale-session rejection, frame-error draining and repeated-presence snapshots observing the previous emission gate.71 runtime tests passed. This is controlled parity with deterministic model output; not labelled recognition accuracy or a full real-model event parity benchmark.

Local candidate localhost/sentry-v8-opt78-gait-worker-candidate built on opt77 with only v8_events.py replaced. ImageID2c70fc8ab42809e463e2eef9b9356bd555a3b2cdeb30373e4364a21712eb4987; localmanifestsha256:df892f5cf8478bb4146bb15526f97659e25a8f197a516a323872c0cf95cf3215. No registry publication. V8 schema, model files, production policy thresholds and source Docker version default remain unchanged. Two-camera10FPS crowd replay90seconds steady running, followed by planned120seconds five-live-source8FPS test. Results will be appended; do not interpret interim rates as qualification.

Opt78 recorded90second2x10FPS test PASSED; cumulative selected10.0357/10.0623FPS,564evidence323embeddings including19gait,1,507packages, no captured fatal lines. Gait wait/processing means: [{"camera": "capacity-01", "gait_processing": 23.494, "gait_result_wait": 22.783, "gait_background_update": 22.219}, {"camera": "capacity-02", "gait_processing": 24.912, "gait_result_wait": 24.198, "gait_background_update": 23.731}]. Most gait latency remains on critical path; no substantial speedup claim and no maximum-capacity claim. Independent same-frame gait event handoff may occur after other use-case events; source timestamps/provenance unchanged, delivery remains camera-thread-owned. Five-live-source120second test now running.


Opt78 live120second5x8FPS run FAILED throughput: ch9/ch10/ch16/traffic1/traffic2 selected1.218/3.283/7.955/2.160/3.645FPS. No captured fatal lines. Captured1,537packages846evidence316embeddings (180scene73body30face33gait),2plate_reads. One fire_smoke_suspected observation is model output, NOT a verified fire. Five streams remained processing; no capacity qualification. Gait stage means: [{"camera": "capacity-01", "gait_processing": 138.029, "gait_result_wait": 138.195, "gait_background_update": 128.996}, {"camera": "capacity-02", "gait_processing": 121.92, "gait_result_wait": 121.046, "gait_background_update": 114.087}, {"camera": "capacity-03", "gait_processing": 133.084, "gait_result_wait": 130.522, "gait_background_update": 123.648}, {"camera": "capacity-04", "gait_processing": 74.389, "gait_result_wait": 59.102, "gait_background_update": 66.709}, {"camera": "capacity-05", "gait_processing": 75.465, "gait_result_wait": 54.696, "gait_background_update": 70.696}]. Different scene contents/host load from opt77, so these rates are not a controlled speedup/regression comparison.
Decision: keep worker defaultOFF and opt78 as a tested candidate. Recorded test passed, but most expensive gait background updates remain on camera critical path and five-camera target still fails. Worker result wait includes thread scheduling and may exceed worker processing in a sampled aggregate; subtracting means is not exact overlap measurement. Small recorded average difference (~0.7ms) is not justification for a default rollout. No contract/model/gate changes, no push. Test containers stopped. Preserve candidate and tests rather than declaring this a capacity fix. Next investigation must target background computation cost with verified equivalence; do not repeat allocation reuse or once-per-frame coalescing as untested new proposals. Cross-frame async is NOT implemented: it requires defining bounded backpressure and state/result barriers without losing required updates, and cannot remove compute saturation.


## 27. Audit why earlier16-camera claims conflict with current results

User explicitly challenged apparent loss of progress and remembered16cameras at8/10FPS. Read historical reports AND recovered raw data before proposing more optimization.

V8 opt38 raw report /tmp/sentry-v8-capacity-16.52krxuzp/report.json confirms16x8 configured,127.922 aggregate reported decode/selectedFPS, but aggregate admitted inference1.024FPS, full_usecases=false,64 final captured packages consisting32camera_health+32zone_count, ZERO evidence and ZERO embeddings. Four worker processes. Historical qualification text omitted these key limits and overgeneralized capacity; this is not16-camera full-use-case capacity. Do not confuse48 durable at the earlier health sample with64 final captured packages.

Separate older V6 dev32 claim exists and must not be dismissed as the same test. Recovered /tmp/dev32-health-final2.json:16camera records,127.6aggregate reported decodedFPS,90.244aggregate inferenceFPS at that snapshot,42–50seconds uptime. Cumulative4575decoded,2680inference,1895quiet frames; outputs203scene,29counts,8object_present,2plate_outcome,2track_lost,1ended_unknown,22health; no person_feature_sample. Plate detection147calls,OCR5calls; no gait collection stage in this snapshot. This exercised some secondary analytics but does not establish crowded gait/identity capacity. Snapshot is not a sustained performance benchmark. No confirmed16x10 full-use-case result located in this audit.

Current tests differ in enabled work, activity, resolution (three1440p live cameras vs other recordings), grouping, devices/cadence, frame admission and freshness checks. Cannot conclude all decline is workload, nor conclude a regression solely from these mismatched tests. Our process error: capacity numbers were communicated without their workload qualifiers, then incremental micro-optimizations continued without a frozen old-versus-new full-workload comparison. Stale handoff heading pointing tosection18 was also corrected here.

Next decision: pause speculative optimization; use available opt38 image and current baseline on identical saved sources, identical supported use-case bindings/FPS, with raw/selected/admitted FPS, source age, actual secondary calls and outputs recorded. Separate16-camera low-demand reproduction from crowded full-use-case comparison. Account for intentional historical algorithm changes rather than claiming complete parity. V6dev33 image also exists locally for separate historical comparison, but contract differs. Do not rebuild architecture or alter production gates before identifying whether a real execution regression exists. No runtime changes/builds/pushes performed in this audit. Raw evidence archived optimization_evidence/2026-11-04-capacity-history-audit/.


## 28. Controlled old/current capacity comparison

User authorized comparison before more optimization. No runtime changes or builds. Comparing frozen opt38 (90fab432b900...) with source-default opt76 (d9b15e09c45c...), not opt78 experimental worker. Tests run sequentially on the same shared host; other programs left running. Camera configuration identical within each pair, same recordings/ports/FPS/durations. Image defaults retained to compare complete versions: tracker mode, camera grouping, plate device and implementation differ, so this is not a single-variable algorithm experiment.

Quiet pair:16x8FPS, same Camera12 recording, count+ANPR only,10secondwarmup60secondsteady. BOTH PASS with current harness. Opt38 finalselected127.920/inferred1.024FPS,6,154.75MB; opt76 finalselected127.606/inferred3.326FPS,2,968.04MB. Neither produced evidence/embeddings. Thus the current image retains the historical16camera quiet capability; earlier claims cannot be extrapolated to full-use-case crowded cameras. Container memory differs alongside grouping/defaults; no attribution of savings to one change. GPU utilization is node-wide and includes unrelated workloads.

Archive:optimization_evidence/2026-11-04-controlled-capacity/. Quiet sourceSHA25681a7d110b2cb66fcab9f76f3681c417a5fa66dce35edff70a974752b329c704e. Crowd sourceSHA2564096090af9b99a004e45dd1a1eb784f6c7e52b1de87b8a08c99137f7b75e66a2. Next pair running2x10FPS crowd/full-usecases,10secondwarmup90secondsteady. Capture per-camera cumulative selected/inferred counts, output activity and source-to-production latency where available. Snapshot FPS alone can hide source staleness.


### Completed controlled comparison and decision

All four sequential tests completed; no runtime changes/builds/pushes. Desired-state JSON exactly identical within each image pair. Both quiet16x8 tests and both crowd2x10 tests passed the harness throughput criteria. These are short runs on a shared host, not general capacity guarantees.

Crowd90second pair: opt38 cumulative selected9.969FPS each, inferred9.609/9.569FPS; opt76 inferred10.035FPS each. Final container memory opt38 3,536.15MB versus opt76 3,125.39MB. Captured opt38 233evidence136embeddings versus opt76 564evidence322embeddings. Counts cannot be interpreted as accuracy/recall because source progression and tracker/cadence/defaults differ.

Freshness evidence: opt38 source-to-production reported lifetime mean scene34.03–34.05seconds, person-feature32.16–32.59seconds; opt76 scene4.45–5.01milliseconds and person-feature0–133.24milliseconds. These are runtime PTS-to-wall-clock estimates; zero may reflect clamping/clock anchoring, so they do not establish independent physical camera latency. Across captured scenes opt38 source-time span33.27seconds per camera versus opt76 103.60/109.61seconds. Thus similar FPS did not mean similar source-time progress. Baked old graph contains gvafpsthrottle; current uses PTS-based drop gate. This supports the earlier stale-processing diagnosis but is not a single-change causal benchmark.

Decision: current baseline retains historical quiet16-camera throughput and demonstrates better source progress on the tested crowd workload. No broad regression established by these pairs; crowded1440p five-live-camera failure remains unresolved. Freeze opt76 baseline, keep opt78 gait worker off. Next capacity work should use the same busy recording and scale camera count with inference rate, source-time progress and output activity together; do not call a decode-only/FPS-only result full capacity. Avoid further architecture/queue changes until the busy limit is reproducible. Raw comparison/configs/logs and image/default differences archived in optimization_evidence/2026-11-04-controlled-capacity/. All test containers stopped.


## 29. Four-camera active-workload check on frozen opt76

User authorized proceeding from the controlled comparison. Ran opt76 unchanged, gait worker remains disabled. Four independent MPEG-TS/TCP replay connections to the same720p crowd recording;10FPS each, all-use-case bindings enabled,10secondwarmup120secondsteady. Other host programs left running. No build/runtime/contract/policy changes or push.

PASS under existing harness90% criteria. Cumulative selectedFPS across105.072seconds:9.869/9.908/10.079/9.888. Cumulative admitted inferenceFPS:9.860/9.917/10.079/9.888. Thus actual admitted inference tracked selection, not just a quiet-camera decode pass. Small above-target rolling/cumulative readings can reflect measurement boundaries/inflight buffers, not extra sustained capacity. All four healthy at final health, no captured fatal lines. Final container memory3,721.38MB. This short test is not a maximum-camera or continuous-exact10FPS guarantee.

Capture:4,039packages,1,425evidence,820embeddings:77face428body40gait275scene. ANPR unsuccessful outcomes76; no positive plate read or hazard episode in this fixture, so do not claim positive ANPR/hazard validation from it. Mock agent validated accepted package structure/references; this is not real Management retrieval/durability testing. Sampled health showed4,032admitted/durable,26backpressure responses,zeroexhausted/protocol_errors. Later final capture4,039 is a different endpoint, not7lost. Temporary backpressure was present and must not be omitted from operational assessment.

Captured scene source-time spans133.70–139.71seconds including startup/shutdown over the120secondsteady test; not a direct source FPS measurement. Reported source-to-production scene means4.8–16.3ms, person-feature0–130.0ms. These use PTS clock anchoring/clamping and do not independently certify physical latency; no older-run-style tens-of-seconds delay observed in these metrics.

Evidence archived optimization_evidence/2026-11-04-controlled-capacity/opt76-crowd4/ and combinedcomparison.json. Raw artifact/tmp/sentry-v8-capacity-4.tavxmb4e. All test containers stopped. Next informative comparison is resolution on the same source content: this720p result must not be extrapolated to the earlier five live streams with three1440p cameras. Retain frozen baseline and reproduce that heavier cost before more architecture changes.


## 30. Matched scene-resolution comparison on opt76

User authorized increasing source resolution after four720p cameras passed. Frozen opt76, all bindings/FPS unchanged, four independent TCP replay sources each,10FPS/camera,10secondwarmup120secondsteady, sequential tests on the shared host. Prepared matching720p and2560x1440 encodes from the same445-frame29.97FPS crowd recording: libx264 fast CRF18 GOP30 bf0 yuv420p, Lanczos scaling, two encoder threads, offline before measurement. Upscaled fixture tests higher pixel-processing cost; it does not recreate native1440p detail or HEVC behavior. Reencoding and larger source-coordinate subjects can alter detections/adaptive gates, so actual work counts must accompany any timing conclusion.

Fixtures/metadata SHA hashes and probe output archived optimization_evidence/2026-11-04-resolution-comparison/fixtures.json; generated fixture files /tmp/sentry-resolution-fixtures/. No runtime/model/contract/policy changes or new build. Matching720p control running first, then1440p. Compare cumulative selected and admitted inference rates, stage-call counts/timings, memory, outputs and source-time progression; don't qualify capacity using frame-FPS alone. Results appended below when complete.


### Completed matched-resolution results

720p control PASS under existing final-window/cumulative90% criteria; one earlier rolling aggregate sample dipped to35.648/40FPS and is retained. Cumulative inference10.060/9.851/10.060/10.079FPS. Final container memory3,668.09MB. Gait-processing lifetime means28.14–29.36ms,1,024–1,080calls/camera. Capture4,300packages1,389evidence807embeddings (83face409body41gait274scene). Sampled health4,296admitted/4,292durable,11backpressure,4coalesced,zeroexhausted/protocol_errors; snapshot differences are not a loss calculation.

1440p FAIL. Cumulative inference4.424/4.405/4.453/4.481FPS. Four healthy at final health and no captured fatal lines, but throughput/freshness failed. Memory5,300.85MB. Gait-processing lifetime means141.13–143.68ms,583–596calls/camera, roughly5x the720p per-call wall time (includes shared-host scheduling/contention; not pure CPU cycles). Actual stage-call counts differ, so no claim of a perfect pixel-only microbenchmark. Same desired-state JSON except external Secret contents; source fixtures have identical scene/frame-count/FPS/encoding settings but differ in resolution/upscaling/encoded bytes. Runtime adaptive gates and detections may respond differently to larger source subjects.

1440p reported mean scene source-to-production delay33.15–34.83seconds versus720p3.19–16.60milliseconds. These are PTS/wall-anchor estimates, not independently measured physical latency. Captured scene source-time spans59.43–61.43seconds at1440p versus133.70–139.71seconds at720p across comparable startup/steady/shutdown captures. This confirms much poorer source-time progress; frames still arriving/health=healthy does not establish freshness. Current PTS dropping does not prevent source backlog under this overloaded workload. Do not claim the old stale-processing problem is universally fixed.

1440p capture2,056packages912evidence518embeddings (41face325body25gait127scene).8plate_read outputs are model results on this unlabelled crowd fixture, not certified plate accuracy. Sampled health2,039admitted/durable,2backpressure,zeroexhausted/protocol_errors. Delivery endpoints differ from final capture; no subtraction as lost data. Both tests used mock delivery agent, not actual Management.

Conclusion: reproduced a high-resolution performance/freshness failure on unchanged opt76. Gait CPU background-subtraction preprocessing is a major measured contributor; this experiment does not isolate it as the only bottleneck or establish decoder-only1440p capacity. Next work must target that computation AND overload freshness, preserving V8/event associations and distinguishing background algorithm/input changes from transparent execution optimizations. Do not add more queue layers, claim a generic four-camera maximum, or disable gait silently. The five mixed live-camera failure is consistent with this result but not fully explained by resolution alone (HEVC/detail/density differ).

Artifacts archived optimization_evidence/2026-11-04-resolution-comparison/{opt76-720p,opt76-1440p}/ plus comparison.json, summarize.py and fixtures.json. Raw runs /tmp/sentry-v8-capacity-4.i1yp_pj6 and /tmp/sentry-v8-capacity-4.i8v4yh09. Fixture encodes remain /tmp/sentry-resolution-fixtures/. All test containers stopped; no code/model/contract/policy changes, no new image, no commit/push.


## 31. Matched1080p follow-up (2026-11-05 IST)

User asked about1080p after1440p result. Prepared1920x1080 fixture from samecrowd clip with matchinglibx264 fastCRF18,threads2,GOP30,bf0,yuv420p,Lanczos settings.445frames29.97FPS14.848182seconds. Metadata/hash appended to existing fixtures.json. Frozenopt76,4cameras10FPS,allbindings,10secondwarmup120secondsteady,sharedhostothersleft running. No runtime/model/contract/policy edits/build/push.

FAIL. Cumulative selected AND inferred rates across105.081seconds:7.109/6.966/7.194/7.071FPS. Fourhealthy at final health,no capturedfatal lines. Final container memory4,476.43MB. Gaitprocessing lifetime averages74.54–75.67ms/call,876–909calls/camera. Earlier readings68–70ms were interim; use final lifetime values for comparison. Final rolling aggregate49.256FPS reflects catchup/buffer windows, NOT sustained capacity above40; do not substitute it for cumulative rates.

Reported scene source-to-production mean16.40–16.85seconds; captured source-time span96.23–97.73seconds across startup/steady/shutdown. These are estimated PTS clock-based metrics, not independently calibratedphysical camera latency; together they show lag under this workload. Counts3,195packages1,200evidence713embeddings:62face414body38gait199scene.2plate_read observations unlabelled/notaccuracycertified. Sampled health3,150admitted/durable,3backpressure,zeroexhausted/protocolerrors; finalcapture at laterendpoint isnot45missingpackages. Mockagent only,notManagementretrieval/durability.

All three resolution results are in optimization_evidence/2026-11-04-resolution-comparison/comparison.json and per-run directories;1080raw/tmp/sentry-v8-capacity-4.0dd4z_1i.720p matchedcontrol inference9.85–10.08FPS/gait28–29ms;1080p6.97–7.19FPS/gait74–76ms;1440p4.41–4.48FPS/gait141–144ms.1080p has2.25x720p pixels;1440p4x. Largerframes and changing detection/policywork, shared-host contention and preprocessing jointly matter; gait CPU is a major measured contributor,not proven solecause. All use cases enabled doesnot establish every positiveusecase oraccuracy. Upscaledscene isloadtest,notnativecameraqualitytest. Four1080p cameras at10FPS are not qualified bythis result; lowerFPS/camera-count combinations not established here. Alltestcontainersstopped.


### Clarification: every-third-frame gait sampling already exists

User proposed gait on every third FPS-selected frame. Verified baked opt76 v8_events.py line1027 uses packet.index%3==0, as current source does. DLS packet.index comes from DlsMotionPolicyGate's index after PTS FPS selection and includes quiet observations; active continuous inference at10FPS gives approximately3.33gait collection opportunities/second/person before other eligibility gates. Actual opportunities can be lower with quiet/skipped frames. Existing720/1080/1440 measurements already include this cadence; do not propose it as a new optimization. Each eligible person still triggers its own collect(full_original_frame,[person]) and background.apply(fullframe) on sampled frames. Thus person count multiplies background updates. Gait collection cadence is separate from gated embedding inference and emitted samples. No runtime changes or new tests in this clarification.


## 32. User-authorized once-per-gait-frame background update (opt79)

User explicitly asked to update background once per gait frame and share mask across people. This authorizes the preprocessing change previously rejected as transparent parity. It is NOT equivalent to old per-person MOG learning. Quality/confirmation/emission gates, every-third-selected-frame collection, source-coordinate crops, original-resolution evidence, model weights, embedding profiles and V8 schemas unchanged.

Implemented collect(frame_key=(session_id, packet.index)). Background mask cached by camera plus explicit session/frame token, shape/dtype and background-model identity. One full-resolution mask per camera; no original BGR copy retained. Other eligible people on same frame reuse it, with independent crop/morphology/normalization/motion/history/sequence gates. Frame-end finally releases the cached mask even after errors, after any gait worker has joined. Camera/session/shape/model replacement cannot reuse prior mask. Missing key retains legacy behavior for standalone callers; internal rollback SENTINEL_GAIT_SHARED_BACKGROUND=0 restores per-person updates. Default1 is internal image behavior, not desired-state input. Existing asynchronous gait worker remains disabled.

Telemetry records actual background_update only on real MOG call and background_reuse on reused mask; do not count reuse as a zero-duration model invocation. Tests compare new semantics to one-update-per-frame reference over40frames/3people: exact masks/background state and oneupdate/frame, plus camera/session/shape/model isolation, rollback, stale release and error cleanup. Full runtime suite75passed; canonical/embedded V8 validators passed. These tests do not prove parity with the old learning sequence or identity accuracy.

Local opt79 overlay built on opt78 replacing only person_reid.py and v8_events.py. ImageID f82a0769ed12eff25ef7bdabc9619370ebfdceeed737eaed21e5f2381e4feb86, localmanifestsha256:03d82a6c021781484c4d476aeb229dc08bbb9068467fba701d2c157bc19e8cfa. Tag localhost/sentry-v8-opt79-shared-gait-background. Not published. Shared-host4x10FPS/1440p/full-usecase120second test running; follow with silhouette drift diagnostic and1080p. Do not claim10FPS capacity before full results. Existing known mask differences are expected under this authorized change.


### Intermediate opt79 qualification results

4x10FPS1440p120seconds: FAILED target but cumulative inference7.328–7.518FPS/camera vsopt76 4.405–4.481FPS.36gait embeddings,1,406evidence,806embeddings overall. Actual MOG calls286–294/camera;reuse679–703/camera (about70% update opportunities reused). Reported scene mean delay14.63–16.54seconds vsopt76 33.15–34.83seconds, still stale/overloaded.31handoffbackpressure,zeroexhausted/protocolerrors. No generic10FPScapacity claim. Gait mask drift probe:149/150normalized masks differ from legacy across30frames/five constructed regions;meanIoU0.2146wherebothvalid. This is substantial preprocessing drift on artificial regions, not evidence of good/bad recognition accuracy. New semantics match once-per-frame reference in tests.

4x10FPS1080p120seconds: harnessFAIL due rolling-window dips despite cumulative per-camera inference9.783/9.897/10.335/10.040FPS (rolling/snapshot alignment can create apparent above-target rates). Opt76 comparison6.966–7.194FPS.72gait embeddings,1,593evidence. Reported scene mean latency10.7–1,142.9milliseconds vsopt76 about16seconds.5handoffbackpressure,zeroexhausted/protocolerrors. Freshness improved in these runs, but not guaranteed physical latency or causal speedup under all host loads. Shared-host sequential comparisons, no other apps stopped.

Running explicit4x8FPS1080p60second check of default desired rate;do not conflate with10FPStarget. Build wrapper's stalev8-dev3 default and Docker opt76 default changed to2026.11.05-v8-opt79-shared-gait-background so future source builds cannot silently label changed behavior as an older version. Baked candidate already hasopt79 label. Production-policy gate values and V8schemas unchanged; background learning cadence changes only as user explicitly authorized. No push.


### Final opt79 default-rate result and saved state

Explicit4x8FPS1080p60second steady test PASS. Cumulative inferred7.851/7.871/7.871/8.191FPS; modest above-target value is snapshot alignment/buffered-window effect, not sustained8.19FPS guarantee. Capture2,190packages849evidence506embeddings:40face295body16gait155scene. No captured fatal lines. Estimated scene production delay10.3–16.3ms (PTS anchoring/clamping caveat remains). Sampled health2,184admitted/2,183durable,6backpressure,zeroexhausted/protocol_errors; final capture occurs later, do not subtract endpoints as losses. Mock-agent package admission, not actual Management retrieval/durability.

Full runtime75tests and both V8 validators passed. Gates/association logic/modelweights/wireprofiledimensions unchanged; adaptive resolver may still choose different effective values as input observations change. Background learning now advances once per gait-sampled frame with at least one eligible person, independent of number of people. Mask drift is expected and explicitly user-authorized; input distribution/recognition quality is not validated by performance or bitwise comparison to the old algorithm. No labelled gait accuracy claim.

No new decoder,device placement,scene sampling,body/face pipeline or async gait-worker enablement. Only two runtimefiles changed for sharedmask/key/cleanup;newtests cover reference/isolation/cleanup, and image-buildversion defaults updated. Main policy and V8schemas not edited. Original-resolution person/face/vehicle evidence retained. Implementation on branch sentry_cv_multi, uncommitted/unpushed alongside existing work; no destructive cleanup. All test containers stopped. Overlay recipe, source snapshots, tests/validator outputs, mask drift probe/results and five baseline/candidate reports archived optimization_evidence/2026-11-05-shared-gait-background/. Candidate image stays local at localhost/sentry-v8-opt79-shared-gait-background. No general camera maximum or production-ready claim. Later testing should distinguish shared-mask correctness, recognition accuracy and sustained capacity; do not repeat queue-only optimization as a solution to remaining1440p cost.


## 33. Planned isolated-host campaign requested by user

User requested testing on192.168.1.64 asadmin1 underDocuments, authorized stopping competing workloads, and explicitly asked to list tests first. Prepared ISOLATED_VALIDATION_PLAN.md with frozenopt79, host/process isolation, contract/startup/media/device tests, capacity workload matrix, all-use-case/gate/association checks, shared-gait-mask validation, delivery/retrieval, faults/recovery, bounded pressure and stability tests, instrumentation/acceptance criteria and handoff requirements. Credentials were not saved. No SSH connection, remote stop/kill, transfer or remote run performed at this planning step. Stop identified competing applications/auto-restart sources, preserve SSH/network/essentialOS/device drivers/testing runtime, record restoration information; do not blanket kill. Remote hardware/driver/agent/source capabilities remain unverified. Actual Management test requires reachable compatible agent/ingest and credentials; unavailable items must be reportedBLOCKED,not silently replaced by mock success. Tests mostly2–3minutes, final10–15minutehold; no hourlongsoakdefault. Existing75tests and shared-host results are historical evidence,not isolated-host qualification.


### Isolated campaign clarification: scene search on every camera

User emphasized total camera capacity and the balance between camera count/use cases, with scene search on EVERY camera. Updated ISOLATED_VALIDATION_PLAN.md sectionD.1: all deployment profiles retain scene JPEG+vector at configured active2seconds; quiet~5seconds measured explicitly. No passing a higher camera count by disabling/slowing/dropping scene. Add scene-floor,presence,identity,vehicle/ANPR,hazard,all-use-case and mixed-role profiles, identity/ANPR enabled fractions, actual model-call verification, per-camera scene-cadence/coverage and bandwidth/storage/agent costs. Capacity recommendations depend on resolution,FPS,activity/ROI density,role mix,freshness/output completeness/headroom, not a single N-camera figure. Record fixed graph work if disabled bindings only suppress outputs. No remote execution yet.


### Expanded isolated test requirements: latency, memory, motion, thermal and one hour

User clarified that the plan must explicitly cover all earlier optimization diagnostics plus per-stage/end-to-end latency,CPU/GPU/NPU/RAM/buffers,temperature,power/throttling,motion,resource stress and a1-hour test. Updated ISOLATED_VALIDATION_PLAN.md sectionsH–L with explicit instrumentation,thermal/power cases,mandatory60continuous busy minutes after warmup,additional overload/recovery cases,historical-regression mapping and final report tables. This explicit1-hour instruction supersedes the earlier preference against hourlongtests and the10–15minute plan. Main deployment tests retain scene search on everycamera;no changing gates/defaults or enabling experimental workers/batching during frozen comparison. No remote execution or workload termination yet.


## 34. Isolated campaign STARTED on192.168.1.64 (2026-11-05 IST)

User explicitly authorized executing all tests without stopping, including one-hour stress. Remote admin1 SSH key/control master works; persistent root console tool session94747 after sudo authentication, echo disabled, credentials NOT saved. SSH ControlPath /tmp/sentry-validation-ssh-%C. Remote root /home/admin1/Documents/sentry-validation with repo/tools/artifacts/logs/inventory/fixtures. Host CoreUltra9 285H16cores~32GB, Ubuntu24.04.4 kernel7.0.0-30, Intel GPUrenderD128/NPUaccel0;Docker,notPodman. Temporary Docker socket ACL grants admin1 access; preserved SSH,network/Tailscale,thermal/power daemons,Docker.

Stopped user sentry-cv and sentry-cv-ui;rootapexfabric-control,k3s,meshcentral,meshagent,AnyDesk,gdm/remote desktop,PostgreSQL,cron and update timers. Recorded prior services/processes/containers/kube pods in remoteinventory. K3s repeatedly returned: runtime masks alone did not override /etc unit definitions. Added temporary /run/systemd/system/<unit>.d/99-sentry-validation-isolation.conf with ConditionPathExists=/run/sentry-validation-allow-workloads (absent),daemon-reloaded/stopped and k3s-killall;K3snowinactive. Runtimeuserunit masksalsoapplied. Preserve restart/restoration instructions; no blanket system-process kill.

Initial Podman save failed due local /var/tmp diskspace; retry TMPDIR=/dev/shm streamed Docker archive successfully. Frozen sourceimagef82a0769... importedDockerID626ef9f4ff8d04ab4ce9553c43d9a754d5fa1e0dd047850774a70887d2b09aeb;ALL filesystem diffIDs identical and runtime module hashes match. Imageconfiguration normalized by transport; equivalence recordedlocaloptimization_evidence/2026-11-05-isolated-host/image-equivalence.json. No rebuild. RemoteavailabledevicesCPU,GPU,NPU. Archive remainsremoteopt79.docker.tar.

Root monitors running: monitor.py(1Hzhost/health/memory/sensors/NPU/GTcounter),intel_gpu_top engineJSON,andturbostat power/frequency/thermal. PIDfiles remote{monitor,gpu-monitor,turbostat}.pid; STOP_MONITOR flag gracefullystopsPythoncollector. Programsource scripts/isolated_validation/ transferredto remote repo. Existingharness Dockeradapter translates group-add keep-groups to992/44;runtime image untouched. Selected gallery and APIaccepted_at capture addedto TEST RECEIVER only; faultinjection modes503/429/400/401/slow/drop_ack. Source-suite support and read-only /fixtures mountaddedto TEST DRIVER only.

User clarified scope STOPS at agentAPI: verifyevents/evidence/embeddings reachvalidAPI andack/retry;downstreamManagement ingestion/retrieval explicitlyOUT OF SCOPE,nolongerblockeddependency. Testreceiver is protocol-compatible,notproductionagentimplementation. Do not claim remoteManagement durability. Initialsmoke1x8FPS fullprofile1080p60secondsPASS artifacts/tmp/sentry-v8-capacity-1.o97zcb93(remote). Isolationwasbeingsecuredwhileearlierrunstarted,sofunctionalsmokeonly,notcertifiedcapacity. PreflightbackgroundPID674670running unit/schema/bakedmodel/plugin checks andstreamprobes; logsremote/logs/preflight.log;resultsremote/preflight-results.json. Helper files srcscripts/isolated_validation; no push. Continuecampaign, do not end beforeallplannedtestsand1hourrunorclassifygenuineblocked/outofscopewithreason.


### Isolated campaign checkpoint: RUNNING, not complete

Remotecontrol: SSH ControlPath /tmp/sentry-validation-ssh-%C; rootinteractiveexecsession94747 stillalive; no credentials persisted. Remotehost192.168.1.64root/home/admin1/Documents/sentry-validation. InitialshellsessionIDs68844/89289 completed; actualcampaign is backgroundPID685612, PIDfilecampaign.pid. Earlier675425parent was paused after its child completed naturally,then replaced with resumable V2driver;no frozen runtime/model changes. V2recovers completed report from oldcase log ratherthan rerunning it. Rawledger-driver-v1.json retained. STATUS.json,CURRENT.json,ledger.json,logs/campaign.log are live progress. No final response until tests+onehour+extendededgechecks/report complete or genuineblockedcases explicitly classified.

Runtimeverification: sourceOCI configIDf82a... -> DockerconvertedconfigID626ef9...;EVERY rootfsdiffID and criticalruntimeconfigfield(User,Env,Entrypoint,Cmd,WorkingDir,StopSignal,Labels) identical. SourceSHA person_reid14f12d4f840b8856a159f7b39a579435cb1df7bfcbe72fd8593e92bb23a4692b,v8_eventsa48b8c2eb4c412614bd59d0f4ce3ae56e6c9aa89f73dbf7d4eb76b32875abccf matchdestination. HostOpenVINOCPU/GPU/NPUavailable.75tests passed in BAKEDimage with purepytestdependencies mountedread-only;earliertest-runner failures missingpy and missingharnessmount were infrastructurefixes,notCVdefects. Finalresultremoteartifacts/preflight/runtime-unit-tests-final.txt. Canonical+bakedvalidatorsPASS,model/plugininventorycaptured. Preflight-results.json initiallylistsrunnerfailure;finalretryfileoverrideswith75pass andmustbereconciledinfinalreport.

Isolation: runtime masks couldn'toverride existing/etcunitdefinitions. Temporary condition dropins in/run/systemd/system/{k3s,apexfabric-control,meshcentral,meshagent,gdm}.service.d/99-sentry-validation-isolation.conf require absent/run/sentry-validation-allow-workloads. Explicitstartk3s verifiedinactive/ConditionResult=no. Usermasks sentry-cv/sentry-cv-ui. Noacceleratorclientsbeforetests;NTP/network/tailscale/thermald/dockerretained. rootobservers:monitorPIDfile(now675084),intel_gpu_top672439,turbostat672441,thermalguard685614;use PIDfilesratherthanassumestatic. Pythonmonitor STOP_MONITOR gracefulclose;stopGPUtop/turbostatgracefullyatend. Thermalguardaborts onlytestcontainers if validreportedcrit-2C sustained10s;CPUcrit105C,observed~64C,SSDcrit~94C. Host sensorbogus65261C limitsnotused. DockerACLadmin1temporary;recordrestoreaftertests. Remote files directories monitor nowchownscasedirs1000 fordriverwrites.

Preflightsources:all3RTSP.95and6RTSPSgpu-boxpaths(includingcamera01)probeOK;TCP9003/9004/9006/9008/9010probeOK;9001/9002/9005/9007/9009timeout. Sourceavailabilityartifacts/preflight/sources.json. Actualruntime livecases queued for reachableaddresses. Testreceiver implements/v1/packages fullschema+parent/digest/sizevalidation;galleryfirst2per(camera,type,kind)actualJPEGs/fullmanifests;accepted_at UTC+monotonic added. Faultmodes503/429/400/401/slow/drop_ack. UserexplicitlyscopedcampaigntoAGENTAPI;postagentManagementdelivery/retrieval OUTOFSCOPE,notblocked. No actualproductionagentimplementationclaim.

Helpers localscripts/isolated_validation copiedremoterepo. Docker adapter namedpodman translateskeep-groupsto992/44. probe_case profiles force scene_search everycamera and2secondactivecadence;filteredprofile omits--full-usecasesharnessflagbecauseharness incorrectlyrequireszone_count forscene-only,notbecausebindingslackscene. Scenario metadata recordsactualprofile. Capacitydriver recordsselected/admitted/quiet separately. First4x1080x8rawharnessPASS butstrictactiveinference95%FAIL oncameras3/4 becausequietframesexplaingap. V2retainsstrictflagandaddsselectedcompletedcoverage withquietduty, labelscontinuous_active_rate vs motion_gated_workload_rate. Neverreportquietframes asmodelinference. Ratesonlycandidate;freshness/scenecoverageprovisionaluntilanalysis/independentmarkers. Parentresumedat8camera1080all branchafterrecoveringscale-all-4-crowd720case. CheckliveSTATUSfornewstate.

Campaign.py queues capacitybranches(full1080/720,scene,identity,vehicle,mixed),functionalclips,rolefractions,APIerrorfaults,sourceoutages,reachableRTSP/RTSPS/TCP runs,pre-stressmixed then60MINUTErunatconservativepassing1080x8point. It endsSTATUSphaseinitial-campaign-complete,notalltestscomplete. It doesNOTYETexecuteextendednative/agent/socket/CPUrestrictions/config/media/marker/gait-gallery/Kubernetesedgecases;prepare/runtheseafterinitialcampaign,orinsertwithclearsafecheckpoint. Main script nowhasmain() guardsoimportingmoduleforadditionalcases is safe,ledgerresumable. Guardrailsrootavailablememory2GiB,artifactdisk3GiB,case wall timeout;failuresstopescalationbranchnotalltesting. Everycase hasartifactcase.json,command.json,report+fullhealth+gallery+compressedlogs plus1Hzmonitor.jsonl.gz.

Extra fixtures generatedLOCALLY(notremoteperformancehost) under/tmp/sentry-validation-extra-fixtures:quiet720,motion-transition,fire-clear,crowd1080HEVC,portrait,nonaligned1278x718,fps15,fps25,latency720/1440 withbinaryframeIDbar. Mosttransferredremote fixtures; markertransfer toolsession94179 maystillpendingcheckcompletion. Frozenprimaryfixture720/1080/1440 andANPR/fire_smoke/parking/loitering/vehicle_classify alsoavailable. make_latency_fixture.py produces120seconds10FPS(frameIDs0..1199)binaryA55A/header32bitid/8bitchecksum inbottomstrip. marker_sender.py andprepare_marker_driver.py stagedremoteandtestharnessconditional branchadded. Forlatencycases setSENTRY_TEST_MARKER_REPLAY=<case epochlogdir>,SENTRY_TEST_GALLERY_LIMIT=200;profile scene orall;futureanalyze_markers.py runsinsideimage(CPUonly)decodesJPEGmarker;joinpackageID accepted_monowithproducerfirst_bytes_mono+frameID/10 toindependentlymeasureage(afterstartupfilter). Markerproducer needsSIGTERMcleanuphandler improvementbeforeuse;childffmpegbrokenpipeusuallyexitsbutexplicitcleanuprecommended. No independentmarker testexecutedyet.

Additionalcontrolledpolicytestsgeneratedbyport_policy_tests.py:30historicalbehavioral scenarios nowcallACTUALV8pipeline withCaptureStore/injectedobservations andfakevectors,notV7runtimeorSQLiteuploader. Local30PASS;remoteexecutionpendingextendedphase. Coverscounts,zones,dwell,time,presence/quiet/loss/recovery,face/body/gaitselection,ANPR,crossing,hazardclear. Mustruninbakedimagewithtestdepsandmountscripts;donotcalltheseactualmodelaccuracytests. gait_input_gallery.py staged:offlineCVdecode+bakedGPUdetector+actualMOGmasks saves30personcrop/silhouettepanels;NOTRUNyet. No GPUdiagnostic shouldrunduringcapacity/hourtests.

Stilltodo:finishinitialcampaignincludinghour;extendedtestsforconfigurationerrors/hotreload/credentialsredaction,media(local/HTTPunsupported/etc),sameTCPfanout,FPScases,clock/motion/firecleartransitions,agentabsent/socketpermissions/restartwithvalidackstate,workerpause/kill/containerrestart/CPUandmemoryboundedfaults,independentmarkerlatency,gait-inputgallery,offlineartifactconformance,sensors/healthcrosscheck,memory/capacity/freshness/thermalplots,coverageIDmappingandfinalreport/handoff. Do notclaimallplannedtestsdonebasedonlyoncurrentledger. APIscopeclarification removespostagentremoteingest/skip/Managementdurabilityrequirements. No push or runtimefix duringcampaign.

### Isolated campaign: invalid first attempt retained, corrected attempt2 running

Before accepting capacity results, discovered TEST DRIVER argparse abbreviation: --recording was consumed as --recordings, so intended fixture runs actually used default live TCP inputs. Raw reports expose source_mode=live_tcp. Those results cannot qualify controlled recording comparisons. Preserved original ledger/logs/artifacts under remote attempt1/; 0.02-second rows after thermal abort were NOT EXECUTED, and the first attempt did NOT complete the hour. Fixed only test wrapper allow_abbrev=False; added dry-run verification and fixture source_mode assertion. Frozen image/model/policy unchanged.

Actual thermal event104C against CPU critical105C occurred in that exploratory live run; retained inventory/thermal-attempt1.json. Thermal guard now rearms and subsequent cases wait for cooldown, rather than immediately cascading aborted rows. First corrected1x8 case also thermal-aborted during startup; retained, not a pass. New case IDs prefixed a2-. Second corrected2x1080x8 full-profile60second steady run completed; raw report source_mode=local_independent_tcp_replays verified. Selected/inference cumulative8.135 and8.115FPS (sampling boundary caveat); captured452evidence272vectors and1189packages, sampled1183admitted/durable. All4profiles emitted, not accuracy certified. Four-camera8FPS test running; no final capacity or completed-hour claim yet. Temperature spikes near critical require explicit final analysis.

Remote current drivers PIDfiles campaign.pid/extended.pid; logs campaign-attempt2.log/extended-attempt2.log. Root monitor continues1Hz. Extended runner reloads ledger after initial phase to avoid stale state overwrite. User explicitly clarified delivery acceptance scope ends at agentAPI; Management after that is OUT OF SCOPE. Compatible test receiver validates package schemas/digests/parent links and exact image/vector bytes; it is not evidence of production agent internal durability. No runtime rebuild, contract change or push.

### Separate temporary PL2-capped test series

Repeated stock-power cold starts thermal-aborted1x1080x8,4x1080x10 and1440p points. Stock2x/4x1080x8 passed short rate checks; no hour completed. Read host RAPL: long-term45W/window~55.97s,short-term115W,peak205W; CPU powersave governor/balance_performance. To avoid repeating unsafe cold-start attempts, temporarily lowered ONLY short-term package limit to65W via sysfs;long-term45W unchanged, image/policy untouched. Saved original115000000 and timestamp to remote inventory/power-limit-change.json; restore after testing. This is a distinct host condition, never merge silently with stock-power capacity.

Paused old parent during6camera1080case;it completed whilePL2waschanging,so marked power-transition.json invalid_for_capacity. Raw report retained; not stock/capped qualification. New drivers campaign.pid706882/extended.pid706883 use a3-pl2-65w- prefix and preserve earlier ledger. Every newcase records actual RAPLlimits in case.json. Capped sequence repeats foundations then scaling,functional,API/sourcefaults/live sources/hour andextendedcases. If hour thermal-aborts, retry fewer cameras; an aborted hold never counts60minutes. Thermalguard staysenabled. Additional testhelpers configuration_checks/gallery_audit/analyze_artifacts and live config_updates action prepared; no productionruntime changes. No push.

### Thermal diagnostic outcome: separate turbo-disabled series

PL2=65W alone did NOT prevent single-camera startup reaching105C at measured package~19–37W. Therefore do not attribute the thermal problem solely to115W boost allowance or claim power-cap fixed it. Actual root cause/cooling/firmware not established. CPU sensor and turbostat/RAPL retained; no hardware diagnosis invented. Paused next parent and disabled host intel_pstate/no_turbo (0->1), saved inventory/turbo-diagnostic.json. Kept long-term45W unchanged and short-term65W from prior diagnostic. Restore no_turbo0 and short-term115W at end. No image changes.

a3 two-camera run had CPU setting changed mid-run: power-transition.json marks invalid_for_capacity, raw retained, not a new pass. New a4-turbo-off- case series records no_turbo=1 and RAPLlimits explicitly. First full-profile1080p1x8FPS60second steady completed candidatePASS; maximum observed CPU package80C. This demonstrates safe test condition for that run only, not stock turbo capacity. Two-camera follow-uprunning. This is final bounded thermal configuration diagnostic; if single-camera foundation had still thermal-aborted, driver would markthermal-blocked and avoid further load escalation. User has not been asked to approve repeated code changes; production image/gates untouched.

Additional helpers:26valid/invalidfilesystemSecret/config scenarios locally verified againstfrozenimage (actual /run/secrets pathrequired; earlier synthetic/temp-path self-check corrected BEFOREtargetclassification). Offlinegalleryaudit validatesJPEGdecode/dimensions/SHA/parentIDs,embeddingdims/finite/L2/presence/frame. Syntheticloggingformatter checks prepared. Test-onlydevice-hidingadapter and live revision/geometry/malformed/Secretpermission action added. Extendedstandby restartedto load latesthelpers. No runtime edits/push. Still mandatoryhourandrestofcampaignpending; no completionclaim.

### Isolated checkpoint: throughput, profile-input correction and final test tools

Under a4(turboOFF,PL2=65W,PL1=45W),1x1080x8,2x1080x8,2x1080x10 and2x1440x8 short harness candidates passed.4x1080x8 cumulative inference7.95–8.07 but raw harnessFAIL due stricter snapshot/stability criteria; never call it a qualified pass from mean alone.4x1080x10 and4x1440x8/x10 failed.6x1080x8 cumulative6.36–6.66FPS;4x720x8 passed;8x720x8 failed5.62–6.12FPS and captured oneDNN CL_OUT_OF_RESOURCES(-5). No root cause proven. Most a4 CPU maxima75–91C. Full all-use-case results valid; no model/gate/image changes.

Second test-generator issue identified: filtering role bindings retained scene_trigger_use_case_ids pointing to disabled presence apps. Image correctly rejected scene4,identity4,and already-launchedvehicle4 inputs. Marked their artifact dirs invalid-test-configuration.json;these are NOT capacity failures. Fixed ONLY wrapper to filter trigger references to enabled localbindings;all9profiles(all,scene,presence,identity,vehicle,hazard,mixed.25/.5/.75) validated with actualfrozenimage against real /run/secrets paths BEFOREreruns. Generatedinputsremoteartifacts/preflight/generated-profiles.json. New subsequent roles use correctedgenerator;extendeddriver adds newIDs valid-bindings-scale-scene/identity/vehicle withprogressivecounts, preservinginvalidoldruns. No thresholdchangeorrewritingevidence.

Independent rawdecodedFPS derived from native PTSgate accepted+dropped counters shows~30sourceFPS versus~8selected/inferredFPS at lowload. Health decoded_fps alone isselected rate. Scene source-event intervals~2.00s;APIarrivalp95~2.1s at2cams,~2.4–3.0sunderheavierload,includingstartup/faultwindows;do notclaimphysical latency. analyzeresultslocallyoptimization_evidence/2026-11-05-isolated-host/analysis-interim.json (15rows,notfinal).

Marker sender now records actualvideoPES PTS + TCPsend monotonic time per frame,notonlyanestimatedepoch. Parser selfcheck26consecutiveframes passed0..9mapping. analyze_markers.py joins JPEGbinaryframeID,sourcePESsend,andAPIacceptance;rejects ambiguous120sloopduplicates;reports steadypercamera p50/p95/p99 andsignedruntime-clockresidual. Sourcefirst-PES-byte→completepackageAPIacceptance isprimarymeasure,notcamera-captureorManagementlatency. Marked720p1cam/1440p4camtests queued inextendedphase,notyetexecuted.

Extendedtools nowinclude26filesystemconfigcases,30injectedactualV8policyscenarios,syntheticloggingformatterchecks,missingGPU/NPUfaults,liverevision/Secretpermissions,worker/agent/socket/restart/CPU/memory/statefaults,45secondworkerpause(hangsurrogate,notactualsingle-modeldeadlock),21camgroupboundary,groupingdiagnostics,TCPfanout5,reusable-sourcecameraadd/remove,one/allunavailablestartup,HTTP/local/HEVCnegativecases,emptycontrol4k1FPS,empty→busy→empty8x10loadramp,focusedsharedbackground0/1comparisons andofflinegaitinputgallery. No mainproductionprofile silentlydropsscene. Added APIcommon-endpointdrainreconciliation:pausenativeproducers,drainCVscheduler,compareadmitted/durablewithaccepteduniqueIDs/objectIDs/contiguoussequences,thenresume. Avoidsubtractingearlierhealthcountsfromlatercapture.

Preparedpost-performancekubernetes_check.py + reusable_sender.py forDISPOSABLEk3sdata-dirandAPIport6444,neveroriginalkubeconfig;importfrozenarchive,ConfigMapdesired,SecretURLs,APIreceiver sidecar,sharedUnixsocket,nonroot10001withprivilegeddeviceaccess,/readyzand/healthzprobes,cgroupsmetrics. NOTRUNyet;mustrunonlyafterperformance/hour/extendedcompleteandcleanupitsowncluster. Actualproductiondevice-plugin/nonprivilegedmode NOTclaimed.

Helper-onlyanalyze_artifacts.py/gallery_audit.py/render_report_tables.py prepared. No finalreportyet. CPU/memory table legacy_mb values areMiB(1024^2);GPU nonRC6 activity isnotenginecompute%;rootintel_gpu_topcontainsconcatenatedJSONobjectswithperioddurations forofflineengineanalysis. Rootmetrics/turbostat continue. MainPIDfilecampaign.pid709062;extended.pidchangeswhenstandbyreloaded(usefile,currentlatestaftercopy). RootPTYsession94747stillrequiredforKube/powerrestore. Localrootfree~598MiB:keepfullrawartifactsremote22GiBfree;mirrorselectedreports/plots/handofflocallywithdiskguard. No commit/push.

### Marked full hour and input/fault coverage finalized

Maina4progress~35completed: correctedmixed4x8(50%identity/50%vehicle)PASS,8mixedFAIL;fourcam25%and75%identityrolepointsPASS. All retain scene. Inputinvalidscene/identity/vehicleoldrunsretainedasINVALID_TEST_CONFIGURATION;correctivevalidatedrolebranchesqueuedearlyextended. API503/42915secondfaultseachrecorded4exhausted,~189backpressure,0protocolerrors. IMPORTANT:V8contractexplicitretryPolicy6attempts,.25/.5/1/2/4delays,exhaustedActionrelease_memory_and_do_not_claim_produced. These areexpectedpreACKunproducedexpirations,NOTproofacceptedJPEGslostoracontractviolation. Do notdeclarefaultFAILjustbecausecapacityharnessreturn2. No mismatchedsnapshot subtraction.

Preparedmarkedhourinputscrowd1080p30(3600frames120s)andANPR720p10(1200frames120s),transferredduringfunctional-loiteringcase(excludethatperformancefromcapacityclaims);sameunderlyingactivity/resolutionsandapproxprincipalrates,crowd29.97->30exact. All12first/intermediate/last encodedJPEGmarkerchecksPASS;PESparser10and30FPSsequentialindexselfcheckPASS;continuousalignment180samples/3loops exact200msandlonggapambiguityrejectionPASS. Sourcesremote fixtures/latency-1080p30.mp4,latency-anpr720p10.mp4. No producerCVencodingduringstress(preencodedstream-copy).

probe_case.py interceptsONLYstress-60min-allcase toselectmarkedalternatingcrowd/vehiclefiles,createartifact sender-epochs,enableSENTRY_TEST_SCENE_GALLERY_LIMIT4000(othergallerykindslimit2),andrecordexternal-marker-instrumentation.json withoriginalargvandactualfilenames. Parentcaseconfigstillnamesunderlyingcrowdfixture;useinstrumentationandepochfilesforactualsourceinventory. Testharnessmarkerbranchfixedtohonorper-camerarecordingssuite. SourceFPS/framecountreadbyffprobe,videoPESsendloggedperframe. analyze_markers.py nowunwrapps120srepeatsinorderedcamera/session/frameordinalsequence;ambiguousgap>=60sorimplausibleprogressnotguessed. FullhourcanthereforemeasureindependentscenePES→agentacceptancefreshnessthroughout,notonlyruntimeclampedage. Definitions/limitationsinREADMEandJSON. Extra targeted2cam1080p30markedlatencycasequeuedalongside720p1cam/1440p4cam.

ReceiverTESTerrorpayloadsnowcontract-validcodes503durability_unavailable,429agent_busy,400/401invalid_package;validatedBEFOREmainfault503(preparehelpermtime21:59:56UTCvs503start22:03:37UTC).401isunexpectedHTTPstatustest,nolocalcredentialdependency. Addedtestreceiverauditfieldsconfig_revision/source_frame_id/stream_session_id/use_case_id;applied22:07:39UTC,soearliercaptureslacktheseextraauditfields;wireunchanged. Patchedremoteactualrun/v8-live/mock_delivery_agent.pyandharnesssourcesneedarchivingasreproducible testtools,notonlylocalpatchrecipes.

Extendedcoveragefinalizedwithraw5simultaneousTCPreaders+singlefollowupafterfanoutimage,toavoidunsupportedblameofsource;corruptMPEGTSpeer;unreadableprimarymodelbindmask;/httpsnegative;liveSecretURLrotation+revision(usingreusablesources19111crowd1080,19112ANPR720)withfullscenegallerytoevidenceactualsourceandoutputrevision. Suspectedfromcodethat_hot_reload_signatureusesSecretpathnamewhileworker_skipsonresolvedURIdiff:NOTYETREPRODUCED,donotstateconfirmeduntiltest. Existinggeometry/revisionupdates,secretspermissiontestsremainseparate.

Kuberneteshelperhardened:explicitEMPTYconfigandcleanK3Senv+separate token/data-dir/API6444,neverloadoriginalclusterconfig;hostNetworkpodnodeNameset,noCNIqualification;privilegeddeviceaccess10001UID,config/Secretprojections,/readyz&/healthz,referenceagentsidecar. Testsrevision2ConfigMapprojectionandactualAPIconfig_revision2;150sprojectiondeadline. Cleanupownpods/processdescendantsonly,neverblanketcontainerdkill;generatedtoken/kubeconfigmustNOTbeincludedinpublicreportarchive. Notrununtilallperformance/hour/extendedfinished. RootPTY94747stillneeded.

Finalhelperfilescopiedremoteandextendedstandbyrestarted;usePIDfiles,notoldnumbers. Sourcecode/image/models/contracts/gatesunchanged;noCPUfallbackenabled,experimentalworkersstillOFF,noimagebuild/push. Continueallremainingtestsandhour;hournothappenedYET. Fullrawartifactsremote(localfreedisk~450MiB);mirrorcompactreports/plots/selectedexamples. Finalplots,coverageclassification,latencyalignment/fullgalleryaudit,Kubeexecution,gracefulmonitorstop,originalPL2/no_turborestore,DockerACLcleanupandrestorationinstructionsstillREQUIRED.

### Standby postprocessing and current API recovery findings

Rootpostprocess.py startedasrootPID790372(use root-postprocess.pid),waitingforSTATUSextended-complete. ItwillTHENrundisposableKubeintegration,offlinefullgalleryauditandallmarkeranalyses,stopmonitorsgracefully,generateanalysis/tables,verifyfrozenimageID,andrestoreoriginalPL2/no_turbo/DockersocketACL. Itendsmeasurement-complete-report-pending,NOTcomplete;finalplots/expected-faultclassification/report/handoffremainmodelwork. Competingappsremainstoppedperuser;restoreinstructionsstillrequired. No Kube/model/offlineauditwillrunduringperformance/hour. RootPTY94747stillaliveforrepairs. Ifhelperfails,inspectlogs/root-postprocess.log androot-postprocess.json. Kubehelperdoesnotload/etc/rancher/k3sconfig:explicitstandaloneemptyconfig,newtoken,data-dir,API6444;noexportoftoken/kubeconfig. ConfigMaprevision2projectionandactualagentobservationrevision2tested;hostNetwork/privilegeddeviceaccessONLY.

APIfaultresults:400explicitnonretryablerejection195protocolerrors/exhausted,401unexpectedstatus196,asdesigned;503/429fourunproducedexhaustedeachfinitebudget;slow2sresponse173backpressure,0exhausted/protocolerrors;lostACK184duplicateACKs,0exhausted/protocolerrors. DuplicateACKhandlerchecksidenticalpackageID/digest/bytesandsameallocatedobjectsequences. Thesearefaultbehaviorresults,notuniformFPSqualification. Actualagent/ManagementDBdurabilityOUTOFSCOPE;compatibleAPIreceiverfullschemas/bytechecksareboundaryproof. ContractV8lines261–267explicitexhaustreleaseallowed;donotmistakeunproducedpreACKexpirationsforlossofacceptedimages.

Mainreachedsourceoutages5/15/30after38cases(UTC22:19thirdoutagestart);next14reachableliveURLs,pre-stress,andmarkedhour. No hourcompletionclaimyet. Foregroundrootmetricscontinuing,NoTurbo1/PL265/PL145unchanged. Correctedscenetriggerprofilegeneratoractivelyusedforfuturecases. Extendedstandbyrestartedtoloadfinalizedlist;use extended.pid(renewedafterlastsync),notstaticoldnumber.

Addedplannedmixed/fractionalFPSinputcase3cams[4,8.5,10],fullbindings;harnessuniform24FPSassumptionisNOTthiscaseactualtarget22.5andmustnotdriveitsverdict. Actualdesiredconfig/healthpercamtarget/ratesmustbereconciled. Sourcecode surface_stream.py fps=int(proc.get('fps',...))maytruncatefractional8.5;requiresactualtestbeforecallingreproduced. Addedone/all-sourceunavailable,corruptMPEGTSpeer,sourcefanout raw5+1diagnostic;noautomaticblameofsourcepublisherwithoutcomparison. NewliveSecretURLrotationcasechecksactualpixelresolution/APIrevisionagainstcontrollerappliedrevision;possiblepathname-onlyhotreloadgapnotyetconfirmed. Othernewtestslistedpreviouslyqueued,NOTdoneuntilartifactsproveit.

Houractualinputoverrideonlytestwrapper:markedcrowd1080p30andmarkedANPR720p10,sceneJPEGgalleriesall4000limit,normalotherkindslimit2;senderPESperframeallcycles,externalmarker-instrumentation.jsonactualfilenames/parentoriginalargv. Restofimage/model/gate/contractunchanged. Allrootpostmarkersanalyzedwithcontinuousloopunwrappingandexplicitunknownonlonggap. Localgeneration/selfchecksfinished,remoteallmarkedfilespresent;nofixtureencodingduringhour. Rootlocalfree~450MiB;fullrawremoteprimary,copycompactfinalevidenceonly.


### Isolated campaign continuation: genuine marked hour now running

User explicitly confirms acceptance boundary is the agent API. No downstream Management uploads or snapshot retrieval are required. Validate complete package links, JPEG/vector bytes, schema, digest, retries and acknowledgement only; receiver acceptance is not proof of a separate agent database's durability.

First marked-hour startup failed because test fixture encoder emitted yuv444p / H.264 High 4:4:4. Marked invalid-test-fixture.json; it did NOT run an hour. Fixed TEST fixture generators to explicit yuv420p, retaining original bad inputs. Frozen-image VA decoder checks: original 444 fails, all four corrected 420 files pass. This was a fixture correction, no production-image modification. Artifacts marked-source-va-check/results.json.

Corrected marked two-camera 8-FPS preflight completed 60 seconds steady, PASS. Independent marker audit: 89 scenes, zero missing/ambiguous markers; steady camera1 p50/p95/p99 288.57/397.34/455.89 ms, camera2 457.03/515.58/559.15 ms. Definition: first video-PES socket send to complete package acceptance at agent API; excludes physical capture/encoding and all post-API work. Offline audit helper needs user0 for private host artifacts; production image remains UID10001. Zero-gallery audit is explicitly rejected.

Actual hour case a4-turbo-off-stress-60min-all-2x8-yuv420 began 2026-11-04T23:05:04Z, with startup/warmup preceding 3600-second steady hold. At 23:11 UTC ready=true, both workers alive. Actual sources: marked crowd1080p30-420 and ANPR720p10-420; every scene JPEG captured, independent PES timestamps retained. Host no_turbo=1, PL1=45W, PL2=65W. Current controllers priority-hour.pid=817036 and root-postprocess.pid=817050; use PID files thereafter. Extended runner will restart only after genuine hour completes; no parallel GPU/model tests during hour. Prior extended PID is stale, intentionally stopped and completed case recovered. All coverage, Kube integration, offline audits, final report/plots and temporary power/ACL restoration still pending. Do not report hour complete until >=3580 seconds of actual samples are verified.


### Continuation instrumentation and final comparison preparation

At23:17UTC actual marked yuv420 hour remains running, both cameras healthy near8FPS, container memory~3305MiB, no decoder errors; no completed-hour claim. Agent acceptance counts in an earlier23:14 sample9339/9339, no pending/exhaustion/protocol errors. Do not quote these as final totals.

Added TEST/OFFLINE helpers window_rate_audit.py and gpu_engine_analysis.py. Counter-delta windows matter: some raw harnessPASS cases (e.g.4x720p8,scene8x1080p8,identity4x1080p8) have slower10second intervals; final capacity MUST distinguish cumulative mean, raw last-sample harness result, and campaign all-window criterion. No false PASS by taking means or rolling2second snapshots. Independent freshness still required. Invalid-fixture/power-transition flags now included by analyze_artifacts/render_report_tables.

Test receiver completeness strengthened for FUTURE receiver processes only: exact set equality for all promised evidence/embeddingIDs and duplicate-ID rejection. Currenthour receiver already validates schema/parentmembership/hash/bytes; fullscene gallery provides exact promise audit there, othermodalities sampled. No hotreload/restart of hourreceiver; no productioncode/wirechange. prepare_receiver_completeness.py records reproduciblepatch.

Frozen opt76 baseline exists local (localhost/sentry-v8-opt76-body-prefetch, d9b15e09c45c...). Preparing Dockerarchive in/dev/shm, TMPDIR=/dev/shm/sentry-validation-image-tmp; firstsave default/var/tmp failed diskfull and auto-cleaned temporaryfile, remotehour unaffected. Secondexportpendinguse toolsession37825. Need afterexport start LOCAL reference_transfer.py standby; it waits remoteREFERENCE_IMPORT_REQUEST.json then streams archive throughSSH todockerload only at idleboundary, verifiesallrootfsdiffIDs+criticalconfig, writesREFERENCE_IMPORT_READY.json. extended_campaign.py now has finite pairedfrozenopt76/opt79 checks at2x1080p8 and4x1440p8 afterrollbackcomparisons and beforegaitgallery. Itwaits600s forimport; rootpoststillwaitsextended-complete. ArchiveRAM/tmpfilesmustbecleanedafter verifiedimport. Do notrunimportconcurrentwithhour/measurements. No additionaltestcasesbeyond this plannedreferencecomparison.

Localcompactreports/health/case metadata+ledger mirroredunderoptimization_evidence/2026-11-05-isolated-host/compact (~20MiB); raw staysremote. Localfree~334MiB,remote/home~21GiB/root14GiB. Do notcopyfullraw/multipleGBarchiveslocallyondisk. Root94747stillalive; priority817036/rootpost817050running. Competingapps remainstopped; powersettingsrestoreonlyaftermeasurements. Finalreport,coverage/reconciliation,plots,source-ratepostcheck,kerneljournalandcleanupstillpending.


### Actual hour mid-run: concrete gait state-retention finding

At2026-11-04T23:29:04Z (roughly24minutes fromcase start), crowd camera health shows gait_retained_presences544, gait_retained_silhouettes7408 (~20,860,928payloadbytes), gait_retained_track_centers2074. Vehiclecamera13presences/58silhouettes/208centres. SharedprocessRSS2762.14MiB/PSS2704.73MiB; currentCVcgroup~3320MiB. Earlier shortpreflight~50presences is a DIFFERENTRUN, not the hour's baseline; use hour1Hztrace to quantifygrowth. Saved checkpoint-24min.json inhourartifacts.

Verifiedactualfrozenmatchingcriticalsourcehashes: person_reid.py14f12d...,v8_events.pya48b8c... . OpenVINOGaitExtractor.collect appends perpresence buffers/times/cache and per(camera,trackID)centres; DlsGaitExtractor initializesdictsand closeonlyengine.close. Lifecycle expiry removes tracks/presences but no gait eviction/pruning call found; camera reconnect clears lifecycle registries but not gaitdicts. This is a concrete unprunedstate-retention gap, not proof fullheapgrowsindefinitelyfromonepoint. Continuefullhourandderivecounterslope/lastintervals. Do notsilentlyfixorrebuildundercampaign. Foregroundonce-per-framecache release tests are separate and pass; thisfinding concerns long-lived histories/centres, not sharedmask perframecache.

Test-receivercontractchecks8/8PASS locally againstfrozenimageCPU-only+realCVscenepackage:valid201,identical200samesequences,missingpromisedimage/vector,truncatedbytes,wrongparents,reusedIDwithchangedbytes409. Results mirroredremoteartifacts/receiver-contract-checks/results.json. This validates testboundaryoracle, notproductionagentSQL. Futureexact-IDreceiverpatchalreadyonremotediskfornextcases, runninghourloadedreceiverunchanged. PlanF01/F02nowexplicitagentAPIboundary/ManagementOUTOFSCOPE.

opt76 RAMarchive successfullyexported3.3GiB afterTMPDIR movedto/dev/shm; localreference_transfer.py standbyPID3690460, logoptimization_evidence/2026-11-05-isolated-host/reference-transfer-standby.log, PIDfilethere. Itwaitsremoteimportrequestlatertoidletransfer, notduringhour. CleanownedRAMarchiveafterverifiedimport. Newofflinehelpersfault_summary.py/window_rate_audit.py/gpu_engine_analysis.py/plot_report.pyprepared; rootpostalreadyloadedwillnotautomaticallyrunnewhelpers, executeafteritfinishes. analyze_artifacts.pyfuturecallnowaddsSIGNEDobserved_at→accepted_at agepertype,withnegativevaluespreservedandexplicitnonindependentclock caveat.


### Reproduced producer-state bug after explicit handoff admission rejection

New controlledCPU-onlydiagnostic rejected_feature_diagnostic.py ranagainstfrozenopt79 actualbakedV8EventPipeline._emit_feature_candidates, noGPU/no models. Store.persist explicitlyreturnsFalse forfirstqualifiedbody/gaitsample; actualproducerstillsets last_feature=(quality.5,time100). Restorestoreadmission, supplysamequalifiedsampleat106(beyond5secondcooldown):attemptcountremains1,zeroaccepted. Bothbody/gaitreproduced. Localresultsoptimization_evidence/2026-11-05-isolated-host/rejected-feature-diagnostic.json; targetrerunqueuedimmediatelyafter30policytests inextendedphase.

This proves rejection suppresses a subsequent equal-qualitysample; it doesNOTtestpost-admissionACKexhaustion orprovehistoricalManagementimagefailurerootcause. _persistcurrentlydiscardsstore.persistreturn andgenericfeaturepathalwaysadvanceslast_feature. Countersseparatelyrecordadmitted/rejectedcorrectly. Fixshouldpreservepending/producedsemanticsandavoidconsumingbest-sample/cooldownonfailedadmission; doNOTsilentlypatchproductionimageinthesefrozenmeasurements. Reportasactualrecovery/production-policygap,evennormalAPIhandoffisworking. Frozenhashesunchanged. No newimage,contractedit,push.

At23:31:27UTC ~26minhour:27468admitted=durable,0pending/exhausted/protocolerrors,7backpressure;bothhealthynear8FPS. Cgroup3352.66MiB;crowdgait593presences,vehicle15. Finaltrend/freshnessstillpending. No claimedhourcompletion.


### Critical correction: cached telemetry counter timestamps

Initial window_rate_audit.py used HTTPpollmonotonicdtforcamera-cachedcounters, whichcanfabricate~6.4FPS10secondwindowsfrom~8secondsworthof8FPScounters. Reproduced scene4camera03:counter650at22:52:53.470540Z→716at22:53:01.617454Z, actual8.146914seconds, rate8.1012FPS;pollinterval10.007sincorrectlygives6.595. ThisisAMEASUREMENTMETHODERROR,notimageperformancechange. Preserve rawharness/ledger, butDONOTreportthosefakewindowdipsasruntimefailures.

Correctedofflinewindow_rate_audit.py matches EXACT(decoded,inference,quiet)countertuplesto1Hzmonitorrecordswithinsample±3s,usescameraobserved_atdenominator,checksstarted_atcounterepoch. Missing/reset/outside8–12sintervalsremainUNQUALIFIED(None),notfakePASS/FAIL. Cumulativecorrectedratesaddedseparately;rawpollratespreserved. Finaltables/plotsMUSTusecorrectedratesandclarifyapprox~10secondwindows;rawpollsamplesarenotexactmodeltimestampmeasurements. GPU/PESmarkers areindependentandunaffected.

Verifiedcompletedexampleswithmatchedtimestamps(allqualifiedwindows):4x1080all8FPSmin7.389;4x720all8min7.587;scene4min7.915;scene8min7.323;identity4min7.544. ThereforeearlierhandoffstatementsclaimingstrictwindowFAILfromrawpoll-deltaareSUPERSEDED. Stillneedcumulative95%andindependentfreshness/scenecoverage/headroom. Mainhourcount2wasconservativechosenearlier;notahardwaremaximum. Replacealreadyplannedtargeted1080p30markerrepeat2camswith4camstoqualifytheactualboundary;houralreadycovers2. Noimage/gatechange. Extendeddriverupdatedbeforeitloadsafterhour.

Kuberneteshelperhardenedbeforeexecution:API6444andagentLB6446avoiddefaultLB6444collision; separatekubeletrootunderk3s-disposable,ports10260/10258. k3s localCLIhelpverifiesoptions. Originalglobal/etc/rancher/node/passwordprotectedinROOTPRIVATEinventory/original-node-password.private-backup(mode600),fingerprintJSONnoactualcredentialprinted; helperfinallyrestoresifchanged. NEVERcopy/privatearchivebackup; removeafterverifiedfinalpreservation. Kubeconfig/tokenfilesalsoPRIVATEandmustnotbepublished. DisposableclusterNOTRUNyet, onlyafterperformance/extended. ProductionK3sremainsstoppedguarded.

At23:36:41UTC ~31minhour:bothhealthy,noerrors/reconnects,33990durable,1coalesced,0exhausted/protocolerrors;3350–3390MiBmemory. Correctcoalescingaccountingratherthanadmittedminusdurableasloss. Allremainingmeasurements/report/restorestillpending. Localreferenceimportstandby3690460valid;RAMarchive3.3GiBwaitingidlephase.


### Continuation checkpoint 23:50UTC: final audit automation, hour still ongoing

Actualhoura4-turbo-off-stress-60min-all-2x8-yuv420~45minuteselapsed, NOTfinished. Bothhealthy/noerrors/reconnectslastchecked. At23:47:40:47874durable,2coalesced,0pending/exhausted/protocolerrors;memory3397.55MiB. Memoryrecentlyreleased~50MiBafterpeak3414+,sodonotclaimmonotonicfullheapleak. Gaitoldhistoryretentionstillseparateconfirmedissue. Plotfulltrace/latewindowsbeforeconclusion.

Rejected-featurediagnosticextendedtoFACEaswellasBODY/GAIT: allthreeupdatequality/cooldownafterexplicitadmissionFalseandrefusesamequalityafterrestore106sec. Facecandidateflush102updatesstateanddeletesqueuedfacecandidateafterrejection. LocalJSON3modalities,remotehelperupdatedfortargetrerunafterpolicy30tests. Noactualmodelfallback/inferenceinthiscontrolleddiagnostic.

TimestampcorrectedwindowauditnowalsoexportsmatchedpercamcumulativeFPS/actualintervals,cumulative95%criterion,andintervalrecordsforplots. Missingmatches/reset/outside8–12secsstayNoneunqualified. plot_report.pyandrender_report_tables.pyupdatedtousematchedrates,rawharnessflagkeptseparate. MAINanalysisJSONstillkeepsrawpollrates;don'tuseforfinalcapacitywithoutwindow-rate-audit.json. Rootpostcallsanalysis/tablesBEFOREnewaudit; finalstandbyrerunstablesafteraudit.

Rootfinalofflineauditstandbyfinal_audits.pyPID872847(PIDfilefinal-audits.pid;loglogs/final-audits.log)waitsONLYmeasurement-complete-report-pending fromrootpost817050,thenexecuteswindowaudit/GPUengines/faultsummary/hourtrace/correctedtables/later40secsource-onlyprobes/kerneljournal,archivesactualpatchedtesttools+canonicalschemaswithoutSecrets,verifiesoriginalPL2/no_turbo/nodecredential,removesprivatebackupifpreserved,recordsservices-after. EndsSTATUSpost-audits-complete-report-pending:manualreport/plots/coverage/JSONverdictinspection/cleanupSTILLREQUIRED. ItdoesNOTdeclareusergoalcomplete. numpy/psutilavailabletargetuserandroot;pandas/matplotlibnotinstalledremote,useLOCALplotting.

case_actions.pycommonendpointreconciliationnowaccountsallowedcoalescing:expectedaccepted=admitted-coalesced-exhausted,requires0retained/noexhausted/protocolerrorsplusuniqueIDs/objects/contiguoussequencesandAPIstatuscount. Donotcalladmitted-minusdurablea losswhenpackageswerevalidlycoalesced. Currenthour'sloadedreceiverunchanged;futurecasesexactpromise-IDchecksstrongerpatch.

KubehelperportedseparateLB6446/kubeletroot+ports10260/10258,willruncorrectlyonlyafterallperformance. RootnodecredentialprivatebackupMUSTNEVERarchive/copy;final_auditsremovesafterverifiedpreservation. ExistingrootPTY94747alive. Rootpost817050waitsextendedcomplete;localreference_transferstandby3690460waitsreference-import-idle;priority817036runninghour. Extendedpidfilecurrentlyoldstopped793391untilprioritylaunchesnewafterhour. UseSTATUS/CURRENT/PIDfilesnotstaticPIDsafterphasechanges.

Next:letremaining~15minutesactualhourfinish;verifyprioritylaunchesextendedandobserveallfinitecases;inspectfailures/drivererrorswithoutalteringimage;letrootpostKube/fullgallery/markers/restorationthenfinalauditscomplete;downloadcompactreportdata,plotlocally,viewgait/evidencepanels,manuallyclassifyexpectedfaults/unsupported/provenissues,reportallpendingplanitemsandlimitationshonestly,cleanupOWNprocesses/temporaryRAMarchives/privateclusterdatawhereappropriateandprovideapprestorationinstructions. No rebuildorpushrequested. Fullrawremoteprimary;localfree~329MiB,tar3.3GiB/dev/shm, don'tcopyrawGBstolocaldisk.


### Genuine marked hour COMPLETE; extended resumed

Actualcasea4-turbo-off-stress-60min-all-2x8-yuv420completed2026-11-05T00:05:49UTC.360steadysamples,first→lastspan3590.232s(acquisitionevery10s;realconfiguredsteadyhold3600s),elapsedcase~3644.9s. RawharnessPASS,0fatallines/abort/exhausted/protocolerrors;bothdecodershealthy0reconnects. Rawpoll-basedhourmeaninference7.9811and8.0006FPS; matchedtimestampfinalauditstillpendingbutlongdenominatoruncertaintysmall. Frozenimage/gates/hostpowerconstant.

CAPTUREfinaltotals70913packages,20761evidenceobjects,6410embeddings:face547/body2024/gait211/scene3628. Events:camera_health242,line_cross1896,object_present8764,person_feature_sample2782,plate_outcome2909,plate_read2410,presence_ended_unknown6866,presence_recovered16724,scene_sample3628,track_lost23629,zone_count1061,zone_entry1,zone_exit1. Theseproveexecution/bytearrivalnotaccuracy. HealthACKsampleDIFFERENTcutoff70885admitted/70883durable/2coalesced/22backpressure/0exhausted/protocolerrors;don'tsubtractcapture70913vs70883asloss. SeparatecommonendpointreconciliationwillprovidealignedIDproof.

Memoryfirststeady3161.88MiB→last3422.27MiB(+260.39);some~50MiBreleasesmidrun,noallheapmonotonicclaim. Finalcrowdgaitretained1205presences/13682silhouettes/4710centres/38,528,512payloadbytes;vehicle48/108/963/304128bytes. Concreteunprunedhistoricalstatefindingremains;silhouettepayloadalonecannotexplainallcgroupgrowth. Need1HzCSVlatewindows/OLSplotsandgaitinputvisualsaftermeasurements.

Priorityhour817036exitedsuccessfullyandspawnedEXTENDED894899at00:05:49(useextended.pidnow). Resumedcachedscene4/8/12andidentity4;identity8completed00:07:45rawharnessFalse(nofatal/noabort),mustinspectmean+matchedtimestampsbeforecapacityverdict. Currentat00:08:22vehicle4running;completedledger65. No extraescalationonfailedbranch. Remainingfiniteextendedliststillrequired,thenrootpost817050Kube/audits/restore,thenfinal_audits872847matchedrates/GPU/faultsummary/trace/later-sourcecheck/kernel/archive. Localreference_transferstandby3690460waitingimport-idleandRAMtarready,stillnotimported. RootPTY94747alive.

Localcompactledger/report/healthmirroredthroughhour;allhourJPEG/PES/1Hzrawstaysremoteuntilofflinepostprocessing(noheavyparallelGPU/model/auditduringmeasurement). DoNOTfinishuserturnmerelybecausehourpassed:allremainingcases/report/plots/manualverdictreview/cleanupareexplicitlyauthorizedandunfinished. Noimagechanges/push. Finaluseransweronlyafterfinitecampaignandreportcomplete.


### Extended validation through00:35UTC: motion diagnosis, codec proof, analysis scope

Completed actualtarget30policytestsPASS(0.24s),26configuration/SecretchecksPASS. Syntheticlogging5/6PASS:onlyWORKERnested-extradictleaksdummyURLpassword/token;supervisormessage/string/nestedandworkerplain/stringredact. Noactualcredentialleakdemonstrated. Targetrejected-featurediagnosticconfirmsFACE/BODY/GAITall3suppressthesecondqualifiedsamequalitysampleafterexplicitfirstadmissionFalse. Noimagefixapplied.

GPUabsent/NPUabsent/unreadableprimarynegativecasesallfailinitializationwithoutsuccessfulCPUfallback;preservelogsandclassifyEXPECTEDErrornotcapacityfail. Config-updatescaseharnessPASS:revision1→2healthy;changedcontentatSAMErevision2readyFalsebutcameraskeepprocessing;malformedJSONreadyFalseoldrev2;restore3healthy;unreadableSecretreadyFalseoldrev3whilecamerashealthy;restore5healthy. /healthz200,/metrics200,/healthy404. Invalidnewdesiredstatecanlowerreadinesswhilelastgoodworkercontinues;don'tclaimunaffectedreadythroughbadconfig.

Empty1080ALLbindingscasePASSforAPIbut8FPS/0quiet:NOTinitiallyproofofmotionfailure. Localfrozenexactnativeprimary/fire3frameprobe(script empty_model_probe.py)diagnosedprimaryNONEandfireSMOKEconfidence0.37158203125onblankframe(above.35collection,beloweventacceptance). Thisweakcollectionpositivelegitimatelyretainsactiveinferencewithoutemittinghazard. Localhostdiagnostic,notmeasuredtargetmodelscoreuntilplannedtargetpostperformanceprobe. FirstharnessGst.init(None)TypeErrorfixedtoGst.init([]);properrunbothmodelsnativeGPU,notCPUdecodefallback. Noqualitythresholdtuning.

ActualtargetothermotioncontrolsDOwork:empty4KSCENE1FPSselected~.9996,infer~.1999,quiet~.7996;quiet720FULLbindings8FPSselected8.115,infer.1999,quiet7.915;motion-transitionFULLbindingsselected7.959,infer2.836,quiet5.122,31scene/31presence/28identityevents;fire-clearhazardprofileselected8.083,infer2.449,quiet5.635,2suspected+2clear,34scenes. AlltheseharnessPASS/0fatal. Explicitlysupersedesanyclaimempty1080provedgatebug. `END ... False`incontrollerlogsisPRELIMINARYbeforec.score;ledgerharness_passedandclassifiedscopeareauthoritative(e.gfire-clearrunrawharnessTruealthoughENDprelimFalse).

PortraitandnonalignedinputsPASS;fps15PASS,fps25PASS. SourcepreflightPROVESpositiveHEVCtransportcoveragealready:rtsptraffic2HEVC1080,ch9HEVC1440;rtspsch9/ch10/ch16HEVC2560x1440@25,traffic2HEVC1080(unknownheaderfps0/0butactualframes).rtsp/rtsps traffic1H2641080@18,camera01H2641080@25,TCP9003/4/6/8/10H2641080@25. Livepositivecasescompletedearlier. TCPHEVCnegativefailscurrentH264-onlyTCPbranch;don'tmisstateallHEVCunsupported. 8cam10FPSloadrampcompletedharnessFalse/0fatal;needphase-specificcorrectedrates/recoveryanalysis,notcamera-limittakenfrommean.

KubehelperNOTRUNyet. FINALconfigurationkeepsALLbindingsincludingfire, usesprovenmotion-transition.mp4(720p)fornativequiet→wake→quietandprojectedConfigMaprev2. EarlierIDEAtoDISABLEfireonblackrampwasREPLACEDBEFOREexecutionafterrealall-bindingmotioncasespassed;noimage/gate/bindingchangedinproductioncampaign. Kubehelperfirstdoespostperformanceempty_native_modelprobeONtarget(frozenDocker,3frames),thenlaternativecluster;scopewarmdrivernotcoldstartbenchmark. Motionverdicttime-bounded>=2inferenceFPSover<=12safterquiet,then>=10newquietframes;cannotbeconfusedwith.2FPSsweeps. Keeporiginalglobalnodepasswordprivatebackupsecret/excluded,finalhelperverifies/restores/removes.

Fourvehicle-rolecamerascorrectedclockanalysisGENUINELYfails:cameraFPS7.377/7.461/7.574/7.538,windowmin6.159–6.444,allcumulative<95%target8. ThereforetwoLOWERBOUNDARYcases(2and3cams,profilevehicle,markedANPR720p10-420,60ssteady)queuedatpost-extendedidleINSIDEkubernetes_check.pyBEFOREclusterstartup,underunchangedNoTurbo1/PL265/PL145and1Hzobserver. Masterextendedalreadyexitedthen,noledgerwriterconflict;helperimportscampaignfreshandappends2rows;rootpostreadsledgerfreshafterKube. Rootposttimeout1000includes~200secsadded+normallyshortclustercheck. BothlowercaseskeepSCENE2s/evidence+vector,allgallerykindslimit200forsufficientfullframeANPRlatencysamples. Noextraescalationbeyondthese2points. Existing4casewasunmarkedoriginalvideo;newmarkersaredeclaredinputannotation,sodon'tclaimbit-identicalimageaccuracycomparison.

analyze_markers.pynowretainsPRIMARYsceneanalysisunchangedandadds`other_fullframe_events`forplate_read/plate_outcome/line_cross/zoneentry/exitJPEGswithno sourcebboxcropandnumericframeordinal. Sceneanchorshelpunwraploops;secondaryalignmentrunSEPARATELYsosecondarycandidate-deliverydelayscannotcontaminatescenevalidity/distributions. Crops/unparseablebranchIDsareexcluded/notfabricated. First-per-kindgalleriesaresmallsubsets;onlylowercasesenlarge200togetfullframeeventstatistics. Ageincludesbest-candidate/temporalconfirmation,notonlymodeltime. CPU-onlyanalyzerselfcheck(realencodedmarkerJPEG+SYNTHETICknownPES/accepttimes)PASSscene500msvsplate2500ms,distributionsseparate;artifactmarker-analyzer-selfcheck.jsonISNOTrealpipelinebenchmark. Plotprimaryrecordsremainonlyscenes. Rootpostfuturecallloadsnewhelper;noheavyanalysisduringperformance.

Current00:33:33caseedge-source-fps15-request30,completed81;checkSTATUSforlatestprogress. Rootpost817050/finalaudits872847waiting;extended894899running;localreferencewatch3690460waitingimportrequestwithvalid3.3GiBRAMarchive. No runtime/model/contractchange,pushornewimage. Continueallremainingfinitecasesandfullreport/plots/cleanupafteraudits. ActualhourDONE;doNOTrepeatit.


### Continuation through00:56UTC: fractional FPS and realistic reconnect sources

EmbeddedsoftwareprovenactualfrozenimageOpenVINO2026.2.0-21903-52ddc073857-releases/2026/2,DLStreamer2026.2.0,Pythonversioninembedded-software-policy-schemas.json. All11canonicalpolicy/schemacurrenthashesmatchBAKEDexactly;policy95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03. Artifacts localrootoptimization_evidence/2026-11-05-isolated-host/{embedded-software-policy-schemas,canonical-embedded-hash-comparison}.json,mirroredremoteinventory/. Nochangedcontract,gates,models,image.

Source15FPS/request30caseactualrawcounter~15.152FPS(no fatal),source-limitednot30duplicated. OmittedFPSdefaultcase~8.136/healthtarget8PASS. MIXEDFRACTIONALcaseCONFIRMEDBUG:desired[4,8.5,10],topconfigured_loadaggregate22.5/nohardlimit,butcamerahealthtarget[4,8,10]andsustainedprocessing~4/8/10. Filefractional-target-checkpoint.jsonsavedincase. Nativefps=int(proc.get(...))sourceconfirmsfloor;notjustmeasurementnoise. Uniformharness24FPSassumptionNOTactualinputtarget22.5;separatelyflagaccepted8.5→resolved8semanticmismatchregardlessthroughputtolerance. FinalauditratecameraTargetsreadactualconfigJSON.

Workerpause10secFIRSTCASEfinished1unavailable/1degraded,2fatallines. InvestigationCONFIRMSrecoveryconfoundedbyone-clientfixtureexit:source01logConnectionreset/source02Brokenpipe,thenTCPfeederrepeatedConnectionRefusedError. PreserveNATIVEerrorsasrealtriggerobservations;doNOTclassifyeventualreconnectfailurepurelyimageuntilpersistent-source retake. kill_workercasefinishedbothhealthybutfatalerrorsduringdeliberatekill;classifyexpectedfaultcontextnotnormal-runtimecrash.

TESTONLYfixprepare_persistent_replays.pyappliedtoremoteACTUALharnesslaunch_source:whenSENTRY_TEST_REUSABLE_REPLAY,spawnreusable_sender.pyindependentpercamera/connection,notone-shotffmpeg-listen1. probe_case.pyfutureprocessesautomaticallyenableforSENTRY_TEST_ACTION+recording(notmarkers);writes persistent-replay.json. Actions.sources skipsmanualone-shotrelaunchwhenpersistentproduceractive. Existingrunningchildcaseskeepoldloadedcode;subsequentcasesloadnew. ExplicitTCPoutage/capacity/hour/markerpathsUNCHANGED. Reusableproducersarenotimage/decodechanges. Sourcecodewithactualpatchmustbeincludedreproduction-tools.zip(asfinal_auditsalreadydoes).

QueueONEboundedretakeedge-action-pause_worker-persistent-replay(2cams,90ssteady,pause10sat20s)INSIDEkubernetes_check.pyatpost-extendedidleBEFORE2/3markedvehiclelowerboundarycasesandbeforecluster. Rootpost1000sectimeoutcovers~330sadded+typicalcluster~300s;watchtimeoutifclusterstartupslow. Alltheseperformance/faultfollowupsstillNoTurbo1/PL265/PL145and1Hzobserveractive,beforeoriginalpowerrestore. RootpostreadsledgerfreshAFTERhelper,so3newrowsincluded;mainextendedalreadyexitedthen,noledgerwriterconflict. KubeCURRENTresettoidkubernetes:18081afterfollowups. DoNOTintroduceconcurrenttestwriters.

SocketABSENTfault10s:1530admitted/1524durable/149backpressure/1coalesced/4exhausted/0protocol;bothhealthybefore/afterrestore. SocketPERMISSIONfault10s:1561/1557/113backpressure/4exhausted/0protocol. AgentPAUSE10s:1564/1564/115backpressure/0exhausted/protocol,harnessPASS. AgentRESTART10s:1547/1541/130backpressure/1coalesced/4exhausted/0protocol;needsIDsequenceauditafterallcases. Thesehealthcutoffsarenotfinalcapturecount;pending/coalesced/exhaustedaccountingandcommonendpointreconciliationrequired. FinitepreACKreleaseallowedcontract;separateproducerquality-statebugafteradmissionFalseproven3modalities.

At00:55:29currentCPUquota2camcase;likelysubsequentmemory/readonlycasesusenewpersistentproducer(checkpersistent-replay.json),CPUquotaalreadyrunningkeepsoldone-client. Restart_containerresultmustbereadforrecovery/sourceeffects. DoNOTtreatcachedENDprelimFalseasfinalharnessfail. Currentextended894899;rootpost817050/finalaudits872847waiting;localreferencewatch3690460stillwaitingforimportphase. RootPTY94747alive. ActualhourCOMPLETEDalready,no repeat. Allremainingcases/audits/report/plots/cleanupstillunfinished;continueautonomously.


### Confirmed operational findings through01:14UTC

APIcommon-endpointDRAINreconciliationPASS:201admitted=201durable=201uniqueacceptedpackages,421uniqueobjects,contiguoussequences1..421(next_sequence422),0retained/coalesced/exhausted/protocolerrorsatthatSAMEcutoff. APIevidencebytes8,813,886,77embeddings,143evidenceobjects(by421-201-77). Capturecontains100packagescam1/101cam2. ThisdirectlyanswersuserAPIboundary;notproductionagentDB/Managementdurability. Otherlong-runhealthvsfinalcapturecutoffsdifferandmustNOTbesubtractedasloss.

Containerrestartcasefinal2healthy,0fatal,commandreturn0. CPUquota1core10sec→restore0commandsreturn0,final2healthy/0fatal;rawharnessFalsebecauseintentionalratefaultnotunrecoveredcamera. Memorypressure6GiBcap+1.5GiBallocationbothset/restorecommands0,harnessPASS/0fatal. Memorycasestarted00:56:38.922,sourceproducerpatchcopied00:56:40.670(~1.7sLATER),sothisrunningcasestillone-shotproducer(no persistent-replay.json);DO NOTclaimitusedpersistent. Followingstate-readonly/drain/native45/group21DOhavepersistentflag.

READONLYMETRICSCASECONFIRMEDDATA-PATHCOUPLINGBUG:6camera_thread_unhandled_exceptioneventsfromPermissionError inruntime_telemetry.py::_atomic_json(line37 .runtime-camera-...tmp)andface_metrics.py::_save_locked(line82 face-...json.tmp). Bothcamerathreadsinterrupted/retriedwhilemetricsdirectoryunwritable;final2healthyafterchmodrestore755. ThisisnotjustbenignstaleJSONandnotanativeGPUfailure. Preserveastestedoperationalresiliencegap:noncriticalmetricfilewriteerrorsshouldnotkillCVprocessing. Noimagefixapplied.

Persistent-source45secondworkerpauseCOMPLETEDrawharnessPASS/0fatal,final2healthy,oneprocess. ActionVERIFIEDexecutedSIGSTOPpid52at1793840628.536thenSIGCONT1793840673.626(~45.09s). Bothhealthybeforeduringafter-resumedegradedbriefly. Rawpollmeanhigh8.477/8.494FPSover120sdespite45spause:clearlyDONOTusewholecaseFPSasproofuninterruptedprocessing;therewaspause/catchupandpotentialmedia-backedreplayclockbehavior. Needphase-specific1Hzstaleness/progress/estimatedsourceAgeanalysis. Thisprovesmanualresume/recoverywithpersistentstreams,NOTautomaticnaturalNPU/kernelhangrecoveryorlatencySLAthroughpause.

21scene-cameras1FPSgroupboundarytest:ALL21healthyBEFOREkill,cam1–20PID52,cam21PID53. Immediatelyafterkill20unavailableandcam21stillhealthy;FINALall21healthy/twoPIDs/RAM3637.6MiB.20fatal-logentriesoccurunderintentionalgroupkill,not20unexpectednormalcrashes. Demonstrates20+1groupfaultboundaryandpeerisolation/recovery,NOT21cameras8/10FPSfull-usecasecapacity.

Grouping4/5at8camsfull8FPS:bothfinal8healthy/twoPIDs/0fatalbutrawrateharnessFalse. Memory7852.25MiB(group4)and8022.52MiB(group5). Needcorrected-clockratesbeforecomparativeverdict;don'tsaymodelsharingremovedorRAMOOM. Differentgroupingduplicatescompiledinstances;no automaticperformancewinclaimed.

At01:13:46currentedge-source-bandwidth-cap,completed99. Remainingcorruptpeer/Secretrotation/membership/one-all-unavailable/fanout/HTTPnegatives/markedlatency/rollback/frozenreference/gaitgallery/queuedpostextendedretake+2vehiclepoints/Kube/audits/reportstillrequired. Rootpost817050,finalaudit872847standby,extended894899;localreferencewatch3690460waitingforimport. No core/runtime/model/schema/gatechanges,pushornewimage. Originalnodeprivatebackupmustremainprivateandbeverified/removedlate. Continueallwork,donotstopwithrunningstatus.

Effectivehourpolicy(snapshotJSON)cam1revision5changed23:05:55,modesmostlytolerant_collection,firebalanced. Presence4hits/5s unobserved/20sunknown;zone.018margin/4votes/.8s;count4obs/.8s/20sheartbeat;ANPR3.5s/.35final/2reads/min16x5sharp5/single.82;identityfacequality.3,min28px,window2.5s,bodyquality.2/min28x56,gait24silhouettes,cooldown5s;fire.65/5confirm3s/clear10s5negmaxgap2.5. ResolvedTRACKINGdisplaystill320px/9slegacyparams;nativegvatrackdoesNOTconsumeoldcentroidassociationparams(asdocumentedpreviously). Don'tclaimnativeGVAassociationadaptivebecausehealthresolvermodechanges. Otherpresence/zone/qualitygatesdoresolvepercamera/object. Face_success_ratio0despitesuccesshistoryhasambiguousmeasurementwindowsemantics;NOTaconfirmedbugwithoutdefinition/test,don'tinventit.


### 01:28 UTC continuation: API boundary, exact hardware and Secret reconciliation

User clarification preserved: qualification ends at the compatible agent API; downstream Management credentials/uploads/retrieval are outside scope. Same-cutoff reconciliation already passed 201 admitted/201 acknowledged/201 unique accepted packages, 421 linked objects, sequences 1–421. Do not extrapolate this to a production agent database.

Secret URL rotation is a confirmed semantic failure: the supervisor reports revision 2, while all 937 captured packages retain revision 1 and all 64 scene JPEGs retain the old 1920×1080 source rather than the replacement 1280×720 source. The raw rate-only harness passed; that does not qualify source reconciliation. Frozen image remains unchanged.

Exact target inventory: Intel Ultra 9 285H, 16 logical / 16 physical cores reported by psutil, RAM 33,109,463,040 bytes = 30.83559 GiB (not the earlier approximate 31.6 GiB), no swap, Ubuntu 24.04.4 LTS, kernel 7.0.0-30-generic. Grouping memory 7,852/8,023 MiB is about 7.67/7.83 GiB; retain MiB in final tables to prevent unit confusion.

Camera membership removal/addition reached revisions 2/3 and ended with both decoders healthy, but emitted native longjmp termination messages around worker replacement. Inspect action/capture revisions and timing before concluding normal reconfiguration or harmless expected shutdown. One unavailable/corrupt source did not stop the healthy peer; rate harness fails these deliberately incomplete fleets by design. HTTP negative case is running; extended completed 106 rows. Original one-hour test is complete and must not be repeated. Remaining comparisons, followups, disposable Kubernetes, audits, plots, final report and cleanup remain authorized and unfinished.


### 01:42 UTC: input/revision outcomes and reporting helpers

HTTP and HTTPS actual runs both fail in native `_probe_source()` with UnsupportedSurfaceSource before network decoding; HTTPS server was plain loopback HTTP, but the observed failure precedes TLS so establishes native scheme rejection only, not TLS negotiation behavior. Local-file Secret test also completed; inspect validation diagnostic rather than claiming local-file desired-state support from offline replay helpers.

Five logical cameras reading the same reachable live TCP URL passed the harness: selected rates about 7.94–8.03 FPS each, inferred about 5.75–5.78 with 27–28% quiet frames; 219 scene embeddings, 224 images, 301 packages, no fatal logs. This qualifies transport fanout under this mixed quiet/active scene, NOT five fully active 8-FPS camera capacity. Raw five-reader test is separately saved.

Camera membership revision2(remove) and3(add) are visible in actual API packages (150 rev2 cam1; 1193 rev3 cam1;1272 rev3 cam2), and final cameras healthy. Native longjmp termination messages occurred around worker replacement; retain as a reconfiguration crash/error finding until classified, not silent-success. Unavailable/corrupt-source startup cases preserve healthy peer with real packages; all-camera raw harness false is expected for deliberately incomplete fleet. All-unavailable case has no input/frame outputs; /healthz remains responsive and shared ready true (availability is per camera, not image acceptance of fake healthy video).

Marked720/1080/1440 runs completed. Raw1080 four-camera harness passed,1440 rawharness false despite whole-run means near8 and nofatal; await timestamp-window and marker audit rather than blanket rate pass. Shared-background1080 rollbackoff versuson recorded gait-processing lifetime means around71–73 versus28–30ms/call; these are per-stage calls with different cadence, not total-frame latency or accuracy equivalence. 1440 rollback pairing running. Next is verified frozenopt76import atidle, four paired runs, offlinegaitgallery, then postextendedfollowups/Kubernetes/audits.

New test-only `report_supplement.py` computes offline hour checkpoints, steady resource distributions, late memory slopes, stage-window-mean distributions, actual API reconfiguration revisions and comparison stage calls/work. It must be invoked manually after automatic analysis; no runtime code/image changes. `receiver_contract_checks.py` now saves exact representative201durable/200duplicate ACK bodies; local rerun8/8PASS, results mirroredremote artifacts/receiver-contract-checks.json. Uses a real accepted scene package but reference receiver, not production database durability. Addedhour effectivepolicyJSON inventory/hour-effective-policy.json and exacthardware correction. Draft report includes recorded policy/devices, remains DRAFT until final audit/report/cleanup.

Hour receiver final evidence_bytes=2,494,801,191 bytes across20,761JPEG objects;70,913packages/6,410vectors unchanged. These are APIcapture totals, not allgallerysavedJPEGs (allscenes plus first-per-kindother evidence). Signed runtime observed_at may lead API acceptance because source PTS-to-wall estimates can be ahead; retain negative ages, independent PES markers are the freshness reference. Do not claim exact physical camera UTC validation.


### 02:00 UTC: main queue complete, Kubernetes fixture correction in progress

Main extended controller completed120ledgerrows at01:52:17UTC. Frozenopt76transportverifiedalllayer/configidentity; local3.5GBRAMarchive and emptyRAMstagingdir removed after successful import. Referencecompare1080two/1440four completed. Gaitofflinehelperreturn0:163maskarrays,163nonempty. Visually inspected gait00/20/29: firstforegroundblock and later fragmented/complementary-looking regions, so array-valid DOESNOTprove clean anatomicalsilhouette or native selectedsequencequality. This is offline sparseCPUdecoded diagnostic, not fullnativeaccuracy. Sixrepresentativepanels/JSONcopiedlocalgait-input-gallery/.

Rootpost817050 launchedfollowups: persistent10secworkerpause retake rawharnessPASS/no fatal; vehicle-rolemarked2cameraPASS;3camera rawharnessFalse. These are newlowerboundaries withsceneeverycam andmarkedfullframeJPEGs; awaitcorrectedrates/independentlatency. Ledger123now. Targetempty3frame exactnativeprobe confirmedPRIMARY[] and smoke.37158203125identical tolocal. ProbeJSONscope textstill saysoriginal/localhostgeneric; location and Dockercommand identify targetrun.

FIRSTdisposableKubernetesattemptFAILEDTESTSETUP: explicitkubelethealthport10258 collided withown cloud-controller default10258. API brieflyready thenserver shut down, containerdimportEOF. Runtimeimage neverexecuted in that attempt. Allfailedlogs preserved artifacts/kubernetes-attempt1-port-conflict/; privateclusterdata renamedk3s-disposable-attempt1(containscredentials; DO NOTarchive/copy). Originalnodecredentialverifiedpreserved.

Testhelpercorrectedkubelethealthport10262. Added SENTRY_TEST_KUBE_ONLY skipoffollowups/emptydiagnostic so retake DOESNOTrepeat completedperformancecases. RootpostPID817050 SIGSTOPPED at safeofflineboundary aftergallerychildfinished, beforemarkers/monitorstop/powerrestore. ItsstateT and defunctcompletedgalleryDockerchildareexpected. Need **SIGCONT817050 after Kubernetes retake1023462 finishes**, or finalaudits872847 willwaitforever. DoNOTleavepaused. Originalmainextended894899exitednormally. Originalmonitors/powerNoTurbo1PL265 remainactiveandunchanged.

RootKubernetes-onlyretakePID1023462(pidfilekubernetes-retake.pid), loglogs/kubernetes-retake.log, freshownclusterdata-dir k3s-disposable andnewartifacts/kubernetes. CURRENT manually setkubernetes:18081 so1Hzmonitorworks; rootCURRENTfilecannotbeoverwrittenbyplainadmin1 in-place(use root or atomicreplace). Statuskubernetes-only-retake untilrootpostresumes. RootPTY94747available. Firstfailedclusteralreadyfinallycleaned. CheckactualverdictJSON, notexitstatus. Retake roothelperfresh startup at~01:59; no image/gate/schema edits.

Galleryaudit ALREADYCOMPLETE PASS:16,925sampledpackages,10,108JPEGs(6679frame/2910subject/519face),6765vectors,dimensionsface512/body384/gait4096/scene768, allnorms~1,zeroerrors. ExactJPEGsize/hash/dims/parent/camera/sourcebounds andvectorparent/frame/presencelinkschecked. Receiverchecksallacceptedpackages; galleryis subset except enlarged/allscenes, notall20761hourJPEGs. Rootpostwillcontinueindependentmarkers,analysis,monitorstop/powerrestore/ACLcleanup aftermanualSIGCONT; finalaudits thenmatchedrate/GPU/trace/toolsarchive/restoration. report_supplement.py muststillinvokemanuallyafterchain. Finalplots/manualcapacityfreshness/coverage/report/cleanupstillunfinished.


### FINAL CHECKPOINT — isolated campaign complete (2026-11-05)

Goal: measure the frozen Opt79 shared-gait-background V8 image on isolated192.168.1.64 without changing production gates, models, tracker settings, schema or the image. User boundary is the compatible delivery-agent API, including evidence/vectors/links/ACKs; downstream Management explicitly excluded. Recognition accuracy excluded. The finite campaign and all executable queued followups are complete. This does not claim every possible environment or a clean production release.

Final ledger123runtimecases includes deliberate fault/negative cases and invalid/calibration attempts. Additional75bakedruntime/30injectedpolicy/26configuration/8receiverchecks passed.9correctedprofilesvalidated;11policy/schemahashesmatchbaked. Mainimage DockerID626ef9f4... remains unchanged, originalPodmanf82a0769... and45rootfsdiffIDs/config-equivalence retained. No rebuild, commit or push by this validation work.

Actualhoura4-turbo-off-stress-60min-all-2x8-yuv420:3600secconfiguredhold,360steadysamples3590.232secfirst-last. Matchedinference7.980/8.000FPS, everyqualified10secwindow>=7.654. IndependentscenePES→API p95crowd404ms/vehicle583ms,p99504/644ms;3628markersdecoded,0missing/ambiguous. APIcapture70913packages/20761JPEGs/6410vectors(face547/body2024/gait211/scene3628),JPEGbytes2,494,801,191. Finalhealthcutoff70885admitted/70883durable/2coalescedDIFFERSfromlatercapture; don'tsubtractasloss. Separatecommonendpoint201admitted=201ack=201uniqueaccepts,421objects/sequences1..421,0pending/exhausted/protocolerrors,8,813,886JPEGbytes. APIboundaryworks in these tests. Reference receiver is not productionSQL durability.

HoursteadyCPU p50=1.678cores,p95=2.037cores; RAM3161.88→3422.27MiB(+260.39),late10minslope2.082MiB/min. CPUmean60.07C/p9568C/max77C; packagepowermean16.82W. GPU nonRC6mean70.35% is NOT compute. Correctedindependentintel_gpu_top20836samples acrosscampaign:hourComputep5024.34/p9530.02%,Videop951.59%,VideoEnhancep953.72%;NPUbusymean34.83/p9542.24%node-scoped. Maina4testsNoTurbo1,PL145W/PL265W; stockcoldstartcriticalguardaborts preserved, no stockcapacityclaim.

CRITICALtimestampdefectidentifiedduringfinalaudit:3626/3628hourscenes have observed_at AFTERAPIacceptance,mergedmedianreceipt-minusobserved=-7.868sec. Independentsourceframesarefresh. BakedSourcePtsClocktextsavedinventory/baked-source-pts-clock.txt; anchorsmediaPTSonce/reanchorsbackwards. Defectconfirmed, exactupstreamcauseNOTfullydiagnosed; do notdescribezero-clampedruntimeageascorrectlatency orclaimphysicalUTCtimestampsvalidated. Fixmappingandprovenance withoutretimingeventstoprocess/sendtime.

Correctedcapacity:all1080 4x8shortmatchedrates7.985–8.060PASSandseparate4cammarked1080freshnessp95<=797ms;2x10shortPASS;4x10miss. All1440 2x8ratePASS;4x8/10originalmiss. Marked4x1440doesmeetaverage/windowsbuttwopeerssourcep955.407/6.225SEC,p995.513/6.496s; FAILfreshnessdespite~8FPS! All1080six8fail~6.34–6.65;720eight8fail~5.76–6.10withnativeOpenCLerror. Scene8x8shortPASS,12fail;identity4PASS/8fail;vehiclemarked2and3PASS(rate&freshness),originalunmarked4fail. Markerschangeinputpixels,notbit-identicalcomparison. Mixed25/75fourcamsPASS. Not16busyfullcams; schema50distinctfromcapacity. Sustaineddemonstratedplanningpoint2x8afterfixes,4x1080candidateonlyshort.

Optimizationproof:4x1080gaitbackgroundOFF7.47–7.73FPS/CPUp954.890cores vsON7.96–8.00/3.903. 1440OFF4.73–5.28vsON7.06–7.35(stillfails). Frozenopt76→79two1080CPU2.473→1.836coresp95;four1440FPS5.40–5.47→7.13–7.85(stillfails). Noaccuracy/maskparityclaim; rollbackchangeslearningcadence. Directgaitretentionbug1205presences/13682silhouettes/4710centres,36.74MiBsilhouettepayload onlypartofmemorygrowth.

Otherconfirmedimageissues:face/body/gaitqualitystatecommittedafterexplicitadmissionFalse;samequalitylaterlost. SecretURLrotationhealthrev2but937outputsrev1and64old1920x1080scenesratherthannew720source. Fractional8.5acceptedbutnativeint8. Metricsreadonlycaused6camera-threadexceptions. Workerstructurednestedextrasredactionleaksdummysecret;plainstrings/supervisorredact;noactualcredentialleakshown. HTTP/HTTPSacceptedconfigrejectnative. Nativelegacytracking320px/9sresolvervaluesnotappliedtogvatrackassociation. Cameraadd/removeeventuallyrev2/3outputshealthybutlongjmpterminationerrors, overloadnativeCL_OUT_OF_RESOURCESpreserved. Naturalmodel-deadlockrecoveryunproven; manualpausepersistent-source10/45recovers. These are future fixes, not silentlychangedgates.

KubernetesretakePASS: originalattemptkubelethealth10258collidedwithcloudcontroller,failedbeforeCVexecution;preservedinvalidsetuplogs. Correcttestport10262andSENTRY_TEST_KUBE_ONLY skipscompletedfollowups. RetakeactualAPIrevisions1/2,projectedConfigMapupdate51.06s;quiet→substantialinference→quietallbindings. UID10001privilegeddeviceaccess,notproductiondevice-plugin/nonprivqualified. Directcgroupmemory2,677,374,976bytes~2553.34MiB vsnearbyAPI2553.84;CPUlimit4cores,memorylimit8192MiB,tasks196match. Rootpost817050waspausedfortestrerunthenRESUMEDandFINISHED;finalaudits872847FINISHED. No standbywaiting.

GalleryauditPASS16925sampledpackages/10108JPEGs/6765vectors,zeroerrors,face512/body384/gait4096/scene768normalized. Allsavedscenes/enlargedmarkersplusfirst-per-kindotherJPEGs;notallacceptedimagesretained. Reference receiver validatesacceptedbytes/schemas;laterexactpromised-IDchecksstricterthaninitialhourreceiver. Samplefacecontext182x182visuallyinspected. Offlinegait163nonemptyarrays;images00/20/29blocky/fragmented,notclean-silhouetteorselectednativegaitqualityclaim.

Test-onlylatecorrections: GPUJSONparserinitiallystoppedatcommaafter5records; acceptsconcatenated/comma-separatedobjectsnow,20836realrecordsandfiguresregenerated. Source-onlyFFmpeghelperfirstunsupported-rw_timeout;targethelpconfirms-timeout,retake21progresssamplesbutstreamcopy/nullhasNOframecounter,so numericFPSUNAVAILABLE;no thirdrerunorblamingearlierlive5FPSonsource. Rawattemptspreserved. No CVruntimechange. ActualreproductionZIP69files+SHAverifiedagainaftercorrections, includingkubeportfix/GPUparser/plot/report_supplement/sourcehelper.

CLEANUPverified02:16:49UTC andlater: noownprocesses/containers/acceleratorclients, hostCPU0.1%,available29.54GiB,CPUpackage50C. no_turboRESTORED0,PL2RESTORED115W,PL145unchanged;temporaryDockerACLremoved;originalnodecredentialfingerprintpreserved/privatebackupremoved. Bothprivatek3s-disposable directoriesremovedafterprocess/mountchecks. Local3.5GBreferenceRAMarchiveandemptystagingremoved. ApplicationservicesremainSTOPPEDandfiveown/run/systemd99-sentry-validation-isolation.confguardsretainedtoleaveboxisolated; seefinalreportsection19/inventoryservices-afterforrestoration. Original/etcunits andK3sdataunchanged. Rootcredentialvalues/kubeconfig/tokensNEVERexported.

DELIVERABLES: ISOLATED_VALIDATION_REPORT.md(~6kwords), ISOLATED_VALIDATION_PLAN.md, thisdetailedchronologicalhandoff, localoptimization_evidence/2026-11-05-isolated-host/final/(analysis,rates,faults,resource supplement,traceCSV,69-toolreproductionZIP+hashes,examples,schemas,10PNG/SVGfigures). Fullrawhomeonremote /home/admin1/Documents/sentry-validation. Finalportablebundleandcompletionstatuswrittenafterreportlink/hashchecks. No campaign work remains running. Remainingworkisfuturefixeslistedreportsection8/18,not another blindoptimization/retestloop.


### Controlled optimization resumed after128-case capacity work

Userauthorizedproceedwithspecificcost/blockingcomparisonsandoptimization, preservingV8/allproductiongates/evidence. Localdisknow43GiBfree; no storageblock. FrozenOpt79remainsreferenceimage. DO NOTrepeatoldopt78asyncgaitworker,bufferreuse,orROI-morphologyastrivialnewfixes: historyprovedmostgaitwaitremainedcriticalandallocationreusewasno meaningfulgain. NewcandidateparallelizesMOG2independentROWmodelupdates; sameper-pixelalgorithm,history300,var25,shadowsFalse,exactoneupdatepereligibleframe,unchangedfull-resolutionpixels. NeedbitexactforegroundANDbackgroundstate, normalizedsilhouettes/sequence/gates/eventassociationparitybeforeintegration. SharedboundedCPUworkersconsideredonlyafterprototypeproof. No learnedmask/sampling/thresholdchangesauthorized.

Firsttest-onlyscript scripts/isolated_validation/mog_partition_probe.py usesexactfrozenimageOpenCV4.14 CPUonly(nodetector/embeddinginference),oneOpenCVthreadand4Pythonworkers,rowpartitionedMOGmodels,alternatingbaseline/candidateorder,720/1080/1440/oddgeometry andvariedlearningrates. Initialruncomputedbutcouldnotwrite/ results becauseUID10001mountpermissions; preservedprobe-attempt1-output-permission.log, chmodtestoutputdirthenboundedrerun. No productionfileseditedyet. Actualresults pending.

192.168.1.64isNOTidleanymore:pythonPID1053773cwd/home/admin1/chandana_optimization/group_ingest/app/ingest.py~6.5CPUcores/RSS2.3GiB plusmediamtx1126175. SourceASTconfirmsnativegvastreammux/gvadetect/GVAtrackergraph withfreeze_shared. This is a newseparate GPUbenchmark afterourlastcleanup; preservedit. DoNOTrununqualifiedcapacitytestsconcurrentlyorblindlykillnewbenchmark. Needavailableisolatedslot/explicitdecisionbeforepairedGPUperformance; CPUparity/codeworkcancontinuelocally. Originalk3s/apexcontrol/GDMinactive. Sourcecomparisonagainstthenestedoldnativecascade20camera reportshouldnotbeconflatedwithnewgroup_ingestactiveimplementationorrootfolderdirectOpenVINOengine.


### Opt80 candidate — row-parallel gait background update (2026-11-05)

Goal remains the integrated DL Streamer V8 pipeline with unchanged production gates, models, event/evidence associations and source-frame timing. This is a new implementation after the completed Opt79 validation, not a claim that the earlier capacity limit has improved.

Measured feasibility: in the frozen image's OpenCV 4.14 build, synthetic and recorded-footage probes produced exactly identical foreground AND learned background images for all tested frames, including odd dimensions and explicit learning rates. Recorded crowd input, 120 frames/resolution: 1080p median native 38.881ms versus partitioned 14.476ms; 1440p 66.590ms versus 24.048ms. These are CPU-only stage measurements, not end-to-end FPS or capacity. Synthetic 720p regressed, so the runtime retains single-model processing below 1,500,000 pixels.

Implementation: new pipeline/parallel_background.py partitions independent MOG2 per-pixel state into row bands, applies the identical history=300/varThreshold=25/shadows=False update, then reconstructs the full mask before unchanged morphology/normalization/sequence gates. One bounded shared executor per process/worker count, maximum 8 threads limited by CPU affinity/cgroup quota, maximum 2xworkers submitted jobs. Camera/frame ordering is synchronous as before. Errors drain all writers and reject partially advanced background state; last-owner cleanup closes workers. person_reid.py uses the factory and closes backgrounds; DlsGaitExtractor.close now invokes cleanup. No tracker, model, event producer, schema or policy source changed in this candidate. Factory defaults to legacy single-model; candidate image enables four partitions internally. This is not a desired-state input.

Local candidate: localhost/sentry-v8-opt80-parallel-gait-background:2026.11.05, image ID 6b91f43077ab8c5bcaae5140cab1cb7f45a9068d17452ce9a55da3694ad213f1. Overlay of frozen Opt79; only three Python files plus internal execution ENV/version/experiment label differ. No base/dependency/model rebuild. Build context, logs, hashes, probes and tests are in optimization_evidence/2026-11-05-mog-partition/.

Validation: 89 tests passed inside actual candidate (new exact masks/background parity, concurrent-camera isolation/shared-pool cleanup, failure draining, small-frame/native guard, full gait sequence and normalized model-input equality; existing gait/body/frame cache/V8 and 30 policy scenarios). Earlier test harness attempts failed for missing pytest py shim and test schema mount location; both corrected without runtime changes. The new sequence test initially generated motion below its own test threshold and did not exercise inference; fixture corrected, inference now asserted. These failed attempts are preserved in logs. Candidate inventory confirms v8_events.py and surface_stream.py match frozen baseline hashes. Full native smoke test is now starting locally; this host has other programs so it is execution validation, not isolated capacity qualification.

Remote 192.168.1.64 still runs the separate group_ingest benchmark. A specific asynchronous question was sent asking whether to pause/resume it for paired capacity tests. No answer yet; benchmark remains untouched. Candidate archive is being staged only. No production default promotion, registry push or new camera-capacity claim yet. Next: actual native execution and per-stage/end-to-end comparison; paired isolated 4/5x1080p8 and 3x1080p10 only when the new concurrent workload can be safely paused. Preserve scene bindings on every camera. If no end-to-end gain is measured, do not promote this candidate as a capacity improvement.


### Local comparison authorized; remote benchmark preserved

User explicitly answered: leave the separate benchmark on192.168.1.64 running, test here for now, and wait for the user to say when remote tests may resume. No remote benchmark signal or host-setting change was made. A3.3GB candidate archive was staged there for later use but not loaded/run. Do not initiate remote capacity tests.

Opt80 native full-use-case local smoke completed: one1080p crowd camera at8FPS, 60sec steady with10sec warmup, raw driverPASS; matched selected7.815FPS (97.69% target). Generated558packages,215JPEGs,131vectors including11face/76body/4gait/40scene. Finalhealth558admitted=558durable, noexhaustion/protocolerror/fatallog; APIcapturecontinuespastfinalhealthcutoff, so counts atdifferentendpoints maydiffer. Evidence schemas/hash/size/parent checks run at API. Source-to-API freshness was not independently marked in this smoke, so do not promote raw driver capacity_qualified=true to an isolated capacity claim. Local gait background lifetime mean at an in-run checkpoint~28.27ms; no paired native baseline yet.

Paired local native tests now use identical marked1080p30 replay, allusecases and2secscene on everycam, frozen completed-campaign receiver and harness copied into evidence/test-support, independent PES/image-marker freshness and full health snapshots. New scripts/isolated_validation/opt80_compare.py only adapts test paths and records outcomes. Fourcam8FPS Opt79 thenOpt80, nextfivecam8 reversed order if setupworks. Other host programs left running; all capacity conclusions must be labelled local/nonisolated, not compared directly as an improvement over isolated64host.


### Paired local Opt79/80 result and next bounded executor probe

Completed4x8 and5x8 comparisons with identical1080p30 marked replay,90secsteady/15secwarmup,2secsceneallcams, unchangedgates andmodels. Actual camera snapshot timestamps/cumulative inference counters are the rate denominator. See scripts/isolated_validation/analyze_opt80.py and evidence/comparison.json.

4x8: Opt79 inference7.897/7.915/7.928/7.956FPS, gaitMOGcallmean58.72–64.15ms, CPU3.269cores, RAM4063.6MiB, independent sceneP95534–632ms. Opt80 inference7.907–7.928FPS, MOG32.11–33.44ms, CPU3.637cores, RAM4254.3MiB, sceneP95654–817ms. Camera1 frame-processing mean89.442→81.139ms, but no demonstrated capacity gain. Opt79 rawharnessFalse because a finalrolling2sec aggregate sample ratio.948 was below.95, whilematched80secinference/rates/freshnesswerehealthy; preservebothfacts. Opt80 rawharnessTrue. Neither run showedfatalnativeerrors orprotocolerrors/exhaustion. Both hadsomehandoffbackpressure, so do notclaimzerocongestion.

5x8: BOTH fail freshness. Opt79 inference7.592–7.943FPS, MOG70.68–74.71ms, sceneP952.770–5.937sec, CPU4.647cores/RAM4662.9MiB. Opt80 inference7.594–7.872FPS, MOG33.25–36.25ms, sceneP953.545–6.348sec, CPU4.917cores/RAM4555.9MiB. MixedCPU/RAMdifferencesdoNOTestablishpureallocatorcost; workloads/eventcountsalsodiffer. No higher-capacity claim or production default promotion. Exact mask/sequence parity remains valid, but stage speed does not equal end-to-end scalability.

Nextmeasuredblocker: at5x8 Opt80 camera1 body modelwall24.15ms, ofwhichschedulerwait10.34ms/executor13.37ms; two-personbodycall51.05ms. Camera frameprocessingmean120.91ms beforeupstreamwait. Body isstillonesharedserialGVAexecutor; existingprefetchonlyoverlapspreprocessing. New specific experiment: allowtwoBODYrequestlanes usingthealreadytestedper-laneappsrc/appsink/PTSjobownershipmechanism (plate-lanesdefaultunchanged). Addedsecondary_lane_count() mappingSENTINEL_DLS_BODY_LANES, default1/max2; noothermodelorpolicychange. Currentlocaldls_secondary.py nowincludes this OFF-by-default experiment, so differs from the alreadybuiltOpt80. DoNOTsilentlyretagOpt80. New scripts/isolated_validation/body_lane_probe.py runsrealNPU/GVAwithsame16cropinputs,twoowners,1/2/2/1lanesand10repetitions each;requirebit-exact384Dvectorsbeforeanycandidatepromotion. Probe nowrunninglocally; nootherCVtestsimultaneously. Ifnoexecutiongain, keepdefault1 andrecordrejection. Remotebenchmarkuntouchedperuser.


### Body two-lane probe rejected; no additional production toggle retained

Native NPU/GVA probe completed with bit-exact384D normalized vectors in all40two-owner repetitions (640vector evaluations, same16inputs/repetition). OutputSHA256e0c6774f846bde0dba5b74e972b1665fbd4938b0f328b6a3ce21ad6f47640a84 in everyconfiguration. Postwarmup16vector medians, alternating1/2/2/1lanes:97.805/100.939/96.556/108.317ms. The ranges overlap; no convincing executor-throughput gain. More requests cannot be assumed to add NPU compute capacity. Avoid another speculative image/build/testloop: archived probe code+results under optimization_evidence/2026-11-05-body-lanes/; removed new body-lane helper/ENV support from runtime. Local dls_secondary.py onceagainhashmatches testedOpt80. Body prefetch remains unchanged and enabled; noOpt81imagebuilt.

Now checking the other requested rate boundary: three1080p cameras at10FPS, Opt80thenOpt79, same90secmarkedfull-usecasecase. This tests whether the lower gait spike helps the shorter100ms camera period. Remote192.168.1.64 stilluntouched. Final decision will explicitly distinguish processing latency from actual useful camera capacity; no claim of fifteen/twentybusy cameras.


### User stopped testing; new QAFT detector model integrated (2026-11-05)

User explicitly said "once stop testing", then requested the new PIPELINE/vehicle_person_qaft_int8 detector. All previous local comparisons had already completed; verified no owned CV test container or comparison/marker/inference probe remained running. Do not launch further testing locally or remotely until the user resumes it. Remote192.168.1.64 separate benchmark remains untouched.

Previous optimization checkpoint is now complete:103candidate tests passed, including exact emitted gait event/source timestamp/presence/track/evidence metadata, cropJPEGbytes and vector parity. Six90secpaired runs saved persistently in optimization_evidence/2026-11-05-mog-partition/, including rawartifacts, marked/unmarkedfixtures and hashes.17,674acceptedpackages captured acrosssixruns, no capturedfatalnativeerrors. Opt80 halvesMOGstage but did not increase useful capacity; acrossallcameras4x8processing86.36→82.28ms (~4.7%),3x10processing70.60→65.30 (~7.5%);5x8mean118.16→117.64ms (<1%) and BOTH versions stale.3x10freshnesspassedlocallyforboth; this doesnot override conditionalisolated64host results. CPUusehigherwithOpt80. Two-body-laneprobe rejected, helperremoved; noOpt81built. DetailsOPT80_LOCAL_COMPARISON.md. No optimization release promotion or push.

NEW task: replace only primary vehicle/person detector. Verifiedall17suppliedSHA256SUMS entries, copied complete sourcepackage into models/traffic-v11/vehicle_person_qaft_int8/. Checkpoint8fa716bf26f92037522256a50a38fa6fb1de830277bf6f440c02146f8beae000. OriginaldynamicXML697f1c56.../BINd70e2a9a... unchanged. Classorder matchesprior nineclasses(bus/car/minivan/motorcycle/rickshaw/truck/bicycle/person/others); existingperson→pedestrian normalization andothers exclusion retained. Sourceinputfloat32[N,3,640,640]RGB/255,pad114;output[N,300,6]end-to-end, existingmodelproc retained. Sourcepackage batchvalidation1/2/3/4/8/16 is CPU/AMD metadata, NOT an IntelGPU qualification by us.

Preparedmodel_static_b1.xml using frozenOpt79OpenVINO2026.2 read→reshape→save(compress_to_fp16=False), no devicecompile/inference/PTQ/calibration. SourceandderivedIR86FakeQuantizeeach; derivedBINhashIDENTICAL tooriginald70e2a9a5a47c3e0da5db59a3c389de636ee10dcbef1f5d6ebe721993a9ff28c. DerivedXML2c2c45652b442b51fcc7ff4255c0b7cc2f521de010961d102084eb0097b14936. integration.json records4filehashes/shapes/provenance. OriginaldynamicbatchIR retained; currentnativeGVAgraphstillbatch1/nireq8, unchanged. DoNOTclaimmulti-camera batching enabled simply because the model supports it. No new FP32device hint or CPUmodel/decodefallback added.

Repointed all activeV8 model defaults: surface_stream.py native/static and directdynamic paths; stream_fleet_openvino.py direct/sharedscheduler paths; config/worker.json. Source diff against actual frozenOpt79 contains onlythese model-path substitutions and associatedcomment. Updated Dockerfile.sentinel-v8 detectorCOPY/checks/labels and sourcebuild version default2026.11.05-v8-qaft-int8-model-candidate. Active imagepacksnewdynamic/static/provenance and removes oldFP16primaryaliases from currentfilesystem; originalhistoricalrepo models retained for older versions/rollback. Existing .gitattributesexceptions extended to two compactQAFTbins so cloning doesnot requirethe previouslyexhaustedLFSquota. No commits orpushes.

BUILT local immutable candidate localhost/sentry-v8-qaft-int8-model-candidate:2026.11.05; IDd035cb44df82a66cbcdaa29e3ca22177f7bfdbf080009d6a27bf6633e5eb2127; localmanifestsha256:3a6fce5944495bed10d870b57489dee73e7e87bde531dbba9bdc84b512fe1f64. Overlay of frozenOpt79, NOT Opt80; no experimentalparallelgait inclusion. GPUdetector/VAdecode/nativeGVAchain/modelproc/contract/policy/secondarymodels unchanged. BakedweightsnewQAFT. Buildperformedfilecopy, removalofoldprimaryaliases and Python syntaxcompilation only. No imageprocessingworker was started, no newstream/model inference/unit/capacitytest wasrunafterstop. Therefore QAFT runtime/GPU/accuracy/performance status is NOT validated. Do not callimageworking/productionreadywithoutfuturetesting.

Concretebuildcontext, baselineextractedfiles, source-diff.patch, buildlog and update.json: model_updates/2026-11-05-qaft-int8/. New source buildversion avoids overwriting frozenOpt79tag; exactoverlayreproduction in build-context/Dockerfile. No registrypublication. NextONLYwhenuserauthorizes: GPU/native startup and detections, same-frame QAFTpostprocessing/sourcegeometry, batchscheduling qualification if actuallyrequested, event/evidence/embedding delivery, then availablehardware capacity. Preserveeveryproductiongate andV8schema; do notresumeoldcapacitycampaignautomatically.


### QAFT image validation resumed locally by user

Latest user explicitly confirmed the image/model and instructed us to continue previous and additional testing on this new image. This revokes the local test stop. Remote64 benchmark exclusion remains: no remote tests until the user explicitly says when. Frozen candidate ID d035cb44df82... is the only QAFT image under qualification; no Opt80 gait optimization is being added. Preserve V8/production gates and all secondary models. Start native1cam8FPS then directGPU static/dynamic batch checks, output/API schemas and associations, scene-per-camera/freshness, measured8/10FPS capacity boundaries, supported live sources and reconnect/motion/hazard checks, short soak with resources. No automatic image promotion or push. Capacity results remain local/nonisolated; compare a fresh Opt79 control before claiming model-related gains. Model accuracy claims require labelled data and are separate.


### QAFT first native smoke passed; dynamic GPU batching failure caught

QAFT1x8native1080p markedcrowd smoke60secsteady passed rawharness. Matchedinference7.762FPS, scenesourcePES→APIp95303ms; outputs422packages/172evidence/110vectors(face11/body51/gait6/scene42). No fatalnativeerrors. Sampledhealth416admitted=416durable, no protocolerrors/exhaustion/backpressure; finalAPIcapturecontinuespasthealthcutoff, do notsubtractcountsasloss. Modelgate/motionadmissioncanreduceinferencebelowselectedFPS, so distinguishcounts.

GPUbatchprobe initialattemptfailedbeforemodelinitialization becausehardcodedframe500exceededshortfixture; correctedpositionsfromactualframecount, preservedattempt1-fixture-bounds.log. GPUprobe actualstaticbatch1has3/2/2/1/0/2/7/9detections on8sampledframes; executionGPU.0. DynamicOpenVINOcompiledmodel(batchdimensionunknown) compiles/runsbutproduces0accepted detections for EVERYtestedslotatbatch1/2/3/4/8, unlikefixed1 reference. ThusdynamicbatchGPUparityFAIL. Compilednodeprecisionsu8/f16/f32proveINT8mixedgraph, notfullFP32. No CPUfallback and noFP32hintintroduced. Candidate'sDEFAULTnativeGVApathusesstatic1andpassed; alternativeexperimentalvariablebatchGPUroute is NOTqualified. Sourcepackage'sCPUdynamicvalidation doesnotestablishGPU correctness. Nextfixedbatch1/2/3/4/8GPUprobe using sourceIRreshapedbeforecompile; onlyconsideractualGPUbatchsupportifparitypasses. No model/image changes from thisdiagnosis yet. KeepV8gatesintact.


### QAFT GPU fixed-batch support and regression tests

Native4x8marked1080p90secsteady passed: actualinference8.001/7.990/8.017/8.006FPS, per-camera sceneP95429–526ms, meancontainerCPU2.972cores/RAM4070.3MiB. No newimage/gate changes. MuststillrunfreshOpt79control becauseotherprograms/memoryavailabilityvary.

Fixed-shapeGPUprobe usingnewdynamicIRreshapedBEFOREcompilation passedallbatches1/2/3/4/8 (includespartial3): output[N,300,6], GPU.0 only, positive/emptyframes, classagreement/sourcecoordinatebounds andbox/confidencetolerances(minIoU.95,max640-inputbboxdelta2px,maxconfdelta.01). Sampledinferencemedians6.39/10.84/12.67/17.32/32.14ms, notend-to-endcapacity. FullydynamicGPUprobefailed(parityfalse/zeroaccepted); noFP32hintintroduced. ActiveGVAstillfixed1. Dynamic modelpackage supports batches whenfixedbeforeGPUcompile; currentvariable-shapeGPUexperimentalroute remainsunqualified. No CPUinference fallback.

93actualQAFTimage runtime/policy/gait/body/concurrency/morphology tests passed.1newOpt80row-paralleltest explicitlydeselected becauseQAFTimage intentionallybasedonOpt79andcontainsnoOpt80experimentalmodule. Newimage tests donotclaim103Opt80tests apply here.

Inputpreflightcurrent: RTSPSch9worksHEVC2560x1440@25;RTSPStraffic1worksH2641920x1080@25;RTSP192.168.1.95:8554/traffic2worksHEVC1920x1080@25. TCP192.168.1.92:9001timedout; independentlocalTCP fixtures areavailable. Remote64notused. Nextnative8x8boundarytest90sec. Testdriverextensionsaddunmarked/live/mixedURL/outage/agentfaultcases and1Hzhost/fullhealthobservations withcriticaltemperature103C/lowRAM2GiBfiveconsecutivesamplesabort ofONLYownexacttestcontainer. These aretest-driver changes, notCVpolicy.

Localpasswordlesssudoavailable, so independentintel_gpu_topnodeengineJSONmonitorstartedreadonly. No hostpower/turbo/servicesettingchanges. Scopealwaysnode-wide,donotattributeallGPUengineactivitytoCVorcallGTactivepercentagecompute. Monitor must be stopped at end.


### Local cold-start thermal guard and bounded startup retake

CompletedQAFT5x8 marginal: selected~7.97–8.01FPS; actualmodelinference7.775–7.880, quietframes~.11–.19FPS. IndependentimageP95.782–1.366sec, but some10secwindowsselected/completedfellto~7.16FPS, beloweven90%windowtarget. DoNOTclaimfivecamerasqualifiedfrommean alone. QAFT6x8selected~6.97–7.14FPSandimageP956.4–7.3sec FAIL. Eightcase~4.9–5.3 and9–13sec FAIL; allstayedhealthy/no nativefatal. Currentstablemarked8FPS lowerboundfour.

QAFT4x10casecompletedandfailedrate/freshnesscriterion; inspectsummary. Threecamera10FPSinitialcold-startattempt reachedcritical CPU104–105C forfiveconsecutive1Hzsamples, guardstoppedONLYowncontainerduring15secwarmup, BEFOREsteadysamples. Notanimagecrashorvalidcapacityfailure. Capturedresource-abort.json/driverreport/rawartifactpathpreserved. Controllerstoppedasdesigned; itmustnotrepeatthatattemptsilently.

Newtest-driver-only coldstartprofile:4CPUquota duringloading/warmup, thenrestoreALL16availablelogicalCPUsbeforesteadysampling. No hostpower,turbo,servicesorotherprogramchanges, noCVmodel/gatechanges. Testedmechanismwithdisposablesleepcontainersfirst: Podmanupdate--cpus0and--cpu-quota=-1returned0but DID NOT clear kernelquota (4coresremained). Thus donottrustexitcode/inspectmetadata. Positive--cpus16 DID changebothcontainer/scopeactualcpu.maxto1600000100000, whilePodmaninspectHostConfigstillreportedold4cores(stale). NewdriververifieslivekernelCPUquota>=all16coresandrecordscpu.stat/startup-quota-release.json,plus1Hzcpu.stat tochecksteadystatethrottling. Full16CPUquotaiseffectivehardwarecapacityonthis16CPUhost,notliteralunlimitedCPU.max. Bothquotaresetfailedattemptsretained. Quota-checksleepcontainersremoved; onePID1sleepignoredSIGTERM andneededstandardPodmanSIGKILLcleanup, notCVnativecrash.

Retakewillusecase local-3x10-qaft-startupcap4. Remainingfunctional/live/soakcasesandfreshOpt79controlusematchedstartupcap4/full16steadyprofile. Previousunrestrictedsteadycasesremainlabelledaccordingly; no mixingcoldstartlatencies. Temperaturecooledbelow65C; read-onlyGPUenginemonitorcontinues. No additionalimagebuildorpublication. Analyzer nowexplicitlyseparatesselected/inference/quiet/completed(inference+quiet)rates, so deliberate motion-gated skips are not false missedmodelwork; noisy5x8windowsstillfail regardless. No productionpolicyorimagechanges.


### Thermal-limited control excluded; native3x10 retake passed

QAFT3x10startupcap4/full16steadyretakecompletedrawPASS: selected10.001/10.003/10.020FPS, inference9.876/9.865/9.882 plusquietframes, independentimageP95429/471/446ms. Minimum10secselected9.93–9.97 andcompleted9.73–9.89FPS. CPUstartupquota restorationverifiedactualkernel1600000100000; recordthrottlecountsafterrelease,notPodmaninspectstalequota. This is a conditional local short pass,notisolatedmax.

FreshOpt794x8controlwasstoppedbythermalguardduringsteadyload (somecores>=103C; package101Catabort). The sameguardthereforeaffectsoriginalandnewimage; NOT a QAFT-specificmodelcrash. Preservepartialmetricsbutexcludeitfromcapacity/controlcomparisons. Do not rerun4cameraheatstressblindly. ExistingearlierOpt79recordingsremainhistorical,notmatchednewcontrol. Beforefuturetestlaunch, driverwaitsforhostmaxcoretemp<=75C, up to90sec; nohostfan/powerchanges. Additionaltestsnow1–2camfunction/API/soakplusmixedlive3subjecttoguard. ContainerCPUbudgetonly4duringstartup/warmupandfull16after; readonlyGPUmonitoractive. Casesabortedbyresourceguardmustremainfailed/unqualifiedevenifcachedlasthealthmadeoriginalharnessPASS. Newwrapperoverridescapacityresultwithresourceabortwhilepreservingrawflag; ledgerflagscorrected. Image/gatesunchanged. Continuefromcompletedcaseledger, donotrepeataborted3x10orOpt79control.


### Functional/API results and live-source thermal restriction

ANPRclipproduced76plate_read/85plate_outcome/40line_cross plusframeevidence; theseproveexecution,nottextaccuracy. RawharnessFalsefrom2secrollingFPSsample.919 despite80secmatchedselected7.921/8FPS; preserveboth. Fire→normalfixtureproduced2suspected+2cleared episodes, nofatalerrors. Motiontransitionproduced647quiet/264inference selectedframes andsceneoutputs; quietmodeactuallyexercised. EmptyfixturehadNOprimaryobjectsbutstayedactive679inferences/679selected: unchangedfirebranchrawsmokefalsepositive(~.37,belowfinalalertgate) canholdprimarymotionactive. Codeobserve_modelsusesrecentrawfirehits; doNOTclaimemptyfixtureprovesquietrelease. Motiontransitiondoesprovequiet/activeexecution. No productiongateschanged.

Reconnect12sectargetoutagePASS: targetinterrupted,recovered,peerhealthy,no nativefatal. APIHTTP50310secfault:4handoffsexhausted,99backpressure,2coalesced,no protocolerrors; contractEXPLICITLYallows6attempts(250/500/1000/2000/4000ms)thenreportexhausted/releasememory/doNOTclaimproduced. This is observedboundedfailurebehavior, not lostacceptedJPEGs or automaticcontractviolation. No guaranteeallobservedfactsarrivewhenagentunavailable. LostACKfaultPASS:88duplicateACKs,0exhausted/0protocolerrors, stablepackageidentity/bytesacceptedonce. Acceptancecountercutoffs differ; reconcileonlycommonendpoints.

Signedtimestampauditnewimageconfirmsknownclockdefect: smoke-case scenesreceived-minusobservedmedian-5769ms whileindependentPES→APIp95303ms. FrameimagesarefreshbutabsoluteobservedUTCisfuture; no fixclaimed. /healthz200,/healthy404,/metrics200 verified. DeviceplacementdecodeGPU/VAAPI-DLStreamer, primaryGPU/VA, faceGPU+NPU, pose/body/gaitNPU, plateNPU, sceneGPU, fireGPU, OCR MULTI:GPU,NPU. NoCPUmodel/decodefallback; CPUtracking/pre/postprocess remainnormal.

Three-sourcemixedliveattemptwasthermalguard-abortedbeforeANYdecodedframes: package105C/Core24 104C; allcamerasdegraded/gstreamer_started,0frames,notexternalunreachableproof. Rawharnessstartup180sectimeoutfollowedguardstop. Testpostprocessorwronglyassumedpackages.jsonlexistswith0outputs; failedafterrawreport. Fixedtesthelpertohandleemptycaptureandwriteoriginalreportfirst; recoveredcase/reportfromrawartifactwithoutrepeat. Case remainsaborted/unqualified. ThisdoesNOTestablishliveimageworks. No framesmeansliveinteropstillneedssingle-sourcechecks.

ProceedwithindividualRTSPSH264traffic1,RTSPHEVCtraffic2,RTSPSHEVC1440ch9,all8FPS/fullusecases,2CPUstartupthenfull16steady,60secmeasurement. No repeats of thermally-abortedthree-cameraattempt; allremoteservices/hostsettingsuntouched. Then600sec2x8markedsoak. Beforeeachcasecooldownbywaiting<=75C; thermalguardretained. FrozenimageIDd035...unchanged; source/gates/contractunchanged.


### Native campaign complete; export-reference limitation verified; final coverage audit

21nativecases finished. Ten-minute2x8markedsoak rawPASS, matchedselected7.99975/8.00039FPS; inference7.86780/7.85825 plusquiet.13195/.14214. Independent590sceneJPEGmarkers: P95355/353ms, P99417/415ms. KernelcgroupCPUmean1.342cores,steadythrottledperioddelta0. RAM3263.46→3362.07MiB, last5minslope+9.216MiB/min: doNOTcallmemoryflat. MaxcoretempP95104C/max105C evenwithout5consecutivecriticalguardabort: thermal warning, NOTthermallycleanproductionqualification. NodeComputeP5022.53/P9528.46%,NPUbusyP5018.04/P9532.52; GPUcounteralignmentapproximate/node-wide andenginesoverlap. No hosttuning. GPUmonitor67624stopped, no CVtestcontainerleftatthatcheckpoint.

QAFT-onlygalleryaudit(excludingoldcontrol):3731packages/3252JPEGs/3046vectors,0errors. Allcaseaudit3966/3447/3209includedoldcontrolandmustnotbecalledQAFT-only. JPEGhash/size/dimensions/sourcebounds/IDs andmodality512/384/4096/768dimensions/L2/parent/frame/presence checked. Representativeface/body/gait/scene/plate/fire/lineexamplesarchived. /healthz200,/healthy404,/metrics200.

PostperformanceCPU/F32referenceusingexportersettings(imageOV2026.2,sourceexportdocumented2026.0) vsdefaultGPUfixed1 FAIL: confidenceerrorup.199386, additionaldetectionson2/8sampledframes. Thisisconversion/numericalfidelitycheck,notlabelledrecognitionaccuracy. OfflineCPUreferenceonly,notruntimefallback. GPUF32diagnosticreducesmaxerror~.05254butstillFAILandchangesdetections. ExactfloatinputsenttobothCPUandGPUF32(rawinputbypassespreprocessing)showedsamefailure,soPPP/imagepreprocessingalonedoesnotexplainit. AdditionalCPUstatic1vsoriginalCPUdynamic1alsoFAILconfidenceparity(max~.06855); shape/backendnumericalchangesareinvolved, exactrootcauseNOTisolated. DoNOTclaimGPUhardware-onlybug, corruptweights, fixedbyFP32, orstaticserializationchangesweights. GPUfixedbatchsourceIRreshaped1matchedderivedstatic1earlier; BINbytesunchanged/86quantizers. No productionprecision/gate/model/imagechangesmade. Candidate d035... remainsUNQUALIFIEDforpromotionpendingexporter/devicefidelityvalidationandknownUTCissue.

Latercompressedsource-onlypacketdiagnostic:traffic1RTSPS~18.009packets/sec,traffic2RTSP~18.012,ch9RTSPS~25.017, metadataall25FPS. Not simultaneousCV; doesnotcausallyproveearliersourcevsCVbottleneck. OriginalFFmpegprogresscopymethodhadno framefieldandnullrates,correctedwithline-bufferedFFprobepackets. Liveindividualsourcesworked:traffic1selected~6.54/8(targetmiss),traffic2~7.98/8withwindowmiss,ch9~9/8shortwindow(catchup/clockselectionunresolved). Livefreshnessunmarkednotproved. Mixed3liveexcludedthermal0frames.

Finalcoverageauditfounddefaultnear-fullframepolygonsproducedzone_episodefieldsandcounts,butNOpositivezone_entry/zone_exit inanynativeQAFTclip (unitpolicytestscoverthem). Toverifythatnativepathratherthanclaimallpassed, onefocusedANPRrecordingcase nowusescentralbandpolygonx.35–.65/y.02–.98 throughvaliddesiredstate. Sameimage/fullusecases/gates,60secsteady/startup2/full16CPU. Case local-zone-transitions-qaft; thisisconfigscopefixturechange,notpolicychangeorcapacitybenchmark. Verifypositiveentry/exitandepisodeprogress; keepnegativefindingifnone. Thenupdatefinalreport/handoffandcleanupalltestprocesses. No furtherarchitectureexperiments, modelprecisionchanges or remote64tests.


### FINAL QAFT validation checkpoint — local tests complete (2026-11-05)

Latest taskwas "hasimagebuiltusingnewmodel,continuetestingandothernewimagechecks". Completed22nativecases,93applicabletests(1Opt80-onlydeselected),GPUbatch/provenance/cross-device/referencechecks,API/evidenceaudits,liveinputs,positivezones,motion/hazard/reconnect/faults,and600secsoak. Image stayed d035cb44df82a66cbcdaa29e3ca22177f7bfdbf080009d6a27bf6633e5eb2127, localmanifestsha256:3a6fce5944495bed10d870b57489dee73e7e87bde531dbba9bdc84b512fe1f64. No rebuild,precision/gate/model/contractchange,pushorremote64test. Opt80notincluded. FrozenQAFTbuild remainscandidate, NOTpromoted.

Authoritative report: model_updates/2026-11-05-qaft-int8/VALIDATION_REPORT.md. Complete structureddata:validation/results.json; exactcase ledger/progress/rawartifacts/health/1Hzresources/sourceepochs/GPUengineJSON included. Plotsvalidation/soak-resources.png/.pdf. update.jsonnowrecords tested-with-limitations/unqualifiedstatus. Bakedintegration.jsonremains historicalbuild-timeunvalidatedmetadata; immutableimagewasnotrewritten.

NewmodelnativeGPUpathworks; hardwaredecode/secondaryplacementsunchanged. FixedGPUbatch1/2/3/4/8internalconsistencypassed;activeGVAstillbatch1/nireq8. FullydynamicGPUzero-detectionparityFAILED;doNOTenableexperimentalvariableshapeGPUroute. Cross-referenceCPU/F32(exportersettings, runtimeOV2026.2vsdocumented2026.0)vsdefaultGPUfixed1 confidence maxdelta.199/countchanges on2/8. GPUF32diagnosticmax~.0525stillfails; exactsamefloatinput(rawGPUF32)alsostillfails. CPUstatic-vs-dynamicF32parityALSO fails(confdelta~.0685), so causeisnotisolatedtoGPU-onlyorPPP. SourceIRreshapedfixed1vsderivedstatic1GPUwasconsistentandweightsbyteidentical/86quantizers;donotclaimserializationcorruptsweights. Needexporter/shape/backendnumericalfidelityvalidation beforepromotion. No actualruntimemodelCPUfallback; CPUrunswereOFFLINEreferenceonlyafterperf,2threads. No productionFP32hintwasapplied.

Capacitylocalmarked1080pfullusecases/2secsceneeachcam:4x8shortpass;5x8marginal(meansnear8butwindowdips);6x8and8x8failrate/freshness;3x10startup4/full16shortpass;4x10marginal. Theseareconditionalshared-hostresults,notuniversalmaxor15–20cameraachievement. Datasetcontainscatalogburned-inanalyticsplusoursourceframemarkers; notdiversereal-sitegroundtruth. FreshOpt79controlthermal-aborted,excluded;nocleanfreshcausalbaselineclaim. Earlycasesstock;latercoldstartcontainerquota4(or2live)throughwarmupthenactualkernel16coresverified. Resetquota0/-1didnotclearunderPodman,positive16worked;metadatawasstale, kernelverified. Steadycgroupthrottledperioddelta0insoak. Hostpower/turbo/fans/servicesuntouched;waitedcooldowns,originalotherprogramsretained.

Soak2x8:matchedselected7.99975/8.00039FPS,actualmodelinference7.86780/7.85825 +quiet.13195/.14214; markedsceneP95355/353ms,P99417/415ms. KernelmeanCPU1.342cores, memory3263.46→3362.07MiB,last5minslope+9.216MiB/min. NOflat/leakfreeclaim. MaximumcoretemperatureP95104/max105Cintermittentdespiteno5-consecutiveguardabort;throughput/freshnesspassbutNOTthermallycleanproductionpass. NodeGPUComputeP5022.53/P9528.46%,NPUbusyP5018.04/P9532.52%;notisolatedattribution. SourceUTCFUTUREdefectpersists(newsmokescenereceipt-minusobservedmedian-5769mswhilePES→APIp95303ms). Timestampsnotvalidatedcorrectbyfreshimages.

Functional:ANPR76reads/85outcomes/40linecross ininitialclip,nottextaccuracy. Fire2suspected/2clear. Motion647quiet/264inferenceframes;emptyfixturestaysactivebecauseunchangedrecentrawsmokehitcanholdinferencebelowfinalalertgate. Reconnecttarget12secsrecovered,peerhealthy. LostACK88duplicateacks,0exhausted/protocolerrors.50310secfault4exhaustions/99backpressure/2coalesced EXPECTEDpercontract6-attemptbudget;neverclaimthosepackagesproducedorallfactsdeliveryguaranteed. No lost-accepted-evidence claim. Separateknownquality-state-admission issue remainsnotfixed. /healthz200,/metrics200,/healthy404.

FocusedcentralzoneANPRcase(oneadditionalcaseafter21-campaign)PASS:13zone_entry/39zone_exit/31linecross/28plate_read/14plate_outcome pluspresence/count. Moreexitsthanentriesisvalidforfirst-observed-insideunknown-entryepisodes. Episode/timestampfieldsprovide dwell mechanism; noseparatemilestoneeventclaimed. This changedvaliddesired-statepolygontestfixtureONLY,notgates. Rawartifactscopiedpersistent. No othernativepositivezonecoveragewasclaimedbeforethiscase.

FinalQAFT-onlysavedgalleryaudit(afterfocusedzone):3811packages/3322JPEGs/3104vectors,0errors;95facecrops,276subjectcrops,2951frames. DimensionsAdaFace512/TransReID384/Gait4096/SigLIP768,normsfinite/L2,parent/frame/presence/sourcebounds/JPEGsize/hash/dimensionschecked. Savedsubsetnotallacceptedoutputs;receiverchecksacceptancebytes. Allcaseauditprevious3966/3447/3209includesoldcontrolandexcludeslastzone,notfinalQAFTcount. Examplesbytypeinvalidation/examples/. Agentismock-compatibleAPIboundary,NOTproductiondatabaseorManagementretrieval;userexplicitlylimitedscopehere.

Liveindividualsactuallyprocessedframes/events/evidence:RTSPStraffic1H264~6.54selectedFPS(target8miss);RTSPtraffic2HEVC~7.98(targetwindowmiss);RTSPSch9HEVC1440~9/8shortwindow,selectionclock/catchupunresolved. Unmarkedliveabsoluteimagesfreshnessnotproved. Mixed3sourceattemptthermalstoppedbeforeframes,excluded. Latercompressedpacket-only18.009/18.012/25.017FPS vsadvertised25;notconcurrentandnotcausalCVvsfeedproof. TCP192.168.1.92:9001preflighttimeout;independentlocalTCPnativeworked. SourcecopyfirstmethodlackedframecounterandgaveN/Arates;correctedFFprobe-linebufferedpacketmethodarchived. No fabricatedsourceFPS.

Threeexcludedthermalattempts(original3x10startup,Opt79control,mixedlive3)retained. GuardstoppedONLYexacttestcontainers. Driverpostprocess0packagesbugfixedandcaseoriginalrawreportrecoveredwithoutrepeat. resource_abortalwaysoverridescached-harnessPASS. No policy/admission/Secretrotation/HTTP/metrics/nativeadaptiveGVAissuesfromoldisolatedauditwerefixedbythismodeltest;seeexistingreport.

AllCVtestcontainers/replays/referenceprobesstopped. Ownedreadonlyintel_gpu_top67624wasSIGINT-stoppedanditsunifiedsessioncompleted. No backgroundtestintendedtorun. Keepremote64benchmarkuntoucheduntilexplicituserinstruction. NexttaskshouldaddressSPECIFICmodel/shape/backendfidelityandUTCissuesifauthorized,notreruncompletedtestsorclaimmostoptimized/productionready. Furtherbuildsneednewimmutabletag;donotoverwrite d035candidate orfrozenOpt79. Noauto-pushrequested.


### QAFT publication — user requested all code, models and image (2026-11-05)

User explicitly authorized publication after the validation report. Published the unchanged tested image d035cb44df82a66cbcdaa29e3ca22177f7bfdbf080009d6a27bf6633e5eb2127 as ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-candidate. Registry digest sha256:53105b8a0c0654b9b50be6c6b7380223ac4f65588076c0c9ea9e32d6ecc1bd28; anonymous manifest pull verified config digest equals tested ID. The local manifest differs because registry serialization differs. No rebuild/retag-overwrite/policy/precision changes. Retain unqualified status and all fidelity/UTC/thermal/memory findings.

Personal repositories only: kiranmaibattu-cyber/sentry_cv and kiranmaibattu-cyber/PIPELINE, same sentry_cv_multi branch/release tag. Do not publish to ApexFlo without a separate explicit request. Existing PIPELINE worktree deletions and unrelated old changes are left untouched; publish the same complete sentry_cv snapshot into a new PIPELINE branch. Author remains Battu Kiranmai, no Sayandip attribution introduced.

Exact tested runtime source extracted from the image into model_updates/2026-11-05-qaft-int8/image-source (107 non-cache files) with SHA-256 manifest. Current development person_reid.py/dls_secondary.py differ due to the unpromoted Opt80 experiment; both development code and exact tested source are preserved, clearly distinguished. This publication does not claim the general Dockerfile reproduces the immutable image digest. QAFT overlay build context and source model export helpers/provenance retained.

Complete model bundle preserves 86 repository/image files with hashes (51 unique contents), including the >100MiB SigLIP artifact and all prior LFS weights. Versioned GitHub release assets contain models plus the full QAFT/optimization validation evidence; avoids Git file-size/LFS quota constraints. Small reports, structured summaries/audits, plots, model probes and test results also tracked in Git. Large captures/fixtures remain available in the release archive, not omitted. Unix socket files are not archive data and are skipped by tar. Publication credential scan found only the explicitly synthetic QUALIFICATION_PASSWORD logging regression fixture; no actual credential/token/private-key matches found. See RELEASE.md and SHA256SUMS. No new runtime tests or remote64 operations were performed for publication.


### Publication verified complete — 2026-11-05

Both personal sentry_cv_multi branches synchronized to 934797b29ce50782945d877968f9f0479fce432a before this final verification record; immutable release tag intel-285h-2026.11.05-v8-qaft-int8-candidate resolves to source commit576fac0bffef5e4d43931aeaa420e367defe7225 (subsequent commits are publication/README documentation only). Both GitHub prereleases contain all three assets with server SHA-256 and byte lengths matching local: model archive517261735bytes/95c079acf6d80ebc23b2b2460f02934ee69662a030c99ee0b7dd70d866751c64; full validation archive1699519535bytes/944d3f9754648c3e7f8057831854798dd34d35b042839dcf6d8257b1a00ee576; SHA256SUMS226bytes. All86modelarchiveentries independently read and hash-checked,0errors; full validation gzipintegritypassed. Downloaded QAFTstaticBIN, exactimageperson_reid.py, update.json and READMEfromBOTHpublicGitbranchesandverifiedbytesagainstlocal. Imageanonymousregistrymanifest/configidentityverified.

Initial PIPELINE import push was atomically rejected by GH008 because three historical LFS objects were unknown to that repository. Uploaded those three objects (11MB), then normal LFS/Git push succeeded; no force push, history rewrite, old file deletion or bypass of the server rejection. Initial skip-LFS attempt published no PIPELINE refs. The complete release model bundle remains an independent recovery path for LFS users.

Updated rootREADMEtoidentifyV8candidate and explicitlymarkolderV6instructionsas historical; preserved every earlier version rather than claiming V6deliveryforV8. Full publication proof: model_updates/2026-11-05-qaft-int8/publication-verification.json; registryproofseparate. Models and raw evidence are releaseassets on BOTHrepos; not just links to local paths. Exactimage source/weights/provenance, generaldevelopmentcodeincludingclearlymarkedOpt80experiment, tests, schemas, builds/nativehelpersanddetailedhandoffallpreserved. AllcommitsauthoredBattuKiranmai. NoApexFloorganizationpush. NoCVtestcontainersorbackgroundtest/uploadprocessesleftwhencomplete. Finalverificationrecorditselfisfollowupdocumentationanddoesnotalterreleaseimageortag.


### RTSPS Management connection report — diagnosis only (2026-11-05)

User reports Management can open four RTSPS URLs with FFmpeg but GStreamer fails setup. Requested exact image digest and sanitized GStreamer bus error asynchronously; no answer yet. Checked published QAFT candidate d035...: GLib GnuTLS backend supports_tls=True, glib-networking/libgiognutls/default CA database/ca-certificates present. rtspsrc validate-all default retained; no production TLS bypass. Fresh exact-image traffic1 H2641080 + ch9 HEVC1440 probes: FFprobe default and tls_verify1 bothPASS; native rtspsrc TCP -> video RTP -> fakesink receivesvideoin1.080/1.045seconds,0certrejections/errors/warnings. Thus RTSPS is not generically unsupported and missingTLSplugin is not reproduced locally. Does NOT establish all unknownfourURLs/Kubernetesenvironment/decode/modelcapacity. Need deploymenterror to distinguish CA/hostname/time/mount/module differences or other RTSPfailure; donotclaimprobablecauseasproved. FFprobe preflight and native GStreamer actualsource are separateclients/defaults. Diagnostic helper firstGst.init(None) failedGIbeforeconnection; corrected[] andpreservedfailure. No code/gates/contract/imagefix/rebuild/push. Artifacts/model_updates/2026-11-05-qaft-int8/rtsps-diagnostic/. Diagnostic containers exit/remove; no backgroundtest intended.


### Management evidence confirms camera04 unknown-CA and camera15 authentication failure (2026-11-05)

User supplied logs from container cv, pod sentinel-cv-blr-office-2-v8-96c47788c-9vfzp on zmd-pilot-1, reported using published QAFT candidate tag. Cameras04/05/08/12 have gstrtspsrc_retrieve_sdp generic connection error and wrapper UnsupportedSurfaceSource after3startupattempts. In-container isolated camera04 probe reports GTlsBackendGnutls/supports_tls=True and certificate_errors=[1]. GLib flag1=UNKNOWN_CA. This confirms camera04 cannot validate its server certificate chain against the container's available trust anchors; it is NOT missingTLSsupport. Exact cause (private/selfsignedCA, missingintermediate, truststoremount/config) remains toinspect. Equivalentcertificatechecksfor05/08/12areNOTyetprovided;donotextendconfirmedcauseautomatically.

Camera15separateRTSPprobe401Unauthorized confirmsauthenticationfailure;notgroupedasTLSunknownCA. Shutdownbase_inferencefailedonstop(4)/emptyinferenceinstanceisnotindependentproofmodelinferencefailed. Connectionfailsbeforevideodecode/modelwork. WrappererrorobscuresactualtransportreasonandwouldbenefitfrompreservingTLS/authcausesinsanitizedlogs; nofiximplemented.

Earlierlocaltraffic1/ch9RTSPSsuccesscheckedadifferentendpoint/certificaterouteanddoesnotcontradictproductioncamera04unknownCA. Remediationdirection:verifyserverchain/identity,provisionappropriateCAandintermediatechainwhilepreservingnormalvalidation;check05/08/12individually;correct15Secretcredentials/accessseparately. NoglobalTLSvalidationdisable,contract/policy/modelchanges,productionconfigchange,rebuildorpushauthorized/performedinthisexplanation. Userstatespriorrelaxedvalidationtestwasisolatedonly. Thischeckpointisuser-providedevidence,notanewtestontheirKubernetes host.


### Dev3-versus-QAFT TLS compatibility difference CONFIRMED (2026-11-05)

UserclarifiedV8dev3worked. Comparedactualbakeddev3(8c46...)withQAFT(d035...),notjustHEAD. Foundconcreteroutingchange:dev3SURFACE_MOTION_GATE_ENABLED=falseanddefaultFfmpegDecoder(VAAPI),RTSPoptionsTCP/timeoutswithouttls_verify;optionalnativeerrorscanfallbackFFmpeg. NewSURFACE=true/DLSTREAMER_REQUIRED=true:usesrtspsrcdefaultvalidate-allandpropagatesnativefailurewithoutFFmpegfallback. ThusmigrationchangedEFFECTIVETLSverificationbehavior,notdetectorweightsorGStreamerverificationdefault. BothTLSbackendswork;oldGst1.24.2/new1.28.2bothvalidateall. CAbundle121→120roots(5removed4added),notattributedtoofficecertwithoutchain.

ControlledlocalhostselfsignedcertwithcorrectIPSAN,serverdeliberatelyreturns404:actualFFmpegfromBOTHimagescompletedTLSandsentOPTIONS;GStreamerfromBOTHrejectedUNKNOWN_CA1withresourceerror7/retrieve_sdpFailedtoconnectGenericerror,matchingproduction04symptom. 404isintentionalfixtureandnotamediaPASS;nomodels/decoding. Ephemeralprivatekeydeleted. EarlierFFprobe-tls_verify1checksALSOacceptedselfsignedfixture,soCLIoptionwasNOTindependentproofRTSPcertverificationwasenforced;correctearlierinterpretation.

Thisexplainsdev3-defaultworkingversusnewdefaultrejectingunknownCAwithoutcamerachange. Oldprodactualenvoverridesnotobtained;scopeactualbakeddefaultspluscontrolledreproduction. Priorresponsewasincompletebyattributingonlysiteconfigurationbeforecheckingoldimage. TargetrepairissiteCAtrustsupportandexplicitTLSdiagnosticsinthecurrentnativepath,preservecontract/gates/VAAPI/DLS. Noautomaticbypass/rebuild/push/deploymentchange.05/08/12stillunconfirmed,15auth401separate. FullfindingsandJSON: model_updates/2026-11-05-qaft-int8/rtsps-diagnostic/dev3-comparison/.

Fresh paired live traffic1 check completed: both dev3 and QAFT GStreamer received video RTP with default certificate validation, no certificate rejection or bus error (1.051s and1.071s). Thus this public-trust source works in both clients/images; controlled untrusted-certificate test explains why it did not expose the office04 failure. Probes did not decode video or load models; all containers exited.


### RTSPS repair completed; user-selected legacy-compatible default (2026-11-05)

User requested repair, then explained collecting site CAs everywhere is impractical. Asked explicitly about the security default; user answered "Legacy-compatible by default, like dev3". New image therefore defaults SENTINEL_CAMERA_TLS_MODE=legacy-compatible: RTSPS encryption remains, certificate/server-identity checks disabled to match old FFmpeg behavior. Strict mode remains available with public roots and optional camera CA Secret/ConfigMap directory. This is an explicitly authorized transport-security change, NOT a claim that legacy mode authenticates servers. No desired-state fields or analytic gates changed.

Published new immutable personal GHCR tag intel-285h-2026.11.05-v8-qaft-int8-tlsfix2, configIDdbaeacc92973fc5d1e41521e3200cd64151aaa53a472404a79f87833bade3ef5, registrysha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc. Anonymous manifest/config verification succeeded. Old candidate tag not overwritten. Overlay of exactoriginalpublishedQAFT d035..., no Opt80 experiment. Byteinventory verifies ALL33bakedmodelfiles and allcontracts/policyunchanged; onlysurface_stream/multi_camera_gstchanged,source_tlsadded,2TLSenvsettingsadded. CurrentgeneralDockerfileadds glib-networking explicitdependency forfuturebuilds; publishedoverlayusesexistinglibraries.

Trust/callback setup occurs BEFORE a shared camera bin is attached/synchronized, preventing raceonlatecameraaddition. Failedconfigurationstartupownerreleasedsopeerscanstart. MeaningfulsanitizedTLSreason/camera/sessionlogs,probestderr401preserved,finalretryerrorretainscause;realframesclearoldTLSstate. Camera15actual401needsManagementcredentials/access,notfixedbyTLSmode. Office04/05/08/12notdirectlyaccessed;needManagementrolloutverification. No productionpodconfigmodified.

Tests:96PASS/1Opt80-onlydeselected,Opt80testmoduleexcluded.15nativeTLSmatrixPASS(defaultlegacyunknown/hostname/expiry/shared;strictunknownCA,mountedCA,explicitCA,badhostname,expired,malformedPEM,sharedtrusted/untrustedpeerisolation,invalidleaderconfigurationpeerprogress,auth401,publicTLSwithextraCA). Strict127vslegacy0confirmedbeforeNULLsourcestart. SyntheticserverOPTIONS200andDESCRIBE404onlyprovesTLS+RTSP,notdecode. InitialfixtureOPTIONS404thenmissingSETUPmistakesandregressionmountsetupfailuresretained,notruntimefailures. Strict-onlytlsfix1intermediatewaslocal/unpublishedandsupersededafteruserchoice.

Realvideoproof:temporaryselfsignedTLSrelayoverexistingtraffic1,upstreamTLSverified. StrictcontrolrejectsunknownCA1;legacycontrolreceivesRTP. FullactualnewimagewithNOCA mounted,allbindings,1camera4requestedFPS/30secsteady/15secwarmup,VAAPI+DLStreamer healthy,0fatalnative. AgentAPIaccepted681packages347JPEGs37vectors(33scene4body); noface/gaitpositiveclaimfromthisscene. Savedsubset52packages42JPEGs35vectorsaudit0errors. Sampled662/662ACKcutoffdifferentfromfinal681,5backpressure0exhausted/protocolerrors;doNOTsubtractaslossorclaimadmissiondefectfixed. Frame-selectionratesexceeded4target;existingPTS/UTC/catchupnotfixed,unmarkedsourcefreshnessnotindependentlyproved. RawharnesscapacityPASS isnotnewcapacityqualification. Noaccuracy,modelnumericalfix,memoryfix,or15–20camclaim.

Alltestcontainers/relaystopped;ephemeralprivatekeysdeleted. Persistentreports/buildcontexts/sourcehashes/inventories/command/rawcapturesunder model_updates/2026-11-05-rtsps-compatibility-fix/. DEPLOYMENT.md tellsManagementtouseNEWtag;nodeploychangeperformed. GenericpublishcontinuationtargetsBOTHpersonalreposonly,currentbranchsentry_cv_multi;noApexFloorgpush.


### RTSPS repaired release publication verified (2026-11-05)

Codecommit326dbb63523d9a1d1cba903518e70b96ff8c98d0pushedtobothpersonalreposonbranchsentry_cv_multi;identicalimmutableGittagintel-285h-2026.11.05-v8-qaft-int8-tlsfix2. BothGitHubprereleasescreatedwithfull11823542-byteTLSvalidationarchive,serverSHA2567b3b069849e19b0de266d094f509c864e8a90a764ceeab4b25b03d9f6e9ce6d6,plusSHA256SUMS;bothserverassetchecksumsverified. Registryanonymouspull/configidentityverifiedearlier. Alltestcontainersandrelaystopped,noprivatekeysarchived,workingtreecleanatcodepublication. Thislastverificationisdocumentationonly;imageandtagunchanged. ManagementmustdeployNEWtlsfix2tag/digest;oldcandidatecontinuesitsoldstrictbehavior. DefaultlegacymeansNOcameraCArequired,perexplicituserchoice. Camera15needscredentialfix. NoApexFloorganizationordeploymentwrites.


### Forward test catalog created after testing-process review (2026-11-05)

User agreed that earlier checks confused execution/schema/output presence with semantic correctness and asked for all tests, especially ignored edges. Created PIPELINE_TEST_PLAN.md, PIPELINE_TEST_MATRIX.json and PIPELINE_TEST_LEDGER.csv:274namedscenarios/28areas,236P0and38P1. These are case definitions,not274implemented/runtests. EverynewexecutionstatusNOT_RUN,priorcoveragecapturedseparatelywithreferencesandlimitations. VerifieduniqueIDs,274consistentMD/JSON/CSVrows,28areas,allreferencedfilesexist; thiswasdocumentconsistencyvalidationonly,NOTCVtestexecution. Noimagebuild/runtime/policy/schema/testexecutionorpushwasperformedforthisplanningtask.

Per-case scenario, required behavior, forbidden behavior, evidence, priority and run/verdict fields. Explicitindependentsemanticoracles,boundaryT-minus/T/T-plusofeveryapprovedgate,signedsource-timechecks,noUUIDliteralparitywithoutsemanticmapping,runimportpath/hashverificationandactualpodcompatibility. Allusecasescoveredendtoend:presence/tracking/zones/count/dwell/lines,face/body/gait/ANPR/scene/fire,artifactsandagentAPIplusdeploy/TLS/config/model/scheduling/time/health/resources/performance. Addeddedicated12-caseadaptivecategory;notjustresolvertelemetrybutactualnativeconsumers,bounds,per-camera/per-usecaseisolation,hysteresis,missingmetricsandpressureseparation.

Preservedknownopenfindings:QAFTfidelity/dynamicroute,UTCFUTURE/selectionovershoot,refused-admissionqualitystate,retiredgaithistory,Secretrevisionreconciliation,nativemembershiplongjmp,adaptiveGVAintegration,read-onlymetricscrashes,nestedcredentiallogging,HTTPnativegap. CurrentTLS15scenarioscreditedonlytotheirnativeconnection/fixture/singlelive-relayscope,notallproductionpods. Existing96tests/hour/provenoutputsnotblanketcasepasses.

Runorderfreezeimage/harness/pod->deterministicusecasepositiveandnegativeoracles->nativeframes/evidence->same-inputcomparison->faultsandraceboundaries->active8/10FPScapacitywithsceneeverycamera->selected10minthen1hrqualificationifrequested->ManagementrolloutsmokeendingattheagentAPIboundary. Cheapaffectedregressionsbeforehours;stopfailedcapacitybranchesandkeepinvalidsetup/resourceabortvisible. Remote64testingstillrequiresnewexplicitpermission. Managementdatabase/search/globalidentityanddeferredaction/clip/recheckoutsidecurrentCVscope. AddedforwardlinksfromhistoricalISOLATED_VALIDATION_PLAN.mdandREADME. Nexttaskistoagreefixture-specificexpectedtimelinesandrunprioritizedcaseswhenuserasks,nottoclaimthiscatalogalreadyclosedthem.


### Explicit5/8/10FPS capacity and per-camera use-case budgets added (2026-11-05)

User asked whether performance/capacity tests cover5,8,10FPS, scene on every camera and how much extra analytics fits on each camera. First draft explicitly covered8/10 but omitted5; corrected the plan rather than claiming it was already explicit. Added8PERFcases(09–16),maincatalognow282cases/28areas,242P0/40P1. AllnewexecutionstatusesNOT_RUN; no CVtests/imagechanges/build/push performed in this planning update.

New CAPACITY_TEST_PROTOCOL.md + CAPACITY_TEST_MATRIX.json + CAPACITY_RESULTS_TEMPLATE.csv define3rates,12uniformprofiles(scene floor,presence/dwell,counts,geometry/entryexit,face-only,body-only,face+body,face+body+gait,vehicle+ANPR,completevehicle,hazards,all11apps)and9mixed-roleprofiles(identityandANPRshares25/50/75/100plusbalanced;0expensiveshare=scene-only).63emptyresultrows,allcountsblankandNOT_RUN. App/profile descriptors checkedagainstcurrentV8schemaappnames;theseareNOTfulldesiredstateconfigsornewmeasurements.

Scene JPEG+768D onEVERYcamera,active2s/control1view;existingquiet5ssweepreportedseparately. ActualdesiredFPSdemand=sum(cameraFPS),uniformN*FPS;activebasicscenedemandN/2persec,notanartificialaggregatecap. Search1/2/4/8/12/16/20/24/32/40/50onlywhileuseful/safe,refineneighboringpassfailcountsandnativegroupboundaries. HardwarecapacityNOTschema50. Sourcesnative720/1080/1440andmixed,actualROI/positiveworkload,shortsubjectturnover/longstays;noquietblankall-enabledcapacityclaim.

FixedN/FPSadditional-usecasefrontierandmixedcamera-rolefrontierarebothrequired:whichbundlesfit,howmanycamerasgetidentity/ANPR,actualintegerpercentallocationandfairness. A numberofappnamesisNOTauniformcost;identityprofilesandperson/vehiclecandidatecountsmatter. Preserveallquality/emissiongates,scene/evidence,hardwareplacementandfixedgraphcostaccounting.

Fullperformancemetrics:raw/selected/admitted/completed/quietFPS,allmodel/queue/pre/post/policy/convert/crop/JPEG/handofftimings,independentageandUTC,camera/agentacceptedoutputsandbytes,uniqueCPU/cgroupRAM/RSS/PSS,state/surfaces/FDs/threads,GPUengine/NPUbusyscopes,temps/power/frequency/throttling,network/disk. ProcessingFPSnotconstantoutputeventrate. Captureunsupportedcountersasunavailable,sharedhardware/RAMoverlapnotdoublecounted. Proposedcumulative/windowfloors5:4.75/4.5,8:7.6/7.2,10:9.5/9;alsofreshness/scenecompleteness/upperselectionbounds/state/resource/thermalpassrequired. Timings/SLAcriteriafrozenbeforetests,notnewcontractfields. Short2–3minsearch,selected10minpre-soakthenonehouronlywhenrequested/appropriate;nofullCartesianhours.

Updatedmainplanmatrixmetadata,CSVledger,READMEandhistoricalplanforwardlinks. Handoffforwardindexnow282. No capacity numbers filled fromold4x8/3x10results; previousmodels/clock/memoryfindingsremainopen. Remote64restrictionunchanged. Next step is actual prioritized execution when requested, not automatic capacity claims from these descriptors.


### New isolated TLSfix2 campaign on 192.168.1.64 — preparation

User explicitly authorized isolated testing again and stopping competing applications. Target immutable registry digest sha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc, QAFT Opt79-based tlsfix2 (not Opt80). Remote root /home/admin1/Documents/sentry-validation-v8-tlsfix2-20261105. SIGTERM blackbox.py (PID1053491, cwd /home/admin1/chandana_optimization/group_ingest/app) and SIGINT stale intel_gpu_top PID1058266, restart metadata saved before action. Essential services preserved. Registry pull completed; prior campaign/fixtures preserved. Initial free root12GB/home15GB, RAM~29.8GB; thermal/disk/memory guards required. Stock power saved; no settings changed yet. New scripts/test dependencies transferred, no runtime/model overlay. Capacity profiles scene every camera, 5/8/10FPS. Fresh plan cases remain NOT_RUN until actual assertion/evidence. Agent API is terminal delivery boundary; downstream Management excluded. No image or contract/policy changes. Next: actual-image preflight/regression, then marked native busy profile capacity search and fault tests. Do not restart historical campaign or reuse its ledger.


### Isolated remote campaign checkpoint — TLSfix2 preflight and thermal abort

Actual unchanged registry image pulled on .64, all52 rootfs diffIDs/user/environment match local release (Docker reports manifest ID while Podman shows config ID; no rebuild). Completed on remote:96 runtime tests passed(1 Opt80-only deselected; absent Opt80 module ignored),30 injected actual-policy scenarios passed,15 native TLS scenarios passed,canonical/embedded V8 validators passed, required13 plugins and IntelGPU/NPU visible. Configuration fixture initially failed to mkdir privileged /run/secrets; corrected with owned bind mount, baked validator then passed all checks. Keep original failed setup log, not image defect. Generated63 profile/rate desired states with scene every camera; schema checks passed, baked validator execution recorded separately.

First SA1x8 stock-power startup hit105C for5samples with28.35GiB availableRAM; guard stopped owned container before steady measurement. NOT capacity failure or valid throughput result. Preserve SA-1x8-stock/resource-abort.json and raw logs. Applied earlier-campaign thermal profile no_turbo=1/PL1=45W/PL2=65W, original no_turbo=0/PL2=115W saved for restore. No image/model/gate/contract modifications. New capacity IDs suffix power45w. Status/HANDOFF and per-run raw captures on remote; independent markedPES->completeagentAPI acceptance, host/cgroup/GPUengine/NPU telemetry running. Host cv2 observer dependency was missing; installed isolated --target observerdeps, no system packages/image changes. Next tests begin after old aborted harness cleanup. Cases not asserted remain NOT_RUN; no 63 capacity passes claimed.


### Remote TLSfix2 measured progress — isolated power-controlled campaign

Stock SA1x8 startup stopped at105C, excluded from capacity. First45W retry excluded INVALID_REPLAY_SETUP (PTY stopped FFmpeg before bytes); repeated with setsid/stdin=/dev/null and sender-nostdin. One additional5FPS setup attempt failed because host Unix-socket pathname exceeded AF_UNIX length after moving temporary directories; actual native container never started. Test directory shortened to /home/admin1/.sv8-tls2 (on home partition), owned idle sender terminated, separate retry ID. These are harness defects, not native camera failures. Original attempts retained. No CV runtime/image/gates changed.

Completed valid SA1x8-power45w-r2:encoded1080p crowded replay,target8,inference7.89556FPS,selected7.99562,minimum10secinference7.63478,scene56steadysamples/p95=343.264ms/p99=366.579ms measured independentPES-to-completeagentAPI,meancontainer2844.07MiB(2.78GiB),meanCPU0.766cores.854acceptedpackages/296JPEGs/192vectors,profiles75scene88body20face9gait; no fatal native errors. This exercises positive identity paths, not hazard accuracy or all ANPR conditions.5FPS valid retry also metshort rate/freshness checks(inference4.98463,scene55samples,p95=342.869ms).10FPS running.

Independent UTC check reproduces inherited defect:1x8 steady661observations have observed_at ahead of accepted_at by7.5–11.18seconds despite fresh images. Signedtime retained; no zero-clamp used as latency proof. Initial harness/verdict called short result capacity_qualified; assessment is being separated into short_performance_passed vs UTC-contract/full sustained qualification. Original early verdict archived, raw counters/bytes unchanged. Never advertise these short passes as full production capacity or timestamp compliance.1x8 rawVAdecoded~30.0177FPS; selected/inference~8. Steady package temperaturemax72C under45W/no-turbo profile; RAMendpoint+48.91MiB over110sec, short slope18.08MiB/min is not a long-term leak proof. Retainedgaitstate50presences/546silhouettes/148centers at end.

Baked config validator accepts all63 generatedprofile/FPS states,scene everycamera. No capacity for63rows claimed. Controller ledger and per-case raw snapshots persist on.64. Canonical plan ledger retains unassertedcasesNOT_RUN. Next:finish10FPS control,reassess labels/UTC,performascendingmulticamerasearchscene-onlyandall-declaredbindings,thenrole/vehicle/fault/motion andselectedsoak. Restore originalhostpower and cleanownedtestprocesses when finished.


### Remote .64 TLSfix2 campaign — checkpoint 3 (UTC 2026-11-05, IST 2026-11-06)

Goal remains frozen-image execution/performance/use-case balance qualification. User explicitly authorized stopping competing applications and testing `.64`. No CV runtime/model/contract/quality-gate changes, rebuilds or pushes have been made. Current immutable QAFT TLSfix2 digest: `sha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc`. Native tests load baked source/models. Standalone test dependencies/mounts and negative hardware/model masks are test infrastructure only.

Remote root: `/home/admin1/Documents/sentry-validation-v8-tlsfix2-20261105`. Current JSONs `STATUS.json`, `CURRENT.json`, `run-ledger.json`; structured phase history `campaign-events.jsonl`, cumulative `HANDOFF.md`. Do not restart historical campaign or overwrite existing case IDs. Native case folders have invocation, role map, full health snapshots, marked-source PES/epochs, resource monitor, raw container logs and accepted packages, per-camera latency/verdict. Raw dirs are chowned after the finished run so admin1 can inspect them.

Isolation/power: blackbox.py1053491 and old GPU monitor1058266 stopped; OS/SSH/network untouched, metadata saved before stopping. Stock startup hit105C and was excluded. Current controlled profile is no_turbo=1, PL1=45W, PL2=65W, with actual startup4core/steady16core quota verified. Original settings0/45W/115W/205W retained. New host monitor1452104 and GPUengine monitor1452105 record node scopes. Only owned test processes/containers may be cleaned. Original blackbox remains stopped; restart command/cwd recorded, original environment was not captured so do not silently invent a full restart environment.

Completed checks:96 runtime tests(+1explicitly deselected Opt80 case; Opt80-only module ignored),30 actual-producer synthetic policy scenarios,15 native TLS fixtures, canonical/embedded validators, all13requiredplugins/device visibility/model inventory. All63 generated profile/FPS desired states accepted by baked validator; this is configuration coverage, not63 camera-capacity runs. Compatible capture API validates every accepted package's schema/bytes/hash/links;8 explicit receiver protocol/missing-byte/idempotency checks passed. No real agent storage or downstream Management tested per user boundary.

Observed defects remain failures: (1) source observed_at is7.5–11.18sec ahead of agent time despite independently fresh JPEGs; host NTP is synchronized and both clocks are same host; (2) rejected face/body/gait admission advances sent-quality state and suppresses later identical qualified sample; (3) worker nested diagnostic values bypass redaction on synthetic marker, not evidence of actual leaked credentials. Initial gallery audit saw zero images because a700raw parent blocked the nonroot auditor; corrected direct gallery mount script is staged, original invalid attempt retained.

Native robustness failure: `SA-5x5-power45w-r2` aborted at startup with stack-smashing then2109longjmp lines. First native context was near18:06:07UTC, process55; noCL_OUT_OF_RESOURCES in this log. Supervision later restarted worker423; only3/5sources recovered because one-client test publishers had closed, so subsequent reconnect failures are partly fixture-limited. Genuine native abort is NOT a GPU-exhaustion diagnosis or a physical maximum of4cameras. Report has no steady startup completion and is not a throughput measurement. Raw logs preserved.

Short people-heavy encoded1080p30 control results: SA1cam5/8/10 passed rate and independent scene-age checks; inferred4.9846/7.8956/9.8707FPS, sceneP95~343/343/369ms.1x8 meanRAM2844MiB, CPU0.766cores; all-runtime output854packages/296JPEGs/192vectors(75scene88body20face9gait); no labelled accuracy claim. RawVAdecoded30.018FPS vs selected7.996. SA2x5,4x5,2x8,3x8,2x10 short passes. SA8x5 failed: sceneP95~5.1–8.6sec and several low windows; tail20sec remained~4.7–6.2sec for7/8cams, so this is not just an early warmup outlier. SA4x8 had two camerasP95~2.3/3.6sec; SA4x10 worst~6.1sec; SA3x10 worst~2.416sec. These are profile-specific short tests under controlled power, not maximum/sustained capacity or full production qualification.

Scene-only curves underway. S1/2/4/5/6x5 short passes. S7/8x5 kept FPS and image freshness but failed complete scene cadence. S8x5: all cameras~5selected/~4.89inferred, imageP95<=384ms,600packages600durable,14backpressure refusals; some cameras50samples vs55 and gaps>5sec. Scene policy pressure mode normal; refusals make output admission an observed limitation, not evidence GPU exhausted. S1/2/4x8 passed; later counts continue. Per-camera actual output/workload fields retained.

Assessment labels corrected conservatively: `short_performance_passed` is short rate/freshness/scene cadence; cumulative inference>=95%target and minimum10sec window>=90%target, selected upper bound105%. `capacity_qualified=false` for all short runs; UTC failure and sustained qualification separate. Old initial verdict JSONs with loose capacity_qualified naming archived as verdict-initial.json. Raw counters/images unchanged. Native capacity controller still has its already-loaded older assessment but same effective floors for current points; future phases load latest criteria. Never advertise healthy/FPS-only as overall pass.

Test harness failures fixed and preserved as INVALID setup, not image failures: inherited PTY stopped FFmpeg before bytes; Unix-domain pathname too long under verbose root; cross-filesystem copy attempted live socket. Current sender -nostdin, controllers setsid/stdin=/dev/null, live temporary root `/home/admin1/.sv8-tls2` on home, socket removed after receiver stops before archiving. No blanket deletion of /tmp or previous evidence.

A new fixed comparison fixture was built locally away from the test host and transferred: `fixtures/marked-mixed-pv1080p30.mp4`, SHA256`0e5dfc22acf34eb82cf0951eeabeb78aeadb9879454ddccc45ed73cbc33cf0ce`,85842336bytes.120seconds/3600frames/1920x1080/30FPS, first60s crowded source, next60s ANPR720p10 clip upscaled and tripled to30FPS; continuous0..3599CRC bar. This is a transformed controlled workload, not a native captured single-camera dataset or labelled accuracy corpus. Same bytes across all profile budget points, raw input rate fixed; manifest and encoder command saved. Unmarked quiet/motion tests deliberately avoid barcode motion contamination.

Automatic continuation is running: capacity controller actual PID1462368 (phasecapacity); remaining campaign supervisor actual PID should be discovered by command line `remaining_campaign.py` (nohup starter1503595forked). It currently waits for capacity completion, never starts a competing GPU case. Root interactive SSH session51019 is live; shell R and I variables point to remote root and digest image. NormalSSH admin1key auth works; sudo-n does not. Do not persist password.

Remaining phase order in `remaining_campaign.py`: mixed-budget -> compare -> native-scenarios -> faults -> soak. Finite scripts checkpoint every case. Mixed-budget first does two explicitly labelled higher-count processing diagnostics(16x8 and20x10 scene-only) because earlier scene-admission failures are not physical limits; no cadence failure is overridden or higher production capacity claimed. Then for each5/8/10 rate choose fixed camera budget from measuredS/SAcurves; run all19 nonduplicate uniform/mixed profiles on same mixedPV fixture;100%identity/vehicle equivalence toSIFBG/SVX is recorded but must verify canonical config equality before credit. This yields budget at a stated count, not every profile's maximum count.

Compare: QAFT and cached originalOpt79 (`localhost/sentry-v8-opt79-shared-gait-background:latest`, DockerID626ef9f4ff8d) at3x8 same mixedPV; model changes can change ROI load, so not an accuracy/output-parity claim. One45second GStreamer element-latency tracing run is diagnostic only, not capacity; test env GST_TRACERS/GST_DEBUG explicit.

Native-scenarios.py staged: unmarked quiet->active->quiet, quiet safety sweep, native vehicle/ANPR, fire suspect/clear; full liveRTSPS traffic1; ephemeral untrustedTLSvideo relay with strictnegativecontrol; failed first/last TCP camera with healthy peer using persistent source; GPUmissing/NPUmissing/maskedprimary-artifact negative controls(noCPUfallback and matching diagnostics); correctly mounted sampled galleryaudit. Scope each narrow assertion; native presence of events is not labelled accuracy or complete catalog timeline proof. Capture API is terminal boundary.

Faults: source15secoutage/peerrecovery, agent503/429/dropACK/slow; scenario-specific oracles still need final analysis, raw capacity failure during injected outage is expected and must not be used as fault verdict. Soak: choose highest short-passing SA8FPS count,10min pre-soak; descend if needed; only then one real3600sec diagnostic stress. Existing UTC/admission bugs still block full release even if stress passes. If no pre-soak qualifies, report blocked rather than force known-failing hour.

`remaining_campaign.py` finally stops owned containers/observers and restores original power on completion/error. It stops on resource/driver error and records reason. Native fatal tests that finish driver normally are FAIL results and do not prevent unrelated tests. `STOP_CAMPAIGN` is respected between/in continued phases; user stop needs immediate owned current-process/container cleanup as well. Original blackbox intentionally left stopped; restart metadata retained.

No final report or full282-case mapping yet. Handoff and local ignored snapshot under `optimization_evidence/2026-11-05-remote64-tlsfix2` preserve progress. Need later final artifact/time/resource/ACK/gap analyses, genuine coverage map with unimplemented/unasserted cases NOT_RUN/BLOCKED, capacity and use-case table with conditions, and publish only if user asks. Do not claim all282 tests implemented or run.


### Remote campaign checkpoint 4 — UTC2026-11-05 ~20:25 / IST2026-11-06

Active task remains full isolated frozen-image testing, not a runtime optimization patch. Published QAFT TLSfix2 digest unchanged, no production source/model/gate/schema edits or pushes. Remote root /home/admin1/Documents/sentry-validation-v8-tlsfix2-20261105. Shell root session51019 still available; R/I variables unchanged. New waiting supervisor replaced old1503596 while it had NOchildren and capacity controller1462368 was still active; no GPU test interrupted. Current supervisor was nohup starter1520010 (actualdaemon PID discover by cmdline remaining_campaign.py). Root supervision now order compare -> native-scenarios -> faults -> mixed-budget -> soak. It handles owned cleanup/power restore finally.

Capacity search completed at19:20UTC; compare finished19:27:14, native-scenarios finished20:07:09, faults finished20:21:33. Mixed-budget is now running. Read STATUS/CURRENT/run-ledger, not these checkpoint timestamps, for live state. Core crowded1080p30 short pass points remain SA4x5/3x8/2x10, with higher points failing freshness or native startup. Scene-only default cadence search had S6x5 pass, S7/8x5 cadence gaps; S4x8 pass, first later point failed scene cadence; S6x10 pass, S7/8x10 stale images. These are largest OBSERVED short passes, not monotonic physical maxima; queue refusals and startup variability make that distinction essential. Exact full results in ledger.

Important hardware/timing evidence: S8x10 used meanGPUCompute72.32%(peak96.58),Video14.34%,VideoEnhance24.33%,Render/Blitter0;110sec alignedsamples~108. RC6 active residency is separate. Per-camera frame processing~62ms; primary_selective_bgr~33ms1047calls/1170processedframes(~90%); metadata_bridge~21ms. Even scene-only active objects force substantial BGR work; primary remains VA surface inference. This is a measured hot path, not proof of root cause or a claim all branches are zero copy. source_progress_age.py adds a calibrated replay-progress diagnostic: firstsource-second median(transmittedMonotonic-sourceIndex/FPS), never updated to hide stalls. It detects publisher backpressure excluded by PES-to-API. SA8x5 worst replay-progress lag~22s; good SA1x8/4x5/3x8/2x10 controls remained~.3–.45s bythismetric. Scope is controlled replay, not actual camera captureUTC.

Paired same mixedPV input3x8: newQAFT short pass, percam7.92/7.93/7.90FPS, sceneP95~1.240/1.115/1.057s,meanRAM4362MiB; vector totals224scene173body46face12gait. Cached Opt79 oldFP16 same input~8.01/7.99/7.98FPS but worstsceneP95~2.340s (others1.746/1.585),meanRAM4257MiB. One pair only; differing detections/ROI demand mean no universal speedup/accuracy claim.45sec GStreamer element-latency trace of new3x8 captured with GST_TRACERS latency flags and GST_DEBUG; diagnostic instrumentation, not capacity. Raw container log saved.

Native scenarios ran: unmarked motion/quiet safety; ANPR read+unreadable paths (112plate_read/84plate_outcome/47line_cross on vehicle fixture; not text accuracy);720p/1440p identity outputs; fire suspected/clear; actual liveRTSPStraffic1; full actual untrustedTLSrelay with noCA and strictrejectcontrol; failed first/last source while peer healthy; missingGPU/NPU/maskedprimary; live config, sourceSecretrotation, membership, workerkill, common-cutoff APIreconciliation, read-onlymetrics. Source/barcode unmarked note: quiet/motion fixtures have no test barcode so they cannot spuriously wake motion. Other control replay default fixture includes visualbar but unmarked flag only disables independent sender-marker analysis.

Positive RTSPS results: live public stream and full self-signed relay both processed evidence through compatibleAPI on.64. Additional strict-public-no-extra-CA probe passed RTP,validation_flags127,certificateflags0,before-source configtrue.15native TLSmatrix alreadypassed. Ephemeral relay privatekeys deleted; no trust/rootCA deployment change. Input first/last unavailable controls passed healthy peer progress and readiness.

Negative device controls: both GPUhidden and NPUhidden stop inference/worker,0camera outputs/noCPUfallback; overall diagnostic-string assertions returnedFalse because generic secondary-no-output errors did not match expected device cause. Do not misreport CPUfallback occurred; backend diagnostic specificity needs review/fix. PrimaryXML /dev/nullmask control passed explicit rejection. Only negative test mounts hide model/device; positive tests still load baked files.

Actual control semantic review is control-semantic-review.json. SourceSecretrotation FAIL: parenthealthrevision2; changedcamera01 kept76scene images revision1/1920x1080, noexpected1280x720 new-source frames. Unchangedcamera02 DID switch torevision2, so do NOT generalize that all config updates are ignored. Overall package revisions606rev1/402rev2. Replacement publisher19112 independently verified alive, correctMPEGTS bytes, backingquiet720file. Ordinary geometry/config changes emittedrevisions1/2/3/5, so supported updates DO apply. Membership test outputrevisions1/2/3; removedcamera had no lateoutputs inreview window; exact semantic review stillpending. Malformed/same-revision/unreadableSecret behavior and /healthy404 need final action-snapshot review.

WorkerSIGKILL recovered both cameras with new sessions: semanticreviewPASS, no claim this simulates a native single-model deadlock. Common-cutoff APIreconciliationPASS: quiescedproducer, CVaccepted states match receiver uniqueIDs/objects/contiguoussequences. Read-only metrics FAIL:25PermissionErrors and camera-thread exceptions; observability write failure disrupts analytics (afterpermissionrestore mayrecover; not fake-source failure). Original type/scope facts retained, do not turn diagnosticsNULL into PASS.

Handoff faults:15sec source interruption report has0nativefatal and final1852admitted=1852durable,22backpressure; scenario isolation fields stillneed finalreview.15sec503:admitted1551,durable1546,coalesced1,exhausted4,backpressure160.429:1574/1567,coalesced3,exhausted4,backpressure138. LostACK:1647admitted,1501newdurable,144duplicate200,coalesced2,exhausted0,protocolerrors0. Correct accounting is newdurable+duplicate = admitted-coalesced-exhausted, with endpoint/cutoff alignment. Never compare durable-alone to total APIunique IDs and call144missing. Slow-agent test completed next; read actual report. Qualified feature state admission bug remains separatelyreproduced. No real durable sidecar/Management test; user boundary ends at compatibleagentAPI.

Harnessexception-reading fix: remote_case.py now preserves /healthz JSON from HTTP503 via HTTPError body rather than treating it as no endpoint. Actual wirepayload unchanged; HTTPstatus saved separately in health-samples. case_actions.py test helper also parses errorbody. Source clock/metrics in CV untouched. This is necessary for outage/negativecontroltelemetry, not an image fix.

Remaining queued mixed-budget: first16x8 and20x10 scene-only highcount diagnostics.16x8 already fails target128 with aggregate~54–97FPS, RAM~3.3GB, all16recent; final result/latency pending whencheckpointwritten. These diagnostics do notoverride default-scene cadence failures or qualify thosecounts. Then19distinct uniform/mixed profiles at5/8/10 on fixed mixedPV bytes;Nbudget derived min(scene shortpass,2*allapp shortpass).100%identity/vehicle logicalrows referenceSIFBG/SVX; require canonical generated config equality proof beforecredit, no separateGPUrun claimed. This is a fixed-N use-case budget table, not maxima for everyprofile.

Afterbudget:10minute SA8FPS pre-soak at highest shortpass; descendcountifitfails, then one full3600sec stress. If noneprequalifies, markhourblocked instead of forcingknownfail. Existing UTC, source-update, admission-state, logging, readonlymetrics and nativeabort issues blockfullreleaseevenifrate/soakpass. QAFTCPU/GPUfidelity and nativeGVAlackoflegacyadaptiveassociationconsumer remain knownunfixed; accuracydeferred, no claim otherwise. HTTPnativeacceptancegap remains code-confirmed; no fullHTTPvideo qualification inthiscampaign yet.

Need final work: inspect actual currently running diagnostics/budgets, watchguard/remaining-supervisor errors, complete phaseexecution, scope-specific fault/control verdicts and sampled galleryaudit, parse elementtrace and resource/time/memory/handoff/all-scene gaps; map actual assertions to282case catalog leaving unimplemented/unasserted casesNOT_RUN/BLOCKED; produce concise 5/8/10 capacity+usecase table with conditions, not a single universalcamera limit. Restore originalpower and stop only ownedtests/observers onexit (supervisorarmed); originalblackbox intentionallyleftstopped with savedrestartmetadata. Save finalreport and handoffs, no automaticGit/org/registrypush. Current local snapshots are ignored optimization_evidence/2026-11-05-remote64-tlsfix2; REMOTE64_TLSFIX2_TEST_STATUS.md is an ongoing draft and must be updated before final.


### Remote campaign checkpoint5 — UTC2026-11-05 ~20:48

Frozen image and scope unchanged. Current mixed-budget phase is active; remaining-phase supervisorPID1520011. Root SSH PTY session51019 remains active. Read remote STATUS.json for current case; at20:45 it was SIB-mixedPV-6x5-budget. Completed budget S/SP/SC/SX/SIF6x5 passed short checks, SA6x5 failed throughput/freshness/cadence (meanRAM5833MiB,CPU3.91cores; independently calibrated replay progress age p95~14–16sec). Do not call this memory slope a leak: lazy model initialization and people-to-vehicle workload switch happen during the short run.

review_controls.py now independently asserts ordinary config updates and two-to-one-to-two membership PASS. Source-Secret replacement remainsFAIL, unreadable metrics storageFAIL25PermissionErrors; worker-kill recovery and common-cutoff API reconciliationPASS. Native quiet actual counters:765selected,740quiet,25inference,19scene; steady quiet scene gaps~5sec. Motion transition active gaps~2sec, quiet~5sec, boundary max~6.32sec. Correctly label relative cadence versus broken UTC event mapping.

Local report_snapshot.py generates REMOTE64_TLSFIX2_VALIDATION_REPORT.md and REMOTE64_CAPACITY_RESULTS.csv plus REMOTE64_STAGE_RESULTS.csv from completed evidence only; currently ongoing. Snapshot fields never qualify production capacity. Diagnostics show signed future event timestamps and partial catalog coverage.

Disk management: local helper offload_budget_evidence.py runs in tool session21309. It rsyncs only completed current-campaign budget/pre-soak/hour cases to compute-a optimization_evidence/2026-11-05-remote64-tlsfix2/, verifiesSHA256 of EVERYgallery file locally and re-verifies remote gallery unchanged, writes EVIDENCE_RELOCATED.json at both locations, then releases only that remote gallery. All other case files stayremote. No old campaigns deleted. Do not try remote gallery audit after relocation; use verified local copy. Local watcher postprocess_relocated.py in tool session72589 generates resource-and-time-summary/source-progress-age on verified local copies, consuming no testhost compute. Local38GiB free originally; remote9.5GiB after first offloads. Both helpers run foreground in tool exec sessions because detached local nohup was reaped; retain sessions/poll as needed.

12 exact generated desired-state equality checks passed for logicalM-identity-100≡SIFBG andM-vehicle-100≡SVX atN4/6,FPS5/8/10. Saved artifacts/profile-config-equivalence.json. These are configuration-equivalent rows, not additional GPU executions.

Future soak test infrastructure updated only: controller-power.py (SHA previously matched localcontroller.py) now uses SAMEalternating60sec people/60sec vehicle1080p30 marked fixture for600sec pre-soak and3600sec stress. This broadens positive identity/ANPR workload; hazard positive/clear is separate. Running mixed-budget interpreter already loaded and unchanged; future soak process will load this test-only change. No image/runtime/gate/schema edits.

Still pending: remaining57fixed-budget runs total, selected pre/hour soak, finalsemantic/fault review, resource/trace analysis, case-level catalog coverage, archive/report, power/ownedprocesscleanup verification. Avoid claiming all282catalog scenarios passed. User boundary remains complete accepted agentAPIpackage; no downstreamManagement/realagentdisk durability claim.


### Remote campaign checkpoint6 — UTC2026-11-05 ~21:31

Continue active authorized isolated testing; do not end the task yet. Image unchanged, no pushes. .64 supervisor1520011 still active and rooted shellsession51019 available. At21:30 currentSX-mixedPV-6x8-budget. 5FPS budget19profilescomplete,8FPS started; actualselectedbudgetN=6 at8FPS (not earlierestimated4). Authoritativeledger determines floor; generatedrole-equivalence checks alreadycoveredN4/6. At8FPS S6mixedstalledallcams duringrun; SA6failed; SP6andSC6passed. No generalhardwaremaximum inferred. Mixed5FPS S/SP/SC/SX/SIF/SIB/SIFB/SIFBG/SH and mostmixedrolespassednarrowchecks; SV/SVX/SAfailed. SIFBG6x5emittedface/body but0gait, so gait-positive workloadcoverageis incomplete.

Mixedidentity25%5FPS(2identity+4scene) failedstartup/nomeasurablesteadywindow,4S camsbrieflyprocessedbeforeallunavailable; nofatalregexlines. AnalyzeroldrowhasIndexErrorlistindexoutofrange because0steady samples. ThisisNOcapacitymeasurement. Subsequentmixedidentity50%(3identity) and75%(actually4/6identity duePythonround) PASSED, balancedalsoPASSED. Thusstartupfailureis nonmonotonic and cannotjustifyclassifyingallmixedrolesunsupported orclaimingcamera2limit. Reportsneedactualintegerrolecounts, notnominalpercent. DeviceFDaudit21:08:38 showedONLYcurrenttestcontainerAPI/workerusingGPU/NPU; savedinventory/isolation-device-clients.jsonl. EssentialOSservicespreserved.

Importanttest-harnessgapfound: oldmarker_sender discardedFFmpegstderr andsilentlycaughtBrokenPipe; somefailedall-source casescannotyetattributeproducerEOFvsCVclose. Mainimage logsshowdecoder/timeoutswithoutnativerooterror. OldfailedS6x8PESlastindices3021–3168,sourceendedbefore120sfileloop, noevidenceofloopfailure; do notassertanencoderloopbug. OldMidentity25sameambiguity. Thesearefailedoperationalobservationswithunresolvedcause, NOTphysicalcapacitylimits.

Test-onlymarker_sender.py instrumented forFUTUREprocesses(~21:26): per-portFFmpegstderrfile andsender-status.json recordterminalreason,signals,exit/lastsendmonotonic,bytessent,PESlastindex,FFmpegcodeBEFOREandAFTERcleanup. Preservesexactencodedbytes,PESindexlogic andffmpegargs; stderrnowcapturedinsteadDEVNULL. LocalTCPdisconnectselfcheck passed:BrokenPipe,FFmpegwasstillrunningBEFOREcleanup, thenforcedkillwhileflushingclosedpipe. Do notmisinterpretaftercleanup-9asupstreamfailure. Selfcheckroot.publish/remote64-tlsfix2/sender-diagnostic-selfcheck/. Futurepersistentpublisherhelperalso logsconnections,FFmpegstart/exitandstderrtoexistingtestsource-log,adds-nostdin; nobakedruntimechanges. NoactiveCVtestwasinterrupted.

FutureSOAKphase test-prelude is now finiteandexplicit: (1)120sM-identity-25sixcam5FPS persistentpublishercontrol; (2)120sS-onlysixcam8FPS persistentpublishercontrol. Bothunmarkeddiagnostics(noindependentlatencyqualification) toseparateclosedone-connectionfixturefromnativeinitialization/recovery. (3)ReplayORIGINALuserCamera12H2641920x1080,25sourceFPS120srecording at16logicalcams8FPS for90s,newimageandcachedoldOpt79; recordquietvsinference duty, nevercallthisbusy16camqualification. OriginalsourcecopiedtoR/fixtures/Camera12_192.168.1.13_2min_h264.mp4;SHA25681a7d110b2cb66fcab9f76f3681c417a5fa66dce35edff70a974752b329c704e. (4)RefinedescendingneighborSA mixed-PVcountsfor5/8/10FPS, reusingexactalreadyexecutednewimage/samefixturepoints; excludesoldcontrol/trace. (5)600secpre-soak onalternatingpeople/vehicles then3600secstressONLYifonepre-soakpasses,descendingcountifneeded. Allcamerasscene2s. Hazardpositives/clearareseparatenativefixture. Currentbudgetcontrollerinterpreterloadedearlier, futureSOAKloadsmodifiedtestcontrollerfileonly; NOimage/policy/schema/gatepatch.

Futureassessnowrequires≥99%scene-markercoverage, andeveryten-minuteblockp95≤2s/p99≤5sand≥99%alignment. Existingpreviouspassingrowmarkercoveragewaschecked:all≥99%. Oldrowswillnotmagicallygainnewfields; finalpostreviewneedspreservescope.

Localanalysis:offloadforegroundsession21309stillactive. Onlycompletedownbudget/pre/hourgalleriesareSHA256verifiedlocallyandremotebeforeremotegalleryrelease,receiptatbothlocations; allotherremoterawdatastays. LocalpostprocesswatcherNOWsession37569(replaced72589/26536), noCVtesthostCPUused. Itproducesresource-and-time-summary,calibratedsource-progress-age,profile-workload-coverage. Missingpositivepathsarenotpipelineaccuracyfailures; nopoorqualityvectorsforcedjusttomakecoverage. No-steadycasesgetnot_measurableJSONinsteadinfiniteIndexErrorretry; originalanalysis-errorfilesretained.

report_snapshot.py generatesreport,capacityCSVwithper-rowIMAGE,eventcounts,actualrolecountsandpositivecoverage; stageCSVhasweightedaveragesandexplicitoverlap/non-utilizationlimit. Refreshasneeded. LargeCSV/reportareONGOING; noall282passclaim. AdditionalresourceCSV/finalcovered-casecatalogmappingandfullcleanupverificationstillpending. Remotephaseexitcode0isrunnercompletion,NOTallscenariospass. Finalpowerrestorationrequiresactualcomparison,notjustexistenceofrestored-power.json. Blackboxleftstoppedasauthorizedwithrestartmetadata.


### Remote campaign checkpoint7 — REMOTE CONTACT LOST after UTC2026-11-05 21:31

User task remains incomplete: finish isolated tests on.64. Last verified snapshot21:31:27UTC:87recorded native runs/attempts,23/57fixed-budget profiles completed, currentSX-mixedPV-6x8-budget. Around21:32 newSSH reads blocked thenreturnedNoRouteToHost. Ping.64unanswered,ARPINCOMPLETE; localenp3s0UP/LOWER_UP192.168.1.92/24andgateway192.168.1.1responds~.25ms. mDNSapexFabric-box.locallookuptimedout. No matchingknownalternateTailscaleaddressfound. Causeunknown: doNOTattributehostnetworkloss,poweroff,kernelpanic orchangedIPtoimagewithoutverification. No localnetworkconfigmodified. Userasyncquestionrequestedwhether.64ispoweredon/connected; pending.

Remoteindependentremaining_campaignsupervisorMAYcontinueifhostalive. Itwasplannedtofinishremainingbudget,thenpersistentcontrols/originalquiet16camcomparison/mixedSAneighborrefinement/pre/hourstress,finallyownedcleanup/powerrestore. ItsprogressandcleanupCANNOTbeverifiednow. DoNOTrestartsupervisorblindly: startupguardexpectscompletedcapacityphase,andwouldstopactivework/restorepower. DoNOTclaimpowerrestoredjustbecausecleanupcodeexists. Actualoriginalprofileinremoteinventory/original-power.json;currentlastknownnoTurbo45/65W. Blackboxremainedstoppedasrequested. SSHrootPTY51019maystillrepresentstaleTCPconnection;lastcompletedrootcommandwasdeviceFDaudit, noqueuedmutations.

Localreport_snapshot.pynowaccepts--offline,explicitlymarksreportbasedonlastverifiedsnapshotandcontactlost. OfflinegenerationdoesNOToverwritecachedremoteJSONfilemtimes. GeneratesREMOTE64_TLSFIX2_VALIDATION_REPORT.md,REMOTE64_CAPACITY_RESULTS.csv,REMOTE64_STAGE_RESULTS.csv,REMOTE64_RESOURCE_RESULTS.csv. Sourceimagecolumnincludedforoldcontrols. ResourceCSVhasCPU/RAM/temp,nodeGPUactive(notcompute),NPUbusy,rawdecode/replayprogress;shortmemoryslopesarenotleakproof. GPUmonitorstartanchorcapturewasattemptedjustasconnectionfailed:doNOTassumeinventory/gpu-monitor-start.jsonexists. RecoveranchorfromlivePID1452105create_timeifstillalive,onreconnect,beforecleanup;otherwiseearliernotes/rootmonitorstarttimelineprovideapproxalignmentonly.

PIPELINE_TEST_LEDGER.csvupdatedconservativelyforcurrentrunwithrun_evidence/execution_scopecolumns;previouscoveragecolumnspreserved. Counts:152PARTIAL,10PASS,108NOT_RUN,10FAIL,2BLOCKED. Componentpassesnotfullnativequalification. Native/APIpassesnarrowboundaryonly;noneisproductioncapacityapproval. Somecatalogscenariosremainwhollyunexecuted. CoverageinterpretationinREMOTE64_TEST_COVERAGE.md. Noall282passedclaim. RES09/10BLOCKEDmeansverificationblockedaftercontactloss;remoterunnercouldstillcontinueunseen. Knownfailedsource/time/redaction/refused-feature/metricsstorage/devicediagnosticrowsstayFAIL.

Localhelpersoffloadsession21309andpostprocesssession37569wereleftabletoretry/continueexistingwork. OffloadcopiesONLYcompletedcurrent-campaignbudget/pre/hourgalleriesafterSHA256verification,thenreleasesonlyremotecopy;allfilespreservedcompute-aandreceiptatbothends. Lostnetworkmeansfutureoffloadblocked;remotediskguardminimum4GiBbeforecases/3GiBruntimecanstopcampaignandcleanupifsafeheadroomrunsout. DoNOTremoveunverifiedremoteimagesorrestartemptygalleryaudits. Allotherremoterawdataremain. LocalmirrorsupportsatleastSC-mixedPV-6x8-budget;newSXrununverified.

Nextwhenreachable:readbootID/uptime,journalkernel/network/poweraround21:31–21:34(ifreboot,previousbootjournal),STATUS,CURRENT,run-ledger,remaining-phase-results,controller-error,restored-power,actualpowerfiles,ownedcontainers/processesanddiskheadroom. DoNOTcalllossimage-causedwithoutlogs. Ifremotecontrollerstillrunningdonotstartcompetingtest. Ifreboot/deadcontroller,archiveincompletecasewithinterruptionreason,newattemptIDs/preservecaseevidence;reapplyrecordedcontrolledpoweronlyfornewtests,andresumeappropriatephasewithoutoverwritingoldruns. Compareactualrestorationvaluesandownedprocesscleanupbeforefinalclaim. Oncecompletearchive/reviewallresults,refreshfullreport/coverageandprovideclear5/8/10FPSconditionalcapacitywithsceneeverycamera. Noimage/model/runtime/schema/policychangesorGitpushauthorizedbythistesttask.


### Recovery checkpoint8 — UTC2026-11-06T04:56:15.220019+00:00

User said“now check”;.64initialSSHNoRoute,thenpingrespondedandSSHreturnedabout04:09UTC. HostapexFabric-boxhadjustbooted(09:37IST),newbootID8acd2889-9788-4607-ad66-1374810e247d. UserexplicitlyconfirmedherestartedbecauseHOSTFROZE. Treatthisasahostfreezeduringtest,causenotproved,notjustLANoutage. LastoriginalSTATUSstillSX-mixedPV-6x8-budget,87runrecords,23budgetruns. Nosupervisoractive;NOload/inferencetestsrestarted. Oldincompletecasepreserved,noPASSclaim,nogates/contracts/model/imagecodechangeornopush.

NewrootSSHPTY60499(sudoauthorizedusingexistingusercredentials;noecho). VarsR=/home/admin1/Documents/sentry-validation-v8-tlsfix2-20261105,I=publishedb5a5digest,R2=/home/admin1/Documents/sentry-validation-v8-tlsfix2-recovery-20261106. OriginalpowerreadbackAFTERrebootmatchespretestexactly:no_turbo0,PL145W,PL2115W,PL3205W. OldsupervisorfinallydidNOTrun;doNOTmislabelthisitscleanup. ActualCPUnameUltra9 285H,16logical/affinityCPU,kernel7.0.0-30-generic. Dockerimagebydigeststillpresentverified.

Postmortem:DockerstoppedcontainerPERSISTEDacrossboot!e38616288792,sentinel-v8-capacity-6-1654387.Exit255/FinishedAt04:08UTCaredaemonrecordsafterreboot,NOTproofappcrashcausedfreeze.OOMKilledfalse.Preserved88Dockerloglinesending21:32:05UTCnormalmonitorstats.NewR2filesinterrupted-container.log/state/mounts copiedlocalrecovery/.

PartialSXcasecopiedregularSSHtoE/SX-mixedPV-6x8-budget/.resource-monitor.jsonl.gzdamagedtail;originaluntouched. Bytewisezlibsalvagerecovered100validrecordsuntil21:32:09.059UTC;savedresource-monitor-recovered-prefix.jsonl+interrupted-monitor-recovery.json. LastsampleCPUpackage67C,maximum77Cacrossprefix;container2026.95MiB,hostfree27.12GiB,containerCPU3.297/16cores,wholehost23%;GPU84.14isGTactiveNOTcompute;NPU0. Countersstillmovingall6cams,pending0–1. Thesearesurvivinglastsamples,notexactfreezetimeandnotproofeverylatertransientruledout.

ReviewedpreviousbootjournalwithEXPLICITUTCinputs('--utc'outputaloneDOESNOTchangetime-inputtimezone). Noorderlyshutdown/suspend/panicrecordexplains21:32freeze;pstoreempty.KernelhasEARLIERGPUbootwarningGuCrecommended70.53vsloaded70.36,NPUIPCpowerstate-110coldboot05:46/07:16/08:53,GPufencetimeout09:47,wakerefworkqueuewarnings.CausalrelationshiptolaterfreezeUNPROVED.NoOS/firmware/driverupdateperformed.Filteredlogs/localpostboothostreadunderE/recovery/.

RecoveredCRITICALrawscratchoutsideR: /home/admin1/.sv8-tls2/sentry-v8-capacity-6.ovp6vg2c,~80MB. CreatedseparateR2manifest778regularfilesandscratchTARoutsideR,readonlysourcepreserved.CopiedlocalE/recovery/interrupted-scratch.tar+manifest+SHA.VerifiedALL778filesandarchiveSHA699b792d67040f83c4372c36a142153225582c5e0d64672eb99954f1522b9eba.Nativerunningcasehad2,784preservedvalidaccepted-package rows,1partialtrailingline,287scene,687presence,303count,765tracklost,214recovered,446unknown,58linecross,24camerahealth.LastpreservedAPIaccept21:32:14.829UTC.Post-freezefileintegrityisNOTreal-agentdurabilityproof;unflushedwritesunrecoverable.Mockagentnotfsynceddurabilityqualified.

RestRfullarchive:DONEonremote(notalllocalyet).Rtree5.2G,manifest34557regularfiles5360910286bytes.TAR=/home/admin1/Documents/sentry-validation-v8-tlsfix2-20261105.recovery.tar5401323520bytes,SHA b43a986c385a6937c16aad666634c53cd85d6435c5ceee88a3d0be4edda3b0d5.Owneradmin1mode600,sync-fcompleted;checksumsidecarpresent.RootRoriginalfilesuntouched(notchmod/chowned). ArchivesoutsideRavoidsrecursion. Capturemanifestrecordspre-copyoriginalUID/modes.

IMPORTANTcurrentremoteEthernetenp2s0negotiated10Mbps,localenp3s01000Mbps.Measuredcopy~1.12MiB/s.Priorlinkatthetimeoffreezenotrecorded;don'tclaimslowLANcausedfreeze;replaywaslocalhost. Userasynccablequestionasked;userreplied“forwhatdoes5GB”.Answergiven:rawtestbackup(mainlyimages+logs),notnewimage.Sizesfrommanifest:3984MiBsavedimage/vectorparts,878.6MiBlogs/telemetry/reports/config,103.2MiBreplayvideos,145.4MiBtestdeps,1.3MiBscripts/schemas. Summary87runsalreadylocal;fullrawstillcopying. DoNOTstopauthorizedcopyjustbecauseuseraskedwhatitis.

RecoverytransferchangedONCEfromnonresumabletar-over-SSHtoownedremoteTAR+resumablersyncafter10Mbpsconfirmed.First400130048bytepartialprefixMATCHEDremoteTARandwasreused.Purelocalhelperrecover_archive.py(noauthsecretsinscript)verifieswholeTARthenall34557manifestfiles,recordsfull-evidence-verification.json. Onlythenitremovesitsownredundantremote5GBarchiveviaexactpath/hashcheck,leavingoriginalRsourceuntouched.Thisfreesremotetemporaryspace;current/homefree~4.2GiB(99%used)becauseTARduplicate,nonewloadtestsrunning. ScratchTAR80MBremoteleftpreserved.

ToSURVIVEfinalchat/execsessionexit,copyismovedtoONE-TIMEUSERsystemdunitONLOCALcompute-a:sentry-evidence-recovery-20261106.service,MainPID1469812lastseenactive.TypeexecRuntimeMaxSec6h,collect,norecurringtimer.Wrapperretriesnetworkfailures;per-fileresume--append-verify.CopystatusinE/recovery/resumable-archive-status.json;streamoldarchive-status.jsondescribesSUPERSEDEDfailedstream,don'tmistakeforcurrentcopy. Info/background-copy.jsonhasunit/start/stopmetadata. Stopcommandifuserasks:systemctl--userstopunit. Globaloriginaloffloader/postprocess21309/37569DIEDwhenpreviousturnended;notrunningnow. RootPTYold51019stale,new60499. Unifiedlocalcopy58767wasdeliberatelyterminatedonlyitsownsubtreewhenhandedtosystemd;newunitisactualowner.Currentfile~1.3GiBof5.0GiB,checkactualprogressratherthanthisnote.Beforefuturetestsensureverifiedcopythenremoteduplicatecleanup/disksheadroom.

NEWBOOTISNOTCURRENTLYISOLATED:rootdeviceFDscanfoundauto-startedadmin1USERsentry-cv.service(active/enabled,Restarton-failure,Fragment/home/admin1/.config/systemd/user/sentry-cv.service,cwd/home/admin1/sentry_cv),MainPID1608,PythonGPU/NPUclients1654/1655/2507/2686/2902. ItisaseparateworkloadfromrootDockerbenchmarkcontainers. K3s/containerd/dockerallactive. Noinferencetestsstarted,sohaveNOTstoppedthisnewautostartworkloadduringread-onlyrecovery. Beforeanynewisolatedbenchmarkrecorditsrestartmetadataandapplyexistinguserisolationauthorizationcarefully;preserveOS/network/SSHservices. Lastpre-freezedeviceaudit21:08showedonlybenchmarkCVclients;don'tretroactivelyinvalidateoldisolatedrunsfromcurrentbootclients.

SavedREMOTE64_HOST_FREEZE_REVIEW.md,current-state.json,andrecoveryupdatesappendedtostatus/mainreport. Existingmainreportold“contactlost”paragraphishistoric;newfreeze reviewauthoritativecurrentread-onlycheckpoint. Noexhaustive282PASSclaim. Remaining34budgetcomparisonsandcontrols/refinement/pre/hourstressarenotcomplete. Nextusefocusedexternallycapturedkernel/resource/driverdiagnosticbeforefullsweep;avoidfabricatingfirmwarerootcauseorhidinghostfreezeasordinaryFPSlimit.


### Recovery completion — 2026-11-06

The full 5,401,323,520-byte evidence archive completed copying and verification at 05:40:58 UTC (11:10:58 IST). All 34,557 manifest files passed, with zero failures. SHA-256: `b43a986c385a6937c16aad666634c53cd85d6435c5ceee88a3d0be4edda3b0d5`. The separate interrupted capture had already passed all 778 file checks. These checks verify surviving post-freeze bytes; damaged or unflushed original records remain explicitly incomplete.

The one-time local copy service finished successfully and is inactive. Only the redundant temporary remote TAR was removed after verification. The original remote campaign tree and local verified archive remain. The original image, models, gates and contract were unchanged.

Data recovery is complete. The remaining inference tests are still incomplete following the confirmed host freeze; no full test sweep was restarted. Before new isolated tests, account for the separate `sentry-cv.service` workload that auto-started after reboot.

Local verification: `optimization_evidence/2026-11-05-remote64-tlsfix2/recovery/full-evidence-verification.json`. Archive: `remote-root-after-freeze.tar` in the same directory.


### Deferred isolated testing and separate Management incident — 2026-11-06T06:09:24.075654+00:00

The user explicitly said to return to the `.64` work later. Defer further isolated-host tests and freeze reproduction until the user resumes that work. Data recovery is complete: the5.40GB archive and all34,557manifest files verified with0failures; the778-file interrupted capture separately verified. Original evidence remains on`.64`; only the redundant temporary recovery TAR was removed. The completed-run ledger has87records and23budget runs. The remaining workload comparisons and sustained tests are incomplete. No runtime/model/production-policy/contract changes or pushes were made. Preserve the separate auto-started `sentry-cv.service` discovery and recheck isolation before any future benchmark.

The new user request concerns a DIFFERENT, Management-reported BLR deployment incident. The supplied report says BLR cameras04/05/08/12 last produced runtime frames around11:16IST, then repeatedly logged `surface decoder timeout` and reconnected for over12minutes. Fresh frame captures reportedly succeeded from inside that same container. Cam15 reportedly returned401Unauthorized. An empty delivery queue was being confused with current camera health. These live BLR observations were supplied by Management; they have not been independently reproduced here, and the exact deployed tag/digest was not supplied in this message. Do not conflate this with the`.64`host freeze or claim a shared root cause.

Read-only code inspection of the locally cached published TLSfix2 image was completed without networking, device mounts or model execution. Image: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-tlsfix2`. Baked `surface_stream.py`SHA256:8559c827e774e0ecf0a8a551b9d27c76e0cb32976b14a467122a4b5f9af7738b. Saved excerpt: `optimization_evidence/2026-11-05-remote64-tlsfix2/recovery/published-runtime-timeout-inspection.txt`. First rootlessPodman read attempt failed due missing hostslirp4netns; rerunning with`--network=none` succeeded. That initial inspection setup failure is not an image decoder defect.

In the inspected build the final`vasink`is after primary`gvadetect`, native`gvatrack`and optional face cascade. `pull_selected_surface`waits on that sink, checks progress via`dls_gate.decoded_frames`, checks the GStreamer error bus/EOS and otherwise raises`surface decoder timeout`(aroundlines1303–1355). It explicitly allows quiet motion-gated buffers while decode progress continues. Therefore the error is not a precise stage attribution: source/decode failure, blocked native inference/tracking/face output, shared GPU/VA/model state or downstream backpressure can eventually stop forward progress and appear as the same timeout. A fresh independent frame grab proves availability for that client/path at that moment; it does not prove the existing native graph/VAAPI/inference or its reconnect state is healthy.

Interpretation:04/05/08/12 show a runtime forward-progress/recovery failure under the reported conditions. Precise underlying cause remains unconfirmed. Four cameras stopping together makes shared-resource/graph failure worth checking, not proven. Restarting CV may temporarily clear stale state, but is not a demonstrated permanent fix.15has a separate authentication rejection; check its actual credentials/account/auth configuration. Queue occupancy alone proves neither fresh camera frames nor remote acceptance: CV-to-agent handoff and agent-to-server delivery are separate V8 stages; an empty queue can also result from no new production. Do not force every source outage to fail shared readiness or weaken probes to hide the issue.

No BLR runtime restart, remote message, live camera access, image fix or schema change was performed. If the user requests repair, establish the actual deployed digest, sanitized native bus/RTSP errors and per-stage input/admission/completion counters around the first stall; then reproduce and test recovery. Avoid assuming an OCR/face gate, TLS error, VA decoder fault or GPU hang without evidence.


## November 6: published-code diagnosis and narrow reproduction

Inspected TLSfix2 image config ID `dbaeacc92973fc5d1e41521e3200cd64151aaa53a472404a79f87833bade3ef5` without network or accelerator access. This is not confirmation of the actual BLR deployed digest.

The published admission gate waits without a timeout when all reservations are occupied. Non-TCP native recovery calls synchronous GStreamer branch restart before resetting this gate. Branch restart calls `set_state(READY)` before its three-second `get_state` wait; the latter timeout does not bound a blocked `set_state` call. The runtime also sets retrying/reconnect counters before teardown completes, so these values do not prove another source connection happened. A restart waiting there leaves the shared coordinator's `_starting` flag set and can delay other branch restart/start operations; it does not by itself prove all peer streaming stops.

A CPU-only synthetic GStreamer test inside this exact image used the extracted, unchanged `DlsMotionPolicyGate._reserve` implementation and a one-slot gate with `videotestsrc -> identity -> fakesink`. The second buffer waited at full capacity. `set_state(READY)` did not return within one second while that callback was blocked; closing the gate released it and teardown completed. In the comparison, closing the gate before teardown allowed completion within one second. This establishes a reproducible recovery-ordering hazard. It does NOT establish that BLR hit it, that a particular inference model caused the initial stall, or that closing/reopening alone is a complete production fix. A future repair must fence old generations/results and safely reinitialize admission for the new session.

The existing diagnostic switch `SENTINEL_DLS_TRACE_PROGRESS=1` emits structured `native_pipeline_progress` stage counters/ages approximately every two seconds. It must be enabled when the native graph is constructed. Capture the currently stalled process state and exact digest before any rollout/restart that clears evidence. Compare stage progress at decoder, detector input/output, optional face stages, sink and camera consumption; trace probes are installed only for named elements present in that graph. Missing trace stages and intentional motion drops must not be interpreted as failures. Correlate last stage progress with reconnect-start and actual restart-completion logs. Cam15's reported 401 remains a separate authentication failure.

Artifacts: `optimization_evidence/2026-11-05-remote64-tlsfix2/recovery/published-admission-deadlock-reproduction.json`, extracted class `blr-admission-published_gate.py`, reproduction `blr-admission-reproduce.py`, and published-source inspection text files. Reproduction expects the class at `/diagnostic/published_gate.py` (mount/rename accordingly). No production runtime, contract, models, gates, deployment configuration or image was changed; no build/push, live BLR restart or .64 test was performed.


## Management follow-up: independent native hardware decode succeeds

User supplied further Management evidence from zmd-pilot-1: no competing CV workload found; bangalore-office-4 was the only active Kubernetes CV deployment; sampled host CPU about 2%, memory about 17%, no reported Kubernetes memory/disk/PID pressure and no GPU hangs/resets/OOM in the checked kernel-log interval. Other control/database/desktop services were present without demonstrated causation. Independently inside the production container, cameras04/05/08/12 reportedly delivered frames; cam04 decoded30 frames using Intel VAAPI and separately30 through a standalone GStreamer hardware graph. Production still reported surface decoder timeouts. These are Management observations, not locally rerun results, and the exact deployed digest remains unconfirmed here.

This makes basic camera reachability, generic TLS availability and generic hardware-decoder failure poor explanations for the whole incident at those check times. It does not prove that a shared inference component, tracker, callback, surface lifetime or recovery state is the original failing stage. Low utilization can be a consequence of blocked work, and a clean checked kernel interval does not exclude user-space/native deadlock.

Confirmed against inspected TLSfix2 code: restart_branch success means sync_state_with_parent accepted the state change, not resumed frame delivery. poll_worker readiness requires a live process, a plan and a historical camera initialization ready-file; it does not require recent pipeline progress. Thus Ready and retrying can coexist with no usable outputs. This explains status semantics rather than proving the live failure's precise location.

Recommended repair sequence, not yet implemented/authorized by this report alone: capture stalled-state evidence and deployed digest; enable existing stage-progress tracing for subsequent graph construction; cancel/quiesce blocked admissions BEFORE synchronous native teardown and fence old-session results; judge recovery by actual fresh forward progress rather than state-transition acceptance; escalate repeated failed local recovery to bounded supervised rebuild of the affected shared worker/graph, with backoff and restart-loop protection. A native thread stuck in set_state cannot reliably be rescued by another same-thread timeout; supervision must remain outside that blocked worker. Preserve per-camera outage isolation, agent-acknowledged packages and timestamp/session semantics. Do not make every single source outage fail shared readiness. Cam15 authentication401 is separate.

No deployment restart, code/policy/schema modification, image build/push or .64 test was performed in this follow-up. Main task remains understanding/diagnosing BLR; .64 campaign deferred.


## November 6: complete log analysis and reproduced media-branch recovery faults

The user requested proper cause identification, confirmed Management used the latest image, supplied detailed excerpts, then supplied `/home/admin1/Downloads/blr-cv-1110-1120-IST.sanitized.log`. Its SHA-256 is `2783684eea00f2a562215d12c9ecfb2eeb3125cf2deff9c93d3a5160d9959e35`. The file has839 physical lines including export metadata and835 exported runtime lines. It contains no `native_pipeline_progress` stage traces. Archived original, parsed records and timing summary under `optimization_evidence/2026-11-06-blr-stall/`.

Read-only SSH to admin1@zmd-pilot-1 succeeds; DNS resolves100.98.36.118, host reports sentry-apexflo-box, LAN192.168.1.19. This is the node with the bangalore-office-4 CV cgroup and logs, not the deferred .64 test machine. User kubeconfig points to an inactive localhost6443; agent proxy6444 presents a different CA from that old config. Did not bypass cluster TLS validation. Noninteractive sudo and Docker/CRI log access are restricted. No passwords were guessed or reused. A redacting read-only collector was staged at `/tmp/sentry-collect-stall.py` for an administrator, but was not executed with privilege. User supplied logs instead. Do not misreport this as complete production thread-stack/CRI inspection.

The existing CV HTTP service at pod10.42.2.38:8080 was accessible from the node. Three read-only `/healthz` and `/readyz` samples at approximately06:32:55–06:33:01UTC showed all04/05/08/12 with decoded/inference FPS0, increasing last-frame ages and retry counters around252–255; Ready remainedtrue. The worker is a shared process (namespacePID52, hostPID1705460); reported RSS roughly2067MiB. The log's monitor RSS225MB belongs to a different parent processPID9, not the shared inference worker; do not interpret it as full CV/container memory. Health counters are persisted snapshots updated by a reporting thread, so use counter deltas and not only their existence. Increasing reconnect counts and84 successful-return logs RULE OUT a permanent one-call admission/teardown deadlock as a sufficient explanation of this observed retry loop. The previously reproduced gate deadlock remains a separate real hazard, not the established live cause.

### Production sequence

| Camera | Last reliable frame (IST, from later health) | First timeout (IST) | First restart success return (IST) | Success logs in supplied window |
|---|---|---|---|---:|
|08|11:15:44.788|11:15:55.286|11:16:02.011|22|
|12|11:15:54.317|11:16:04.448|11:16:04.570|22|
|05|11:16:08.860|11:16:18.878|11:16:19.335|20|
|04|11:16:11.538|11:16:21.575|11:16:23.006|20|

Total84 branch restart success logs, with the later live health still showing no fresh frames. There are two concrete native RTSP errors omitted from the initial excerpts: logline345 at11:16:46.261 for08 and line361 at11:16:52.619 for05 report `gst_rtsp_src_receive_response: Could not receive message. (System error)`. These happen AFTER initial timeouts and may belong to unsuccessful reconnection; they do not prove the original trigger was a network fault. They also mean this file does not justify an exclusive inference-hang diagnosis. HTTP `ConnectionResetError` is in a request-handler response write and does not establish a camera-pipeline cause. Cam15's repeated401 occurs in `_probe_source` before camera graph construction; its authentication failure remains separate.

### Local reproduction using the published image

Used immutable local config ID `dbaeacc92973fc5d1e41521e3200cd64151aaa53a472404a79f87833bade3ef5`, exact extracted published `multi_camera_gst.py` coordinator and the baked primary QAFT model. Tests on local compute-a are functional lifecycle tests, not isolated capacity measurements or proof of the original BLR trigger. No production deployment or policies were modified.

Nine bounded native lifecycle variants were recorded. In all, a and b produced frames before a's branch restart; a restart returnedtrue but no NEW measured stage progress followed, while b continued. Some appsinks returned one previously queued sample; unchanged input/output counters show that this is not resumed processing. Counts/settings are consolidated in `native-restart-comparison.json`; preserve original console logs. Early JSON/console files inherited a generic synthetic-scope text string from the initial helper; use each case's explicitly recorded configuration and this comparison for correct source/device scope.

The decisive live RTSPS cases used the user-provided `traffic1` source through rtspsrc, H264 depay/parser, VA hardware decoder, vapostproc and source-PTS selection. Both with GPU primary detection and with it replaced by an identity control, pre-restart output was approximately10frames for a and8for b in the sampled window. After the exact published `restart_branch()` call, a produced0 new frames and b16–17. The native source reported `streaming stopped, reason not-linked (-1)`.

The named-pad inspection makes the mechanism concrete: before restart, a's RTP source pad was linked to depay. After cycling the SAME bin throughREADY and syncing back to its PLAYING parent, a's new RTP pad was present but `linked=false`, its peer wasnull, and depay's sink was unlinked. Peer b stayed linked. The branch relies on parse-time delayed linking and has no persistent reconnect pad-added wiring. Current official GStreamer parser source explicitly disconnects a normal delayed-link handler after the first successful one-pad link (archived `upstream/gstreamer-grammar.y.in`, `gst_parse_found_pad`; this is explanatory upstream evidence, not a hash match of the installed parser source). Therefore reusing the bin is not equivalent to building a fresh graph.

A diagnostic-only persistent pad-added callback then linked the new RTP pad successfully (`GST_PAD_LINK_OK`). That removed the not-linked condition, but output still did not resume: this retained native VA chain failed with `not-negotiated (-4)`. Independent synthetic VA upload and hardware encode/decode controls also fail renegotiation after the sameREADY cycle, including without any GVA detector. A debug run directly records `postproc FAILED to configure incaps ... outcaps ... VAMemory`. This proves the recovery path has native media-link and renegotiation problems that do not require inference to be present; it does NOT identify the exact VA driver/plugin internal cause, nor prove the production first stall occurred there. Do not claim that adding one pad callback alone is a finished repair, or that the detector is universally responsible.

### Other confirmed code defects and scope

Executing the exact published RuntimeError handler AST with a successful branch-restart double confirms it closes the primary BGR converter and sets it toNone while retaining pipeline. The next loop skips initialization because pipeline is notNone. A resumed pixel-requiring frame would therefore encounter an absent converter. This is a confirmed recovery-state bug, not an exception observed for04/05/08/12 in the supplied log. Admission records are reset after the native branch is resumed, which also requires careful old-generation fencing in any eventual repair; do not treat this source-order audit as a proven live race.

The exact published admission gate + coordinator CPU-only reproduction also confirms a full gate can block a's restart and delay b's restart via the shared `_starting` flag while b still streams. Releasing the gate lets both restarts complete. Again, production's repeated successful returns distinguish its current symptom from that permanent-block case.

### Current conclusion / remaining work

The sustained outage is accompanied by a demonstrably inadequate media-branch recovery implementation: reuse of native state with lost dynamic links/failed renegotiation, acceptance of state-transition success without fresh frame progress, no effective escalation despite repeated failure, and historical initialization-based readiness. Those faults are concrete and reproducible. The initial event that stopped BLR08 at11:15:44 and then the other cameras is NOT captured at stage level in this log. Do not invent a GPU hang, faulty model, TLS rejection or external network outage as that first cause.

Any repair should preserve V8 and production gates, quiesce/fence admitted work before teardown, correctly recreate/reconnect source and VA resources, recreate dependent converters, verify new-generation fresh-frame progress, and use outside-worker supervision for bounded escalation when native recovery fails. Persistent relinking alone failed the local positive attempt; a complete rebuilt branch/worker solution still needs implementation and validation. Capture/enabling of stage traces on a controlled subsequent startup remains necessary to identify a recurring INITIAL trigger. No runtime fixes, rebuild, push, production restart, source credential change, .64 load test or contract/policy/model modification was made. All owned local diagnostic containers were removed at completion.


## November 6: reconnect repair implemented, built and qualified locally

User explicitly instructed "then do it", then "continue fixing and also keep proper logs". Implemented recovery and diagnostic changes in branch`sentry_cv_multi`, preserving earlier unrelated documentation changes. No remote production restart, package publication or .64 workload was performed. All local diagnostic containers/test agents were stopped and removed by their harnesses.

### Frozen build and change boundary

Final local image: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.06-v8-qaft-int8-reconnect2`.
Config/image ID: `e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7`.
Local manifest digest: `sha256:f82000d80084517537a5b014e8228f48815e47962f0ff9fd31002acb448e4718`. This is NOT a published registry digest.
Repair base config ID remains `dbaeacc92973fc5d1e41521e3200cd64151aaa53a472404a79f87833bade3ef5` (published QAFT TLSfix2 runtime). New `docker/Dockerfile.sentinel-v8-reconnect` and `scripts/build_sentinel_v8_reconnect_image.sh` layer only the repair over that frozen image; no apt/pip/model rebuild, precision/batch/device change or Opt80 promotion. Script checks the base ID and refuses to overwrite an existing output tag. Reproduction build command was `SENTINEL_IMAGE_VERSION=2026.11.06-v8-qaft-int8-reconnect2 scripts/build_sentinel_v8_reconnect_image.sh`; choose a NEW unused version for another build.

Compared73 baked model/contract/schema/policy artifacts:32 model artifacts and41 contract/schema/policy files are byte-identical between base and final image. Also compared67 runtime/config Python/configuration files: only9 expected files differ or are added. Core event producer, adaptive policy, face/body/gait selection and model-proc configuration are unchanged. V8 desired-state8.0, wire7.0 and agent1.0 package formats are unchanged. Canonical and baked contract validators pass. The repair retains the nonroot image user and existing GPU/NPU/VAAPI execution paths.

### Repair behavior

- `rtsp_source.py` permanently wires compatible video RTP pads (H264/H265), ignores audio/wrong-codec pads and refuses an ambiguous replacement. Pad link/remove events include camera/session, source generation and link result. This repairs the one-shot parse-link problem.
- `multi_camera_gst.py` now restarts only rtspsrc's network session. It does NOT READY-cycle the decoder/vapostproc/GVA graph or hold the fleet-wide startup latch during a camera's reconnect. Live prototype with shared GPU detection restored15 fresh frames while the peer produced16. Keeping the native graph warm avoids the reproduced VA renegotiation failure.
- `dlstreamer_bridge.py` adds suspend/resume fencing before reconnect. Blocked producers wake, already-admitted work stays accounted for, and a gate cannot resume over unfinished old work or afterclose. Stalled pending native admissions request supervised graph replacement rather than being silently cleared or mixed with a new epoch.
- `surface_stream.py` retains the matching converter on source-only reconnect and initializes it whenever missing or geometry changes. Native probes/admission callbacks are installed BEFORE attaching a branch to a running parent. First-frame reacquisition receives the existing35-second default allowance, versus10-second steady progress timeout. Actual fresh admitted output is required before declaring recovery. Existing V8 camera-health recovery rotates a logical stream session; decoder health, camera TLS/link diagnostics, trace and feeder session IDs now follow that same ID. Existing policy lifecycle handling is reused unchanged. Removed a bogus boolean fire-converter cleanup sentinel; actual fire evidence/mapping behavior is unchanged.
- `native_watchdog.py` and `stream_fleet_openvino.py` place the final watchdog in the separate fleet parent, using new per-worker shared signals without worker-owned multiprocessing locks. A stuck in-worker/native call cannot keep the parent falsely satisfied merely because the process is alive. Defaults: source native-call deadline45sec; heartbeat deadline60sec; active processing-phase deadline120sec. Pending model output stalled past the ordinary pull timeout requests earlier fencing/replacement. First failed recovery deadline persists across unsuccessful attempts. Normal offline sources with no live RTP input remain camera-local; repeated live input with no recovered native output escalates. The existing bounded process restart/backoff path is reused, and the delivery handoff scheduler remains outside the killed native worker. A graph replacement necessarily interrupts all cameras assigned to that graph; this is reserved for stuck native work, not ordinary source outages.
- `solution_image_entrypoint.py` makes effective readiness require initialization AND at least one current healthy/fresh camera. Current process-generation checks prevent old status from proving readiness after a worker restart. One healthy peer keeps readiness true. `/healthz` remains livenessHTTP200; `/readyz` returns503 when no current camera can process. Hot reload does not blindly force readiness true. HTTP client reset/broken-pipe writes generate safe debug diagnostics instead of misleading request-handler tracebacks.

### Log guide for the next incident

| Event | Meaning / useful fields |
|---|---|
|`native_pipeline_progress`|Default30-second bounded per-camera source/parser/decoder/VA/rate/motion/primary/tracker/face/sink/consumer counters, latestPTS and last-progress ages, plus application phase/phase age. Unseen stages havecount0/age null, not an asserted failure.|
|`native_pipeline_error`, `native_pipeline_warning`|Immediate sanitized native element/error/debug, camera/session, recovery phase and in-memory progress snapshot. No need to infer a model fault from the old generic timeout text.|
|`camera_pipeline_stalled`|On-demand pre-recovery snapshot, error/reason, attempt and outstanding admissions. Captured before resource recovery changes state.|
|`rtsp_video_pad_linked`, `rtsp_video_pad_link_failed`, `rtsp_video_pad_conflict`, `rtsp_source_pad_removed`|Dynamic source connectivity and source-generation evidence.|
|`camera_source_restart_begin`|Native source stop/start attempt begins; watchdog deadline applies.|
|`camera_source_restart_requested`|Native transition accepted/rejected; explicitly`recovery_verified=false`. This is NOT resumed video.|
|`camera_recovery_verified`, `camera_stream_recovered`|A fresh current-epoch frame was actually consumed and admitted; attempt,PTS, receive time, elapsed recovery and current session available.|
|`native_graph_rebuild_requested`, `native_graph_watchdog_fenced`|Native work cannot safely recover in place; reason, affected camera and stage evidence.|
|`native_worker_watchdog_restart`|Separate parent fences/kills the affected native graph worker; oldPID and affected cameras recorded.|
|`camera_process_restarted`|A replacement process was spawned; first-frame readiness is still checked separately.|
|`http_client_disconnected` (debug)|Health/HTTP caller left during response writing; does not itself explain a CV stall.|

All structured records retain UTC timestamp andPID. Camera/session context is explicit on native diagnostics and thread-local on camera application records. Recursive bounded sanitization covers nested dict/list metadata, secret keys, URLs, bearer tokens and embedded quoted JSON. No embedding vectors or image pixels are added to logs. `SENTINEL_DLS_TRACE_PROGRESS=1` selects2-second progress reporting; normal default30sec. Internal`SENTINEL_DLS_PROGRESS_INTERVAL_SECONDS` can set an interval; it is not a desired-state gate. Test-only5sec intervals/fault injection are NOT baked into the image. Python counter snapshots do not query a possibly blocked native state mutex. The old monitor line's RSS remains its parent-processRSS; use workerPIDs and `/healthz` process/cgroup metrics for CV memory.

### Final-image validation and evidence

All artifacts are under `optimization_evidence/2026-11-06-blr-stall/repair-validation/`, with `REPAIR_RESULTS.json`, `image-metadata.json`, build logs, source snapshot and SHA256 manifest.

- `final-reconnect/`: two live H264 RTSPS consumers at configured4FPS with all use-case bindings enabled; one behind an ephemeral untrusted TLS relay, one direct healthy peer. Interrupted the relay twice for18sec. Each round REQUIRED a new verified-recovery log and fresh counters, not merely a buffered old-connection frame. Both recovered; peer stayed healthy/Ready and kept processing; native workerPID47 was preserved. Health, recovery logs and captured packages used matching current stream-sessionIDs. API capture accepted1321 packages,102293195 evidence bytes and55 embeddings, validating V8 schemas, package digests, image bytes and observation/evidence/embedding associations. This is a capture API boundary, not a claim about real-agent fsync or remote upload.
- `native-hang-r2/`: test-only, one-shot blocked primary pad callback. Timeout logged pending admissions2; graph rebuild requested07:30:51.025UTC, separate parent fenced it07:30:51.893, replacement spawned07:30:58.250; both sources linked again07:31:03.305. WorkerPID47 became429 and both cameras resumed. API capture accepted500 packages,41155067 evidence bytes,36 embeddings; current session correlation passed. Test injection remained in a read-only test mount/PYTHONPATH only, never the image.
- `tcp-regression/`: controlled1080p Camera12 H264 recording via TCP MPEG-TS,1camera at4FPS, all bindings,30-second steady window. Healthy;17 accepted packages including9 scene/evidence/embedding packages; no captured fatal lines. It is a compatibility test, not a capacity maximum or a general performance qualification. Existing harness's`capacity_qualified` field is scoped to this tiny workload only.
- Baked final image:167 applicable runtime tests passed,1 Opt80-only gait case deselected and the separate repository-only Opt80 module uncollected because that optimization is deliberately NOT in this image.30 production-policy behavior scenarios passed against the actual baked code. Also added a real subprocess test that holds the workerGIL inside a harmless libc call: the independent parent detects its stale heartbeat and kills/reaps it. This covers the case an in-worker Python watchdog cannot rescue. Canonical and baked V8 contract validation passed. No test dependencies were installed into the image.

### Rejected/invalid fixtures retained rather than counted as image failures

- `reconnect/`: Unix socket path exceeded AF_UNIX length before CV start; moved the capture socket to a short temporary directory.
- `reconnect-r2/`: first pass predicate could accept buffered old-connection frames. Audited and marked INCONCLUSIVE for two recoveries; raw pre-audit report retained. Tightened each round to require its own new verified-recovery event. `reconnect-r3/` then passed on intermediate image1.
- `native-hang/`: test PYTHONPATH override hid gstgva before inference. Stopped only that owned fixture and preserved logs. Harness now extends the image's original PYTHONPATH; final `native-hang-r2/` passed without an image change.
- First baked runner lacked pure-Python`py` dependency and then tried repository-only Opt80 code. Corrected read-only dependency/support mounts and exclusions; final applicable baked suite passed. No hidden pip/apt mutation of the frozen runtime.
- Initial artifact inventory scanned nonexistent schema roots and contained models only. Corrected to the actual`image_schema` and`contracts` directories and asserted nonzero schema coverage before recording the final73-file parity result.

Intermediate image1ID`f4ef5a7acfb6e4ab9b8c73003fd5f6db9ecc1ea85040e291e78fa82fe07ce7bb` remains local, unpromoted. Final image2 additionally aligns operational session IDs with the existing policy's session rotation, and was retested for both interruption and native-hang recovery. Do not publish image1 by mistake.

### Remaining boundary

The confirmed recovery defects are fixed and qualified for the above cases. The initial BLR first-frame-loss trigger was not captured at stage level in the supplied old log; do not claim a particular network/GPU/model cause for it. Default native diagnostics now make the next occurrence observable. Cam15 still needs valid source authentication. HTTP/HTTPS native support, labelled accuracy, maximum camera counts, hours-long soak, the deferred .64 freeze and real-agent/Management delivery are not newly qualified or repaired by this task. No git commit/push, registry push or BLR rollout has occurred. Next authorized action, if requested, is personal-repository/image publication followed by a controlled Management rollout; ApexFlo remains explicit-only.


User requested a shorter image name. Added local tag `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:v8-r2` to the SAME tested image `e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7`. No rebuild, runtime/config/model change or registry publication. Prefer the short tag when referring to this repaired candidate; original tag retained for provenance.


Publication authorized by user "ok now push to repo". Published short image tag `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:v8-r2`, registry manifest `sha256:e8baec1ad054312f0081deb4bf6d34c7c8cc3bfb38516a3582d3d2fed32b4629`; anonymous manifest/config fetch and normal Podman pull-by-digest both verify tested config `e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7`. The earlier local manifest f820... is not this registry digest. Source publication targets only the two personal repositories on `sentry_cv_multi`; no ApexFlo writes or deployment rollout. Prior isolated-host coverage/results are included as a separate documentation commit, with CSV line endings normalized without changing parsed values. Curated recovery release assets exclude raw package observations, camera recordings/images and private TLS keys. Code/tag/release confirmation will follow after remote verification.
