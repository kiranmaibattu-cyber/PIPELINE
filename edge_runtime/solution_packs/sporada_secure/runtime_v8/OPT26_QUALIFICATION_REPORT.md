# V8 opt26 full-cascade qualification report

Date: 2026-11-03 (Asia/Kolkata)

## Subject

- Image tag: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.03-v8-opt26-full-cascade`
- Local image ID: `ba30edf9656be3dbd32914741c754595fad1f72bf2760b2cdd653f122bb9a9c0`
- Repository digest: `sha256:87af7123bc23a0ddda15c915bde3e9e9d6678a18125da477a467880ca87bb8c6`
- Contract: Sentry V8 desired state with the unchanged V7 `7.0` observation/evidence/embedding wire payloads required by V8
- Production-policy SHA-256: `95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03`

## Overall decision

**Not ready for an unrestricted production rollout.**

The one-camera runtime, V8 package delivery, evidence linkage, hazard lifecycle,
controlled reconnect, and ten-minute resource lifecycle all passed. The image
did not pass multi-camera capacity or tracking-continuity acceptance, and ANPR
recall is too low for production use across the labelled camera corpus.

| Area | Result | Main evidence |
|---|---|---|
| V8 contract/schema | PASS | Host and in-image validators pass; source and embedded schema trees have no diff |
| One-camera full pipeline | PASS | Healthy decode/inference, valid V8 packages, linked evidence and embeddings |
| Ten-camera isolation | PARTIAL | All ten cameras initialized and delivered; one camera GPU allocation abort was restarted without blocking peers |
| Ten-camera capacity | FAIL | About 9.8 GB aggregate camera RSS, one GPU allocation abort, and only 54.329/60 configured decode FPS during the sampled window |
| Ten-minute one-camera soak | PASS (bounded) | No restart, reconnect, decoder error, delivery error, or positive post-warm-up memory slope |
| ANPR labelled corpus | FAIL | 8 exact reads from 71 plates (11.3% exact end-to-end recall) |
| Tracker continuity | FAIL | Current `zero-term-imageless` mode scored 537 ID switches on the fixed MOT17 replay versus 454 for the older centroid baseline |
| Fire/smoke lifecycle | PASS | One `smoke` suspected transition and one linked clear transition using the same episode ID |
| Controlled TCP reconnect | PASS | Health remained available; decoder recorded one error/reconnect and returned to about 8 FPS |
| Delivery/evidence | PASS in exercised paths | Schema-valid packages; scene/fire/ANPR evidence and scene/identity embeddings were acknowledged |

## Contract and policy verification

The authoritative contract tree is `newdetails/sentry-v8`. The image embeds the
same files under `/opt/traffic-pilot/contracts/sentry-v8` and
`/opt/traffic-pilot/image_schema`.

Both the host validator and the validator baked into the image reported:

```text
PASS: 10 schemas, 16 V7 wire examples, V8 desired state,
digest/error/status contracts, bounded local handoff, and single-writer ownership
```

The `7.0` wire-schema and multi-box-delivery image labels are intentional, not
stale V7 runtime configuration. V8 explicitly preserves V7 observation,
evidence, embedding and multi-box payloads while adding the V8 desired state
and delivery-agent handoff.

The production-policy file is unchanged and matches the image label:

```text
SENTINEL_V6_PRODUCTION_POLICY.md
sha256 95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03
```

The DL Streamer integration remains upstream of the camera-local production
policy. `gvadetect`, `gvatrack`, face/ANPR/identity/scene/fire model execution
produce observations; Sentinel still owns presence, recovery, zones, counts,
dwell, candidate selection, temporal confirmation, deduplication, evidence
selection and V8 package admission.

## Ten-camera capacity and fault isolation

Inputs were ten independent TCP camera endpoints from
`tcp://192.168.1.92:9001` through `:9010`, configured for an aggregate target of
60 FPS with ANPR and vehicle counting.

Observed per-camera decoded/inference FPS in the final sample:

| Camera | Decode FPS | Inference FPS |
|---|---:|---:|
| camera01 | 5.987 | 5.987 |
| camera02 | 6.028 | 3.617 |
| camera03 | 5.992 | 0.048 |
| camera04 | 0.358 | 0.358 |
| camera05 | 5.998 | 0.048 |
| camera06 | 5.923 | 5.923 |
| camera07 | 5.995 | 0.048 |
| camera08 | 6.064 | 6.064 |
| camera10 | 5.986 | 5.986 |
| camera12 | 5.998 | 0.048 |

- Aggregate decoded FPS: approximately 54.329.
- Aggregate admitted inference FPS: approximately 28.127. Low values on quiet
  cameras are motion-gate suppression, not failed decode.
- Aggregate camera-process RSS: approximately 9.8 GB.
- All ten cameras produced packages; 62 packages were accepted with no schema
  rejection.
- Package types: 14 `camera_health`, 16 `zone_count`, 17 `object_present`,
  13 `plate_outcome`, and 2 `plate_read`.
- Camera02 exited with signal 11 after an Intel graphics allocation abort in
  `multi_graphics_allocation.cpp`. The supervisor restarted it.
- Camera04 and camera06 experienced source interruptions/reconnects without
  blocking healthy peers. Fleet readiness remained true.

Conclusion: process isolation works, but the tested per-camera model/context
architecture is too memory-heavy and did not pass a clean ten-camera capacity
run.

Artifact directory: `/tmp/sentry-opt26-capacity.5cLXlb`

## Ten-minute one-camera soak

Input:
`Camera12_192.168.1.13_2min_h264.mp4`, looped through local MPEG-TS/TCP.

- Duration: 600 seconds after readiness.
- Mean decoded FPS: 7.9952 against an 8 FPS target.
- Mean admitted inference FPS: 0.064. This quiet recording was intentionally
  suppressed by the motion gate; this was a lifecycle soak, not a sustained
  inference stress test.
- Camera-process RSS: 2260.41 MB at the first sample, 2271.11 MB at the last.
- Camera-process PSS: 2202.12 MB at the first sample, peak 2217.80 MB, and
  2212.82 MB at the last sample.
- Linear PSS slope after including warm-up: approximately -0.034 MB/minute.
- Decoder errors: 0.
- Reconnects: 0.
- Decoder starts: 1.
- Container restarts: 0.
- Valid packages: 67 (40 scene samples, 20 zone-count freshness updates and
  7 camera-health updates).
- Evidence bytes acknowledged: 17,012,548.
- No schema rejection, traceback, abort, segmentation fault, longjmp warning,
  or Python resource-tracker warning.

Conclusion: bounded one-camera resource lifecycle passed. This does not prove
an overnight soak or multi-camera stress stability.

Artifact directory: `/tmp/sentry-opt26-soak.g0lemY`

## ANPR across labelled camera conditions

Dataset:
`IMAGE_TESTS/datasets/generated/live-anpr-video/live-anpr-verified-5fps.mp4`
with 71 human-transcribed plates in
`live-anpr-verified-5fps.json`.

The full image processed the 233-second replay at approximately 5 FPS through
TCP MPEG-TS, VA decode, DL Streamer vehicle/plate detection, local vehicle
association, OCR, candidate policy, V8 evidence and delivery.

- Accepted `plate_read`: 10.
- Exact accepted reads: 8.
- Incorrect accepted reads: 2.
- Explicit `plate_outcome=unreadable`: 3.
- No terminal ANPR output: 58 cases.
- Accepted-read precision: 8/10 = 80.0%.
- Accepted-read recall: 10/71 = 14.1%.
- Exact end-to-end recall: 8/71 = 11.3%.
- Terminal outcome coverage: 13/71 = 18.3%.

The eight exact outputs were:

```text
TS07GC6481 TS07GE1907 TG34T6745 TS07HN8478
AP26AF5118 TG07X4749 TS04ED8998 TS08GY6037
```

The two incorrect accepted outputs were:

```text
frame 899: emitted TS09FF2256; truth AP40ED5459
frame 991: emitted TO27FG255; truth TS09EU1206
```

Each accepted read and unreadable outcome carried linked frame evidence. The
stream stayed healthy and no package was rejected. Therefore the low recall is
not a transport/delivery problem. It is primarily in vehicle/plate detection,
track association, candidate collection, and/or terminal-outcome policy.

Artifact directory: `/tmp/sentry-opt26-anpr-detail.5dVeEl`

## Tracker accuracy and identity switches

The current full-cascade image uses authoritative DL Streamer
`gvatrack tracking-type=zero-term-imageless`, followed by Sentinel's separate
presence recovery policy.

On the fixed seven-sequence MOT17 replay (1,650 selected frames and 12,978
identical detector boxes supplied to each tracker), that mode scored:

| Metric | Current GVA mode |
|---|---:|
| HOTA | 30.56% |
| IDF1 | 33.42% |
| MOTA | 25.93% |
| ID switches | 537 |
| Missing tracker IDs | 0 |
| Mean sequence-stage P95 | 1.63 ms |

The older Sentinel centroid baseline had 454 ID switches on the same fixed
detections. GVA `zero-term` scored 463 switches and better HOTA/IDF1, but it is
not the mode currently configured in opt26.

This benchmark isolates tracker association and proves that every supplied box
received an ID. It does not include current-detector recall, downstream
presence recovery, or cross-camera Re-ID. The current mode therefore fails the
identity-switch improvement criterion and should not be described as a tracker
accuracy upgrade merely because it is faster or native DL Streamer.

Reference report:
`IMAGE_TESTS/reports/sentry-v6-gva-tracker-trial/REPORT.md`

## Fire/smoke suspected and clear transitions

Input combined 49.4 seconds of `fire_smoke.mp4` with 35 seconds of clean video.

The edge emitted exactly one confirmed suspected transition:

```text
event_type: fire_smoke_suspected
hazard: smoke
hazard_episode_id: 4673b2e6-6f42-4dc5-8c33-a42ed08aa40b
evidence: frame
```

After confirmed negative observations in the clean tail it emitted exactly one
clear transition with the same hazard and episode ID:

```text
event_type: fire_smoke_cleared
hazard: smoke
hazard_episode_id: 4673b2e6-6f42-4dc5-8c33-a42ed08aa40b
```

No duplicate suspected or clear transition was emitted, and no runtime or
delivery error occurred.

Artifact directory: `/tmp/sentry-opt26-fire-local.JNq4En`

## Controlled disconnect and reconnect

A local MPEG-TS/TCP source was force-killed after the camera reached about
8 FPS, then restarted on the same endpoint.

- `/healthz` continued to update throughout the outage.
- Runtime readiness remained true; one failed/retrying camera did not stop the
  API or worker.
- The decoder eventually reported `degraded`, `retrying=true`, one decoder
  error, one reconnect, and two decoder starts.
- The camera process PID and stream-session ID remained stable.
- The decoder returned to `healthy` and approximately 8 FPS.
- Camera-health transitions were delivered through the normal V8 package path.
- The local MPEG-TS socket buffered frames, so source loss was not observable
  immediately at kill time. Runtime recovery began when EOS reached the
  decoder; source-loss alert latency must account for this buffering.

Artifact directory: `/tmp/sentry-opt26-reconnect-local.2wrjzT`

## Delivery and evidence observations

- The mock delivery agent validated every exercised package against the V8
  package and underlying wire schemas.
- Scene samples carried frame JPEG evidence plus the 768-D SigLIP2 embedding.
- Fire/smoke suspected carried frame evidence; clear was a state-transition
  observation without redundant evidence.
- Accepted and unreadable ANPR outcomes carried frame evidence.
- Prior one-camera cascade replay produced linked face, body and gait samples
  with their embeddings and evidence.
- Complete package delivery used stable package/object IDs and ordered
  observation, evidence and embedding acknowledgements.

No rejection was observed in the qualification runs. The ten-camera run also
showed that a camera-process failure did not invalidate packages from peers.

## Required work before production acceptance

1. Reduce per-camera OpenVINO/GPU/NPU context and model memory. Re-run 1, 8,
   10, 16 and 50-camera capacity tests without graphics allocation aborts.
2. Improve ANPR terminal coverage and exact recall while retaining the current
   explicit unreadable outcome and evidence linkage. Calibrate with labelled
   footage, not only individual crops.
3. Re-evaluate `gvatrack` mode and association parameters. Compare current
   `zero-term-imageless`, `zero-term`, and the prior tracker on current detector
   outputs and site-specific occlusion footage.
4. Run a sustained-motion multi-camera soak, followed by an overnight soak;
   the completed ten-minute soak was mostly motion-suppressed.
5. Add automated acceptance thresholds for GPU allocation failures, PSS per
   camera, ID switches, ANPR exact recall, reconnect time, and complete evidence
   delivery.

Until those items pass, the image is suitable for controlled development and
single-camera/low-camera-count validation, not a claimed 50-camera production
release.
