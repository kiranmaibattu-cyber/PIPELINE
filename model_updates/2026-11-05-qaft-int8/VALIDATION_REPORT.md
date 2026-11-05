# QAFT INT8 image validation — 2026-11-05

**Decision: the model is baked in and the native pipeline runs, but this candidate is not production-qualified.** GPU/export-reference fidelity and the inherited timestamp defect remain unresolved. Testing did not change the image, V8 contract or production gates.

Image: `localhost/sentry-v8-qaft-int8-model-candidate:2026.11.05`. ID: `d035cb44df82a66cbcdaa29e3ca22177f7bfdbf080009d6a27bf6633e5eb2127`. Subsequently published under `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-candidate`; see [publication and exact digest](RELEASE.md). The tested image ID is unchanged.

## Scope and test conditions

22 native cases recorded, including intentional faults and excluded thermal attempts. 93 applicable runtime/policy tests passed; one Opt80-only test was deliberately deselected because this image is based on Opt79. The unpromoted Opt80 gait experiment is absent.

Intel Core Ultra 9 285H, 16 logical CPUs. Other host programs remained running. Remote192.168.1.64 was not used for tests. Early cases used the existing stock setup. Later cases cap only the test container to four CPU cores during loading/warmup (two for individual live sources), then restore all sixteen cores before steady measurement and verify the actual kernel quota. No host power/turbo/fan/service changes. Thermal guards stopped only test containers. These are conditional local results, not isolated machine capacity.

## Model and GPU findings

- Source QAFT INT8/mixed-precision checkpoint and trained quantizers were retained. Dynamic and static-one weight BIN hashes are identical; 86 FakeQuantize nodes remain. Class order, source-coordinate preprocessing/postprocessing and detector confidence gates were not changed.
- The active native graph uses GPU fixed batch one, eight requests and VA-surface sharing. Models supporting batch dimensions do not automatically make this graph a cross-camera batch scheduler.
- GPU fixed batches 1, 2, 3, 4 and 8 produced mutually consistent per-image detections within the recorded tolerances. This checks batching consistency, not agreement with the source model on another backend.
- Fully dynamic GPU execution compiled but produced zero accepted detections where fixed-one and CPU/F32 found objects. It failed parity at tested batches1/2/3/4/8. Do not enable the experimental fully dynamic GPU route.
- Offline conversion diagnostic: the default fixed-one GPU path differed from CPU/F32 using the supplied exporter settings, by up to about0.199 confidence; counts differed on two of eight frames. GPU/F32 floating precision reduced the maximum confidence discrepancy to about0.053 but still changed detections and failed the predefined fidelity checks. Both GPU configurations retained quantized execution nodes. The CPU comparison was a separate diagnostic after performance tests, never a runtime fallback. It uses image OpenVINO2026.2; the supplied exporter documented2026.0. No labelled accuracy claim is made.
- No precision hint, model recalibration, GPU/CPU fallback or rebuild was applied to the tested image. GPU fidelity needs exporter/device validation; do not loosen event gates to conceal it.

Further isolation used the exact same float input on GPU/F32 and CPU/F32 and still failed the chosen confidence/detection tolerance. CPU/F32 fixed-one versus CPU/F32 dynamic-one also differed (maximum matched confidence delta about0.069). The issue is therefore not established as GPU-only or image preprocessing. Shape/backend numerical fidelity needs investigation; no corrupt weight-file claim is justified. The source IR reshaped in memory and serialized fixed-one were already consistent on GPU.

## Capacity on the marked1080p crowd workload

All principal cases enable all use cases and scene sampling every two seconds for every camera. The table separates source-frame selection from actual primary inference. Quiet-mode policy work is accounted separately. Counter rates use each camera snapshot time, not just a final rolling FPS sample. Steady holds are90 seconds except the 600 second soak; measured first-to-last counter spans exclude one sample interval.

| Case | Selected FPS/camera | Primary inference FPS/camera | Worst scene p95 | Result |
|---|---:|---:|---:|---|
| 4x8-qaft | 8.00–8.01 | 7.99–8.02 | 0.526s | Short pass at 4×8 |
| 5x8-qaft | 7.97–8.01 | 7.78–7.88 | 1.366s | Marginal; window dips |
| 6x8-qaft | 6.87–7.12 | 6.87–7.12 | 7.328s | Fails throughput/freshness |
| 8x8-qaft | 4.90–5.32 | 4.90–5.32 | 12.615s | Fails throughput/freshness |
| 4x10-qaft | 9.96–10.01 | 9.86–9.90 | 1.762s | Marginal; window dips |
| 3x10-qaft-startupcap4 | 10.00–10.02 | 9.86–9.88 | 0.471s | Short pass at 3×10 |
| soak-2x8-qaft | 8.00–8.00 | 7.86–7.87 | 0.355s | Ten-minute pass at 2×8 |

Practical reading: four cameras at 8 FPS and three at 10 FPS passed short local tests; five at8 and four at10 were marginal despite near-target averages. Six/eight at8 clearly failed. Only two at8 were held for ten minutes. These findings do not establish a universal camera maximum, continuous24/7 rating or the earlier goal of15–20 busy cameras. Input object density, enabled analytics, motion admission and available host resources affect work.

An original three-camera startup, a fresh Opt79 four-camera control and the mixed three-live-camera attempt hit the thermal guard. They are excluded from qualification. The Opt79 abort means this campaign did not obtain a clean fresh baseline comparison, so do not attribute all throughput differences to INT8.

## Sustained resources (two cameras at 8 FPS)

Matched counter duration: 590.4seconds within the 600 second hold. Selected FPS 8.000, 8.000; inference and quiet frames are separate in results.json.
Container memory: 3263.5→3362.1MiB; last-five-minute slope 9.22MiB/min. A short run does not establish a leak-free long deployment.
Mean CPU from kernel cgroup deltas: 1.342cores. Steady throttled-period delta: 0. Maximum core temperature p95/max: 104.0/105.0°C.
Node NPU busy p50/p95: 18.04/32.522%. GPU engine p50/p95: Blitter 0.00/0.00%; Compute 22.53/28.46%; Render/3D 4.04/7.13%; Video 5.74/6.29%; VideoEnhance 7.89/9.35%

**The soak passed rates and marked freshness, but was not thermally clean:** temperature reached the critical range in intermittent bursts, although the guard did not see five consecutive critical samples. Memory also grew. These measurements do not qualify sustained production operation. See [resource plots](validation/soak-resources.png).

## Functional and delivery results

- ANPR recording:76plate reads,85explicit outcomes and40line-cross observations, with evidence. This proves paths execute, not correct plate text. Its raw FPS harness flag failed a short rolling window despite mean selection near7.92FPS.
- Fire followed by normal footage:two suspected episodes andtwo clear transitions. This is model-observation execution, not proof of a real incident or safety.
- Motion transition:647quiet and264inference frames in the captured run. Quiet/active operation was exercised. The empty-frame fixture stayed active because low-score raw smoke detections can hold inference active even below the final hazard alert gate; do not equate visual quiet with zero model detections.
- Positive zone coverage: a final central-band desired-state polygon on the ANPR recording produced13zone-entry and39zone-exit events, plus presence/episode/count records. More exits than entries is possible because subjects first observed already inside establish an unknown-entry episode; track loss is not automatically an exit. This was a configuration fixture, not a gate change or capacity test. No separate dwell-milestone event is claimed; dwell support is through zone episodes and their observed-inside timestamps.
- Twelve-second TCP camera outage:target interrupted and recovered, with its peer healthy. This tests source-outage isolation, not recovery from every native GPU crash.
- Lost acknowledgements:88duplicate acknowledgements, zero exhausted retries/protocol errors at sampled cutoff. Accepted package IDs and payload bytes were reused.
- Ten-second503fault:four packages exhausted the contract-defined six-attempt budget;99backpressure andtwo coalescing counts. The V8 contract explicitly permits reported exhaustion and memory release without claiming production. This does not mean previously accepted snapshots were lost; it does mean not all observations are guaranteed delivery while the agent is unavailable.
- /healthz200, /metrics200 and /healthy404 verified. Decode remained VAAPI/DLStreamer; primary/scene/fire/face detection used GPU, face embedding/pose/body/gait/plate used NPU, OCR was configured MULTI:GPU,NPU. CPU tracking and preprocessing remain normal work. No runtime model/decode CPU fallback was introduced.

## Live inputs

| Source | Selected FPS | Observed result |
|---|---:|---|
| RTSPS traffic1,1080p H.264 | 6.54 | Frames, events and evidence received; raw harness FAIL |
| RTSP traffic2,1080p HEVC | 7.98 | Frames, events and evidence received; raw harness FAIL |
| RTSPS ch9,1440p HEVC | 8.81 | Frames, events and evidence received; raw harness PASS |

Live sources were unmarked, so source-to-API freshness was not independently established. ch9 exceeded the nominal selection target during its short window; startup catch-up/source timing was not resolved. A later compressed-packet-only test measured approximately18 packets/s for both traffic feeds and25 for ch9, versus advertised25 FPS. This was not simultaneous with CV and cannot by itself attribute the earlier shortfall to the source or inference. The first FFmpeg progress diagnostic had no frame-count field and yielded no usable rate; the corrected FFprobe packet diagnostic is archived. ExternalTCP192.168.1.92:9001timed out during preflight; local independent TCP replays worked.

## Timestamps, artifacts and remaining restrictions

Independent frame markers measure first-video-PES submission to the agent API, excluding physical camera capture/encoding and work after the API. They show fresh images in passing marked cases. Absolute observed UTC still has the inherited mapping defect: one-camera scenes were dated about5.77seconds after receipt. Runtime zero-clamped source-age values must not be presented as correct end-to-end latency. Correct frame associations do not repair absolute timestamps.

The API boundary receiver validates schemas, multipart hashes/sizes, promised IDs and associations. It is not the production agent database and does not test downstream Management retrieval. The saved-gallery audit separately verifies JPEG dimensions/source bounds, embedding dimensions/L2 normalization and parent/frame/presence links. See qaft-only-gallery-audit.json; the all-case audit also includes the old-model control and must not be mislabeled QAFT-only.

GPU engine counters are node-wide and overlap; do not sum them or attribute all activity to this container. Their alignment is approximate. NPU busy metrics are node-scoped. Parent/child stage wall times overlap and are not additive kernel timings. Full per-case resource, GPU, source clock, output and stage data is in results.json.

Do not promote this image as fully validated. Remaining issues include GPU conversion fidelity, fully dynamic GPU execution, absolute source UTC, marginal higher camera loads, thermal restrictions and inherited admission/Secret-rotation/HTTP-native/telemetry issues already documented in ISOLATED_VALIDATION_REPORT.md. No models, event gates or contract fields were changed to improve test scores.

## Evidence

This directory contains22 case records, raw artifacts, exact invocations, health snapshots, one-second resource traces, source epochs, marker latencies, schema/policy test log, model/probe comparisons and GPU engine data. Cold-start quota verification and excluded attempts are retained. Models/image IDs are unchanged from update.json. No registry push occurred during testing. The user subsequently requested publication; see RELEASE.md for the separate publication record.


The final QAFT-only gallery audit covered **3322 JPEGs and 3104 embeddings across 3811 saved packages**, with zero audit errors. This is a saved subset; receiver checks apply at acceptance for all packages.
