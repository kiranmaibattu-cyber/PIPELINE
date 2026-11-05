# V8 opt27 shared DL Streamer qualification report

Date: 2026-11-03 (Asia/Kolkata)

## Subject

- Image tag: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.03-v8-opt27-shared-dls`
- Local image ID: `e49faaea2bcc32fcbbe5b108622053060f4fb2b6ca109b822e4e90e9aebce872`
- Contract: Sentry V8 desired state with V7 `7.0` observation, evidence, embedding, and acknowledgement payloads preserved by V8
- Production-policy SHA-256: `95f244203f6e53c949624925315c4d36ca07de040acdf03ffbdbfd831a832d03`

The image has not been pushed as part of this qualification, so no remote
registry digest is claimed here.

## What changed from opt26

Opt26 already used the production DL Streamer cascade inside each camera
process. Its capacity problem was that each process still owned separate
compiled model instances and device resources.

Opt27 groups at most five camera pipelines in one OS process and uses shared
DL Streamer `model-instance-id` values per stateless model kind. This is real
process-local sharing; it is not the older mmap/BGR shared-scheduler path.

Each camera retains independent:

- GStreamer graph and `gvatrack` element;
- stream session and source timestamps;
- presence, recovery, zone, count, dwell, and line state;
- ANPR, face/body/gait, scene, and fire/smoke policy state;
- evidence selection and V8 package production.

The internal defaults are:

```text
SENTINEL_SHARED_DLSTREAMER_CAMERA_PROCESSES=true
SENTINEL_SHARED_DLSTREAMER_GROUP_SIZE=5
SENTINEL_SHARED_DLSTREAMER_STARTUP_STAGGER_SECONDS=15
```

They are runtime implementation controls and do not extend desired state.

## Validation summary

| Test | Result | Main evidence |
|---|---|---|
| Host V8 contract validator | PASS | 10 schemas, 16 wire examples, desired state and handoff contracts |
| V8 runtime tests | PASS | 26 tests |
| Inherited production-policy tests | PASS | 78 tests |
| One-camera full pipeline | PASS | Healthy decode, valid packages and one unique camera process |
| Five cameras, one shared group | PASS | All cameras shared PID 47 and sustained approximately 8 FPS each |
| Ten cameras, two groups | PASS for bounded run | Approximately 59.97/60 aggregate decode FPS and no process/GPU crash |
| Two cameras, all use cases | PASS for stage execution | Full policy/model stage set executed in one shared process |
| Overnight soak | NOT RUN | Required before production promotion |
| Camera-capacity curve | BOUNDED PASS | 16 active cameras at 4 FPS and 49 quiet cameras at 1 FPS passed |
| Fifty-camera capacity | FAIL | Intel graphics allocation failures caused group-process crashes during startup |

## Ten-camera result

Inputs were ten independent TCP endpoints corresponding to Camera01 through
Camera12. The desired state configured 60 aggregate FPS.

- Unique camera processes: 2.
- Cameras per process: 5 and 5.
- Final aggregate decode FPS: approximately 59.966.
- Final aggregate admitted inference FPS: approximately 23.994. Lower values
  on quiet cameras are motion-gate suppression, not failed decode.
- Unique-process RSS: approximately 3073.09 MB.
- Unique-process PSS: approximately 2675.52 MB.
- Valid packages: 121.
- Event types: 29 `camera_health`, 34 `zone_count`, 41 `object_present`,
  17 `plate_outcome`, and 1 `plate_read`.
- Evidence bytes: 12,112,009.
- Schema/protocol rejection: none observed.
- Intel graphics allocation abort: none observed.
- Camera group process crash: none observed.

The live TCP sources had transient reconnects, but all ten cameras were healthy
at the final sample and neither group was terminated.

Artifact directory: `/tmp/sentry-opt27-ten.8fxbbX`

## Comparison to opt26

| Measurement | Opt26 per-camera processes | Opt27 grouped sharing |
|---|---:|---:|
| Camera processes | 10 | 2 |
| Aggregate configured FPS | 60 | 60 |
| Final aggregate decoded FPS | approximately 54.329 | approximately 59.966 |
| Reported camera-process RSS | approximately 9.8 GB | approximately 3.07 GB, unique PIDs |
| Intel GPU allocation crash | 1 | 0 in bounded run |

RSS is not equivalent to pod cgroup memory, and the old/new accounting differs.
The useful acceptance result is that opt27 counts each process once, keeps all
ten streams at target in the final sample, and did not reproduce the allocation
crash during this run.

## Full-use-case check

Two live cameras with all configured use cases shared one process. Both decoded
and processed at approximately 8 FPS. The shared process used approximately
2643 MB RSS and 2586 MB PSS.

Observed stage families included ANPR OCR/policy, count, metadata bridge,
event policy, fire/smoke bridge/policy, line crossing, presence, TransReID,
face pipeline and pose estimation, gait processing, and scene embedding.
The run produced valid scene, count, presence, track lifecycle, and person
feature packages, including SigLIP2 and TransReID embeddings.

No face or gait embedding was emitted in the 90-second window because the
unchanged production gates did not qualify a candidate. Stage execution is not
the same as guaranteed event emission.

Artifact directory: `/tmp/sentry-opt27-two-full.GqwvPt`

## Contract and policy boundary

No desired-state or wire-schema change was required. Grouping, model-instance
sharing, startup staggering, thread-local log context, and PID-deduplicated
resource reporting are internal runtime behavior.

The tracker and all production gates remain camera-local. Sharing stateless
model execution therefore does not intentionally alter:

- presence confirmation or recovery;
- zone entry/exit, count, dwell, or line crossing;
- ANPR consensus and best evidence selection;
- face/body/gait candidate selection;
- scene cadence and pressure policy;
- fire/smoke suspected/clear transitions;
- event/evidence/embedding package semantics.

## Remaining risks

1. A native crash now affects a bounded group of up to five cameras rather
   than one camera. Other groups remain separate.
2. The authoritative tracker remains DL Streamer
   `zero-term-imageless`. The tracker accuracy/identity-switch concern found
   during opt26 qualification is not fixed by model sharing.
3. The longest opt27 capacity observation was a bounded test, not an overnight
   soak.
4. A later capacity qualification proved 16 active count/ANPR cameras at 4 FPS
   and 49 quiet cameras at 1 FPS. Fifty cameras failed during Intel graphics
   allocation. See `OPT27_CAPACITY_REPORT.md`.
5. Hardware utilization and pod memory should be confirmed under the actual
   Kubernetes cgroup and Intel device-plugin configuration.

## Decision

Opt27 fixes the specific per-camera model/context duplication that caused the
opt26 capacity failure in the tested ten-camera workload. It is suitable for
continued staging qualification. It is not yet approved for unrestricted
production, and the measured 50-camera startup failed.
