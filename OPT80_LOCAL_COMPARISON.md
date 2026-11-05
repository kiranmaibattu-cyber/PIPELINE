# Opt80 local comparison — 2026-11-05

Decision: retain Opt79 as the reference and source-build default. Opt80 is an integrated experimental image with faster CPU gait preprocessing, but these comparisons do not show a higher useful camera capacity. Do not advertise it as the new optimized production release.

## Goal and scope

Preserve the Sentry V8 contract, all production gates and model profiles, motion behavior, tracking and presence state, source-coordinate crops, frame/session associations and event/evidence/embedding delivery. Change one measured internal execution cost at a time. The user explicitly requested local testing and instructed us to leave the separate benchmark on 192.168.1.64 running. No tests or host-setting changes were made there during this comparison.

Local hardware: Intel Core Ultra 9 285H, 16 logical CPUs. Other programs remained running. These are short, paired observations, not isolated maximum-capacity qualifications or a replacement for the earlier isolated-host report.

## Implementation

The candidate partitions independent MOG2 pixel state into horizontal row bands, sharing one bounded CPU executor per process. History 300, variance threshold 25, shadow detection disabled, original pixels and one update per eligible frame are preserved. Full-mask reconstruction precedes unchanged person-ROI morphology, silhouette normalization, motion checks, sequence history and emission gates. The camera still waits for this frame to complete; this is not a new asynchronous camera policy.

Below 1,500,000 pixels, or with a single available CPU or nested OpenCV parallelism, it retains the native implementation. Worker count is bounded by CPU affinity/cgroup quota and eight threads; submitted jobs are bounded to twice that count. Failures drain writers and prevent reuse of partially advanced state. Camera retirement closes shared workers after their last owner. Existing historical gait-buffer retention defects are not fixed by this change.

Changed runtime files: `parallel_background.py` (new), `person_reid.py` (factory/cleanup), `dls_secondary.py` (gait cleanup only). No event producer, decoder, tracker, model, schema or production-policy source was changed. The factory default remains one native partition; the Opt80 image explicitly enables four internally. No desired-state field was added.

## Image provenance

- Reference: `localhost/sentry-v8-opt79-shared-gait-background:latest`, local image ID `f82a0769ed12eff25ef7bdabc9619370ebfdceeed737eaed21e5f2381e4feb86`.
- Candidate: `localhost/sentry-v8-opt80-parallel-gait-background:2026.11.05`, local image ID `6b91f43077ab8c5bcaae5140cab1cb7f45a9068d17452ce9a55da3694ad213f1`.
- Candidate is an overlay of the frozen reference. Models, libraries and native plugins are inherited unchanged; there was no base/dependency rebuild.
- All embedded contract/schema hashes and event/surface producer hashes match the reference. Inventory and exact overlay build context are archived with the evidence.
- No registry publication or deployment was performed. An archive was staged on the remote host for later use, but not loaded or executed there.

## Native paired results

Each row used independent TCP MPEG-TS replay connections to the same marked 1920×1080, 30-FPS crowd fixture, all use cases enabled, and scene search configured every two seconds on every camera. Runs used 15-second warmup and 90-second steady holds; the first-to-last matched counter interval is approximately 80 seconds. Frame rates below use each camera telemetry snapshot time and cumulative inference counters, not a final rolling FPS display.

| Workload | Image | Per-camera inference FPS range | Gait background mean ms/call range | Whole camera processing mean ms/frame | CPU cores, mean | Container MiB, mean | Scene source-to-API p95 range |
|---|---|---:|---:|---:|---:|---:|---:|
| 4x8 | opt79 | 7.897–7.956 | 58.72–64.15 | 86.36 | 3.269 | 4063.6 | 0.534–0.632 s |
| 4x8 | opt80 | 7.907–7.928 | 32.11–33.44 | 82.28 | 3.637 | 4254.3 | 0.653–0.817 s |
| 5x8 | opt79 | 7.592–7.943 | 70.68–74.71 | 118.16 | 4.647 | 4662.9 | 2.770–5.936 s |
| 5x8 | opt80 | 7.594–7.872 | 33.25–36.25 | 117.64 | 4.917 | 4555.9 | 3.545–6.348 s |
| 3x10 | opt79 | 9.979–10.001 | 56.35–56.47 | 70.60 | 2.800 | 3749.8 | 0.480–0.595 s |
| 3x10 | opt80 | 9.974–9.994 | 30.41–31.67 | 65.30 | 3.041 | 3636.1 | 0.475–0.642 s |

The processing-time column is the camera-worker `pipeline.frame_processing_total` metric; it is not full source-to-agent latency and does not include every upstream native wait. Child-stage times overlap and must not be summed.

Four-camera gait update time fell roughly 47%; the weighted mean across all camera-frame processing fell only 4.7% (86.36 to 82.28 ms). Three-camera/10-FPS processing fell about 7.5% (70.60 to 65.30 ms). Five-camera processing changed by less than 1%, while both versions accumulated stale scene images. CPU use increased in all three candidate comparisons. RAM varied with workload and outputs; there is no consistent memory reduction or proof that all differences are caused by the background allocator.

Both three-camera/10-FPS runs maintained throughput and fresh scenes in this short local test. Four-camera/8-FPS cumulative rates and marked freshness were healthy on both images; the original harness nevertheless marked Opt79 failed because one final two-second rolling aggregate sample was 0.948 of target, below its 0.95 cutoff. Preserve that raw failure alongside the more informative matched-counter measurements. Both five-camera cases failed freshness, with some p95 delays above five seconds. Close-to-target average FPS must not be used to qualify those cases.

There were no captured fatal native errors in these six comparisons. Some handoff backpressure occurred. Zero protocol errors or exhausted retries at sampled endpoints does not prove every rejected admission was preserved; known admission-state handling defects from the earlier audit remain unresolved.

## Correctness and delivery checks

- 103 tests passed inside the actual candidate image. Covered exact foreground and learned-background equality across dimensions, shape changes, noncontiguous pixels, uint8/float32 and varied learning rates; shared-pool camera isolation, bounded resource guards, error draining and cleanup; existing concurrency, gait, body, cache and V8 policy scenarios.
- New end-to-end deterministic gait regression compares original and partitioned execution: identical sequence histories, normalized model inputs, emission decisions, source timestamps, presence/track associations, evidence metadata, exact crop JPEG bytes and embedding values. Generated observation/evidence IDs differ by design.
- Native smoke produced face, body, gait and scene embeddings and evidence through the compatible agent API. The receiver checked schemas, part sizes, hashes and parent associations. This is the authorized CV-to-agent API boundary, not downstream Management durability or retrieval.
- Independent markers decoded successfully for every captured scene in the six paired tests, with zero missing, ambiguous or invalid markers. Latency measures source first-video-PES submission to complete package acceptance at the agent API. It excludes physical camera capture and encoding.
- The pre-existing source-PTS UTC mapping defect remains unresolved. Runtime zero-clamped source-age metrics were not used to claim correct latency; frame markers supplied the independent measurement.

## Additional experiment rejected

A two-lane shared body-ReID executor was measured separately using the existing per-lane appsrc/appsink ownership mechanism. Forty two-owner repetitions produced 640 exactly identical 384-D vectors. Alternating one/two/two/one-lane median times for 16 vectors were 97.805, 100.939, 96.556 and 108.317 ms. These overlapping timings did not establish a useful throughput gain. The experimental body-lane setting was removed from runtime, and no additional image was built. The exact test module remains archived for reproduction.

## Decision and remaining work

Opt80 remains a real, enabled candidate image for later controlled comparison, but is not promoted as a capacity improvement. Opt79 remains the reference/default; do not rebuild changed experimental source over its frozen tag. Use the archived overlay build context to reproduce Opt80 with the explicit candidate tag.

The measured limits involve the combined camera critical path: shared body execution/queue waits, original-frame conversion, native cascade work and policy/evidence work. At five cameras, reducing MOG time alone did not reduce the total enough to keep video fresh. Two body request lanes did not remove the device execution cost. Do not repeat those experiments as new fixes or claim another broad rewrite is required.

Further optimization needs a specific measured change to the remaining critical path and another comparison on the same frames. Preserve body updates needed for presence recovery and all event gates. Do not silently lower face/body/scene cadence to match the lighter reference benchmark. Isolated capacity, longer candidate soak, candidate-specific outage qualification and diverse-source testing remain pending; run remote tests only when the user explicitly permits them again. Known timestamp, admission, Secret-rotation, telemetry and HTTP/native discrepancies listed in the isolated validation report remain separate unresolved correctness items.

## Evidence and reproduction

Evidence root: `optimization_evidence/2026-11-05-mog-partition/`, containing image inventories, build context, stage probes, 103-test log, comparison JSON, exact invocations, source epochs, health snapshots and preserved raw artifacts for all six paired runs. Raw artifacts are retained under each case directory, so they no longer depend on `/tmp` paths. Body experiment: `optimization_evidence/2026-11-05-body-lanes/`.

Drivers: `scripts/isolated_validation/opt80_compare.py`, `analyze_opt80.py`, `mog_partition_probe.py` and `body_lane_probe.py`. For the archived body probe, mount its archived `dls_secondary-probe.py`; the production module intentionally no longer contains that experimental setting.

Example paired case command (choose a fresh case name to preserve previous evidence):

```bash
python3 scripts/isolated_validation/opt80_compare.py \
  --root "$PWD/optimization_evidence/2026-11-05-mog-partition" \
  --recording "$PWD/optimization_evidence/2026-11-05-mog-partition/fixtures/marked-crowd-1080p30.mp4" \
  --case local-4x8-opt80-retest \
  --image localhost/sentry-v8-opt80-parallel-gait-background:2026.11.05 \
  --cameras 4 --fps 8 --seconds 90
```

Both test inputs and their SHA-256 hashes are preserved under the evidence `fixtures/` directory. Original invocations used `/tmp` filenames; the example above uses the persistent equivalent.
