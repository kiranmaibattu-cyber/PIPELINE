# Capacity and use-case balance: 5, 8 and 10 FPS per camera

This expands the capacity section of [PIPELINE_TEST_PLAN.md](PIPELINE_TEST_PLAN.md). It defines what to measure; it contains no new measured camera-capacity claim. Execution is `NOT_RUN` for this protocol. The frozen reference is the current `v8-qaft-int8-tlsfix2` image and digest recorded in the main matrix.

The test must answer two separate questions:

1. At **5, 8 and 10 FPS per camera**, how many active cameras can sustain the specified analytics, scene images/vectors, source freshness and resource limits?
2. At a fixed camera count and FPS, which **additional use-case combinations on each camera** still pass, and what happens when only some cameras use expensive identity/ANPR/hazard analytics?

Scene search stays enabled on **every camera in every production-capacity profile**. All profiles retain JPEG evidence and the scene embedding. Primary-only/decode-only measurements may diagnose costs, but cannot answer either production-capacity question.

Machine-readable protocol: [CAPACITY_TEST_MATRIX.json](CAPACITY_TEST_MATRIX.json). Empty result table: [CAPACITY_RESULTS_TEMPLATE.csv](CAPACITY_RESULTS_TEMPLATE.csv). Those are test descriptors and report templates, not executable desired states or completed runs.

## 1. Fixed conditions and demand

- Test each camera FPS explicitly: **5, 8 and 10**. Do not infer a 5-FPS result from an 8/10-FPS result.
- Principal active scene interval: **2 seconds**, one full-frame scene binding per camera, including its JPEG and 768D vector. Report additional scene scopes/bindings when testing them because they add work.
- The existing quiet-motion safety sweep remains approximately **5 seconds**. Keep quiet and mixed-motion tests separate from continuously active capacity. Do not change motion, quality, dedup, tracking or emission rules to raise capacity.
- For `N` equal-rate cameras, the requested camera-processing total is `N × FPS`. For mixed rates it is `sum(camera FPS)`. Do not introduce an unexplained fixed aggregate cap.
- Active scene demand is approximately `N / 2` samples, JPEGs and vectors per second before extra triggers/views and interval-boundary effects. For example, 20 active cameras target about 10 scene samples/second, in addition to their other analytics.
- Requested camera FPS is not the incoming decode rate or every model's inference rate. Record source FPS, raw decode, selection, motion admission and model-specific schedules separately.
- Match image, host, power, cgroups, gate/profile versions, fixture bytes, input transport/encoding and scene settings in each comparison. Declare other programs' CPU/GPU/NPU use; shared-host results are conditional. Use remote192.168.1.64 only after new explicit authorization.

The primary model's 640-pixel input does not make source-resolution costs identical. Decode surfaces, original-frame conversion, gait background work, crops and JPEG encoding still depend on source dimensions and object density. Native720p/1080p/1440p and mixed-resolution results need separate labels; upscaled footage is a load fixture, not additional visual detail.

## 2. Uniform per-camera profiles

All rows include scene search. The names below are test-profile labels, not new contract app names.

| Profile | Additional configured work on each camera | Main purpose |
|---|---|---|
| S | None | Scene-enabled deployment floor; measure any fixed native detection/tracking work still running. |
| SP | Person and vehicle presence, with supported zone episodes/dwell | Presence and dwell cost. Dwell is not invented as a separate mandatory app/event. |
| SC | People and vehicle visible-now counts | Counting cost with scene retained. |
| SX | Presence, visible counts, and people/vehicle entry-exit counts | Geometry/state/event cost on shared detections/tracks. |
| SIF | Person identity with face profile | Native face/pose/embedding demand. |
| SIB | Person identity with body profile | Body crop/ReID demand when no face is required. |
| SIFB | Person identity with face and body profiles | Two independent modalities associated with the same local presence. |
| SIFBG | Person identity with face, body and gait profiles | Full identity workload, including sequence preparation. |
| SV | Vehicle presence and ANPR | Vehicle/plate/OCR candidate and evidence demand. |
| SVX | Vehicle presence, vehicle count, vehicle entry-exit and ANPR | Complete vehicle-oriented role. |
| SH | Fire/smoke | Independent hazard branch alongside mandatory scene. |
| SA | All supported declared apps and identity profiles | Full production workload; list actual bindings and geometry to identify any intentionally overlapping outputs. |

For all three FPS values, find the passing/failing camera-count boundary for each profile needed by the deployment. The baseline control uses native1080p recordings; repeat relevant profiles with native720p/1440p, differing object densities and mixed resolutions. Do not exhaustively run the entire Cartesian product before identifying useful boundaries.

**A number of app names is not a resource budget.** Counting and dwell reuse tracking/state; identity may run face, pose, body and gait models for several people. ANPR cost depends on vehicles, candidate plates and OCR attempts. Report the exact apps, identity modalities, eligible ROI counts and actual model calls. One `person_identity` app with three profiles has a different workload from face-only identity even though the app count is the same.

Use bindings/profiles to control the workload through the existing contract. Verify what actually executes: turning off a binding may suppress output without removing a fixed graph stage. Do not silently remove that stage to improve a test result.

## 3. How many other use cases can each camera run?

Perform both directions of the tradeoff:

1. **Hold the profile fixed, increase cameras:** build a camera-count frontier for S, identity, vehicle, hazard and all-use-case profiles at5/8/10FPS.
2. **Hold cameras and FPS fixed, increase analytics:** at meaningful operating points, start with scene on all cameras, add counting/presence/lines, then identity modalities, ANPR and hazards. Measure each declared bundle and selected interacting pairs. Stop at the first failing combination, preserving the last passing configuration and reason for failure.

For the second direction, exercise expensive bundles independently and in combination: identity; ANPR; hazards; identity+ANPR; identity+hazards; ANPR+hazards; and all three, with scene and the declared common base retained. The order of one incremental trial must not be the only evidence for another bundle's budget.

The result should say, for example, “AtN cameras ×8FPS, all cameras sustain scene+presence+counts; adding full identity toK cameras and ANPR toM cameras also passes,” backed by an actual role map and measurements. Until those tests run, `N`, `K` and `M` remain blank. Do not publish “five use cases per camera” without naming them and their activity.

## 4. Mixed-role fleet profiles

At fixed counts around useful frontiers, test:

- Scene on100percent of cameras; full identity on **0/25/50/75/100percent**.
- Scene on100percent; complete vehicle/ANPR role on **0/25/50/75/100percent**.
- A balanced role map: some identity cameras, some vehicle/ANPR cameras, some presence/count/crossing cameras, and some scene+hazard cameras.
- Mixed5/8/10FPS requests and mixed resolutions, plus one very busy camera among quiet/sparse peers.

List every camera's apps, identity modalities, requested FPS, resolution and measured density. When counts are not divisible by four, record the actual integer allocation and actual percentages. A nominal25percent test must not hide that rounding made it50percent. Zero-percent expensive analytics is the existing scene-only baseline, not permission to remove scene.

Compare fairness and worst-camera latency, not just fleet totals. The balanced profile's exact allocations are declared before the run and remain fixed during that comparison.

## 5. Source activity and use-case execution

Cover quiet, sparse-active, dense-people, dense-vehicles and mixed-active recordings, with both short visits/subject turnover and long stays. Prefer independent real streams for site coverage and hashed replay sources for controlled comparisons.

An “all-enabled” run is not a busy full-use-case run unless its observations actually cause the required work. Record per camera:

- Real person/vehicle detections, active/recoverable tracks and subject lifetimes.
- Face candidates, pose/embedding calls and qualified face outputs; no fabricated face when invisible.
- Body calls, qualifying updates and sends; gait collection opportunities, usable sequences and model calls.
- Plate candidates, OCR attempts/results, conflicts, accepted reads and explicit outcomes.
- Scene jobs/images/vectors; fire/smoke evaluations and temporal state.
- Rejections, suppression, improvements and skipped work by reason.

Use suitable representative clips; keep the approved gates. A run with zero gait work or no OCR candidates cannot establish a crowded gait/ANPR capacity, even if all bindings were enabled. Conversely, internal model work and emitted samples are different counters: improvement-only emission does not require a vector event on every inference.

## 6. Camera-count search and duration

For each selected FPS/profile/resolution/activity branch:

1. Validate one camera and all required output paths first.
2. Explore counts **1,2,4,8,12,16,20,24,32,40,50** only while useful and safe. Fifty is the configuration limit, not a promised capacity.
3. At the first failing point, stop escalating that branch and refine between its last passing and failing counts. Test neighboring integers where useful; repeat informative passing/boundary configurations under the same conditions.
4. Record untested higher counts as `NOT_RUN` with the stopping reason. Do not label them measured failures or a proven hardware ceiling. A specifically requested count can be reported as not qualified until directly tested safely. Record native worker/group boundaries; when the search reaches one, include neighboring counts such as19/20/21 to identify model-context, memory and scheduling jumps.
5. Use measured warmup followed by short 2–3 minute steady holds for the search. Record cold-start compilation separately.
6. Apply a 10-minute pre-soak at the selected operating point. A sustained recommendation needs the relevant longer qualification; one 60-minute continuous stress case is staged after cheaper correctness/resource checks pass, not repeated at every search point.
7. Run overload/recovery, disconnects and worker/agent faults separately from the uninterrupted stress hold.

Predeclare CPU/RAM/PID/disk/temperature stop limits and preserve excluded attempts. A guard abort is not an image crash or a successful capacity result. Any power/CPU quota change is a separate condition and must be verified in kernel cgroups, not inferred from a command's exit code.

## 7. Performance metrics to collect

Sample throughout startup, warmup, steady operation, overload and recovery. Use approximately1second resource samples where supported and retain raw data, exact windows and sample counts. Report per-camera and fleet p50/p95/p99/max where meaningful, plus time-series trends.

| Boundary/resource | Required measurements |
|---|---|
| Source and decode | Compressed arrival/bytes, source PTS and markers, incoming/source FPS, raw decoded FPS, VA decoder, caps, errors and reconnects. |
| Selection and motion | Requested/applied FPS, selected/admitted/completed/quiet frames, drops, motion state/votes, sweep intervals and wake/release delay. |
| Every model/use case | Eligible ROIs, calls, queue depth/oldest age/wait, preprocessing, model-call wall time or actual device time if available, postprocessing, rejection reasons and emitted outputs. |
| Policy and evidence | Confirmation/best-window delay, association work, source conversion/crop time, JPEG encode time/size, vectors/bytes, required versus skipped artifacts. |
| Delivery boundary | CV admission wait/refusal/coalescing, complete package submission/ACK latency, retries/rejections/exhaustion, accepted event/image/vector counts and bytes; reconcile IDs at a common cutoff. |
| Independent freshness | Marked source-frame/PES to complete agent acceptance; signed source UTC error; source sampling gap separately from API arrival gap. Capture/encoding before the measured boundary and Management after it are excluded. |
| CPU | CV container cores and percent of actual cgroup capacity; shared-worker CPU counted once; host contention, throttled periods/time and frequency. |
| RAM/state | CV cgroup memory, unique-process RSS/PSS, growth after warmup, live histories/candidates/arrays, retained surfaces, shared/model memory where measurable, FDs/threads/PIDs. |
| GPU | Supported compute/render/video/video-enhance busy counters, frequencies and accessible allocations, with node/process scope explicitly stated. |
| NPU | Supported busy/time, frequency, accessible allocations and actual per-model placement; do not infer a precise OCR split from a MULTI target string. |
| Thermal/power | Available CPU/package/device temperatures, configured power limits, frequency/throttling and critical excursions; unsupported sensors remain unavailable. |
| Network and disk | Source bytes, package/JPEG/vector throughput, socket pressure, disk I/O/free space and accepted/saved artifact sizes. |

Processing FPS and event/output rate are different. An8FPS camera does not need to emit8 events/second: counts, presence refreshes, improvement-only features and scene sampling follow their own approved policies. Report their model-call and output/ACK rates separately. GPU/NPU allocations on shared-memory hardware may overlap RAM accounting; do not add them to cgroup memory as if they were independent physical usage.

Do not add overlapping parent and child spans as total latency. Model-call wall time includes waiting and is not accelerator utilization. Shared-process RAM must not be summed once per camera. Node GPU/NPU activity includes other processes; unsupported/per-camera hardware utilization must not be fabricated. Separate intentional policy hold/sampling delay from queue/model/encoding/handoff delay.

## 8. Passing means all required dimensions pass

Use predeclared criteria from the main plan. For continuously active claimed profiles:

| Requested per-camera FPS | Minimum cumulative inference rate at95percent | Minimum each10second-window rate at90percent |
|---:|---:|---:|
| 5 | 4.75 | 4.5 |
| 8 | 7.6 | 7.2 |
| 10 | 9.5 | 9.0 |

These proposed measurement thresholds are not new contract fields or customer SLAs. Report exact achieved values. Also test the upper selection bound/duplicate-frame behavior on known-rate sources. Intentional motion-quiet work is reported separately; a quiet run does not qualify active inference.

A passing active operating point also needs:

- Correct, fresh output on **each** camera, not only a sufficient aggregate.
- Scene enabled everywhere, achieved sampling with JPEG/vector completeness and no unexplained/pressure-related sampling loss hidden in a normal-capacity pass.
- Independent source progression; proposed scene source-to-agent p95≤2seconds and p99≤5seconds, with sample counts/uncertainty and no growing backlog. Small-sample percentiles are descriptive, not stable long-term SLAs.
- All configured use cases actually exercised as required by the declared workload, correct gates/associations and no missing promised evidence.
- Bounded queues/state, no unexplained continuing memory growth, native errors or software decode/model fallback; truthful recovery/pressure status.
- Recorded thermal/power limits and supported resource headroom. Define the operational reserve before recommending a deployment count rather than inventing a headroom factor afterward.

Preserve separate rate, freshness, scene coverage, functional, delivery, resource and thermal verdicts. A configuration with enough FPS but old scene frames fails useful capacity. A short useful pass is not automatically the largest sustained or recommended deployment.

## 9. Required answer tables

The completed report must provide these tables with actual run IDs; blanks remain until measured.

**Uniform-role capacity:** one row per FPS/profile/resolution/activity combination, giving tested camera counts, largest useful short-test pass, first failed count and limiting stage, sustained-qualified count and recommended count/conditions. Include per-camera worst FPS and scene freshness, actual ROI/model demand and resources.

**Fixed-count use-case budget:** for each chosen `N × FPS`, list exact per-camera bindings/modalities, uniform or mixed assignments, the passing bundles, first failing bundle, source/scene coverage, latency and resource costs. This directly answers how much other analytics fits alongside scene search.

**Mixed-role frontier:** actual counts of identity, vehicle/ANPR, presence/count/line and scene/hazard roles, including per-camera FPS/resolution; identify fairness failures and the next affordable workload change. Do not reduce this to one number of apps.

**Stage/resource breakdown:** model/queue/policy/conversion/JPEG/handoff timing distributions and CPU/RAM/GPU/NPU/temperature/network timelines for each operating point. Identify whether the measured limit is source availability, decode, inference, CPU work, queueing, evidence or agent pressure; no guessed hardware bottleneck.

This is planning only. Existing earlier4×8 or3×10 results are historical scoped evidence, not prefilled answers for every new profile,5FPS or the repaired image's maximum capacity.
