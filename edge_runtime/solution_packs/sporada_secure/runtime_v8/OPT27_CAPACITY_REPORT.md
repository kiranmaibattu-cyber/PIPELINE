# V8 opt27 camera-capacity report

Date: 2026-11-03 (Asia/Kolkata)

## Image and acceptance rule

- Image: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.03-v8-opt27-shared-dls`
- Local image ID: `e49faaea2bcc32fcbbe5b108622053060f4fb2b6ca109b822e4e90e9aebce872`
- Hardware: Intel Core Ultra 9 285H, Intel iGPU/NPU, 30 GiB system RAM
- Workload: 1920x1080 H.264 MPEG-TS/TCP, vehicle counting and ANPR,
  production DL Streamer path and production motion gate
- Grouping: at most five camera pipelines per process

A run passed only when:

1. every configured camera initialized and remained healthy;
2. every camera had a recent decoded frame;
3. the minimum of the final three samples sustained at least 90% of
   configured aggregate decode FPS;
4. no Intel allocation abort, segmentation fault, or group-process crash
   occurred; and
5. V8 handoff reported no protocol error or exhausted package.

The controlled tests used one independent local TCP replay server per camera.
This avoids treating external source fan-out/reconnect behaviour as CV
capacity. The high-count tests used the Camera12 recording; the active
inference test used the higher-motion Camera02 recording.

## Desired-state FPS resolution

The corrected V8 desired-state behavior is:

```text
maximum cameras = 50
effective camera FPS = camera.fps when supplied, otherwise 8
configured aggregate FPS = sum(effective camera FPS for every camera)
fixed aggregate FPS maximum = none
```

The desired state therefore supplies the requested load directly. For example,
50 cameras without explicit FPS resolve to `50 * 8 = 400` aggregate configured
FPS. The runtime no longer rejects that configuration because of the old fixed
64-FPS guard. Acceptance by the schema is still not proof that the hardware can
sustain the request; `/healthz` reports the resolved aggregate and actual
decoded/inference rates so Management can see the difference.

## Measured capacity curve

| Cameras | FPS/camera | Target aggregate | Result | Final decode | Final inference | Container memory | Notes |
|---:|---:|---:|---|---:|---:|---:|---|
| 10 | 6 | 60 | PASS | 59.97 | 23.99 | 3.58 GB | Live mixed TCP sources; motion gate active |
| 16 | 4 | 64 | PASS | 63.86 | 63.86 | 6.07 GB | Higher-motion Camera02; every decoded frame admitted |
| 21 | 3 | 63 | PASS | 62.98 | 0.50 | 7.69 GB | Quiet Camera12; motion-gated |
| 32 | 2 | 64 | PASS | 64.19 | 2.10 | 9.45 GB | Quiet Camera12; motion-gated |
| 40 | 1 | 40 | PASS | 40.10 | 0.70 | 12.94 GB | Quiet Camera12; motion-gated |
| 45 | 1 | 45 | PASS | 45.04 | 0.51 | 13.41 GB | Quiet Camera12; motion-gated |
| 48 | 1 | 48 | PASS | 48.00 | 0.57 | 13.66 GB | Quiet Camera12; motion-gated |
| 49 | 1 | 49 | PASS | 49.05 | 0.53 | 14.12 GB | Maximum demonstrated camera count |
| 50 | 1 | 50 | FAIL | Did not stabilize | Did not stabilize | ~9.94 GB when sampled during failure | Intel graphics allocation crashes during startup |

The 16-camera higher-motion run is the strongest throughput result. All 16
cameras sustained approximately 4 FPS decode and inference simultaneously.
GPU active residency was approximately 82-83% in inference-heavy windows.
All 32 produced V8 packages were durably acknowledged without backpressure,
exhaustion, or protocol errors.

Artifact: `/tmp/sentry-opt27-capacity-16._sm8a5hu`

## Fifty-camera failure

The 50-camera configuration was valid under V8 because its aggregate target
was 50 FPS. It did not become operationally healthy.

Observed during startup:

- Intel `multi_graphics_allocation.cpp` failures appeared eight times in the
  captured log;
- four five-camera group processes exited with signal 11; the supervisor
  emitted one exit record per affected camera, producing 20 exit records;
- affected groups restarted, after which their one-client test sources had
  disconnected;
- a sampled health response had 6 healthy, 8 degraded and 36 unavailable
  cameras, with approximately 12.37 aggregate decode FPS;
- shared readiness remained available, but camera capacity acceptance failed.

Artifact: `/tmp/sentry-opt27-capacity-50.dwzv4o5v`

Because 49 passed once and 50 failed once, 49 is the maximum demonstrated
count—not a reliability guarantee at the boundary. A single successful short
run is insufficient to promote 49 as a production limit.

## Practical deployment limits

For the tested opt27 image:

- At 6 FPS per camera: ten live cameras passed. This is a measured point, not a
  contract-imposed camera maximum.
- At 4 FPS per camera: 16 cameras passed at 64 aggregate FPS,
  including a higher-motion run where inference matched decode.
- At 3 FPS per camera: 21 cameras passed.
- At 2 FPS per camera: 32 cameras passed.
- At 1 FPS per camera: 49 is the maximum demonstrated count; 50 failed during
  Intel graphics allocation.

For production planning, use the 16-camera/4-FPS point as the strongest
validated active-analytics capacity. Treat 32-camera/2-FPS as a validated
low-motion operating point. Do not use 49 as a production guarantee until it
passes repeated cold starts, a long soak, source disconnect/reconnect tests,
and a representative mix of face, body, gait, scene, fire/smoke and ANPR load.

## What would be required for a reliable 50-camera claim

1. Coordinate model/device initialization across camera groups instead of
   allowing ten groups to allocate concurrently.
2. Re-evaluate group size. Larger groups reduce duplicated model instances but
   increase the native-crash fault domain.
3. Repeat 50-camera cold starts and an overnight soak under a Kubernetes memory
   limit and the production Intel device-plugin configuration.
4. Use representative active footage and the actual per-camera use-case mix.
5. Preserve the current V8 contract and production gates while making these
   runtime resource-management changes.

The FPS-resolution correction was built as:

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.03-v8-opt28-dynamic-fps
```

A live nine-camera test omitted every camera `fps`. The runtime resolved all
nine cameras to 8 FPS, reported 72 configured aggregate FPS in `/healthz`, and
sustained approximately 71.96/72 decode FPS. This directly verifies that the
old fixed 64-FPS rejection is absent.

Artifact: `/tmp/sentry-opt27-capacity-9.v1gp5zh0`

## Opt28 default-8 active-camera boundary

The higher-motion Camera02 recording was replayed through independent TCP
sources with `fps` omitted from every camera. Opt28 therefore resolved every
camera to the default 8 FPS.

| Cameras | Requested aggregate | Result | Observed behaviour |
|---:|---:|---|---|
| 9 | 72 FPS | PASS | Approximately 71-72 decode/inference FPS; GPU about 85-87% in busy windows |
| 10 | 80 FPS | PASS | Approximately 79-80 decode/inference FPS; GPU about 86-87% in busy windows |
| 12 | 96 FPS | BOUNDED PASS | Final-three-sample minimum 90.2%; individual samples ranged approximately 82.6-93.4 FPS |
| 13 | 104 FPS | FAIL | Multiple samples at approximately 79-82% of target; GPU approximately 87-89% |

By the stated 90% acceptance rule, 12 is the maximum demonstrated active
camera count at the default 8 FPS. It is a saturation-edge result, not the
recommended production setting. Ten active cameras at 8 FPS provide the
stronger measured operating point with approximately full requested
throughput. These tests enabled counting and ANPR; enabling scene, face, body,
gait and fire/smoke together requires a separate full-use-case capacity run.

Artifacts:

- `/tmp/sentry-v8-capacity-9.dmyxdqzm`
- `/tmp/sentry-v8-capacity-10.tmr0qciq`
- `/tmp/sentry-v8-capacity-12.a85f4842`
- `/tmp/sentry-v8-capacity-13.3gxdv7t8`
