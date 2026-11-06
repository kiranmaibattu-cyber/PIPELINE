# Opt79 isolated-host validation plan

Forward regression work uses [PIPELINE_TEST_PLAN.md](PIPELINE_TEST_PLAN.md), its 282-case matrix and execution ledger. This file remains the completed historical Opt79 campaign plan; it is not the checklist or a passing verdict for the latest image.

Prepared 2026-11-05 (Asia/Kolkata). Campaign completed; see [final report](ISOLATED_VALIDATION_REPORT.md) for actual results and exclusions. Target host: `192.168.1.64`, user `admin1`, working directory under `/home/admin1/Documents`. Credentials must not be written into this plan, handoff, commands saved as artifacts, or logs.

This is a finite, reproducible validation campaign. It covers the scenarios below; unrun, unavailable and blocked cases must be explicit. It cannot certify every possible camera or indefinite operation. The plan was prepared before execution. Current execution state and exceptions are recorded in `OPTIMIZATION_HANDOFF.md` and the remote ledger; this document is not a live completion claim.

## Frozen candidate and comparison

- Candidate: `localhost/sentry-v8-opt79-shared-gait-background`.
- Local image ID: `f82a0769ed12eff25ef7bdabc9619370ebfdceeed737eaed21e5f2381e4feb86`.
- Recorded local manifest digest: `sha256:03d82a6c021781484c4d476aeb229dc08bbb9068467fba701d2c157bc19e8cfa`.
- Export/import the existing image; verify identity and baked model/schema hashes at destination. A transport can change manifest representation; retain original and imported identifiers and verify configuration/layer/model identities rather than silently substitute a rebuild.
- Reference: frozen opt76, and opt79 with shared background disabled only for the focused comparison. No threshold/model tuning between baseline and candidate.
- V8 desired schema 8.0; wire 7.0; local delivery-agent handoff 1.0. Preserve existing source Secret references. No direct CV upload replacing the agent.
- Shared gait background enabled; previous asynchronous gait worker disabled. Record all actual environment overrides.

## Proposed acceptance rules

These are test criteria, not new wire-contract fields or established customer SLAs. Report stricter measurements alongside the verdict.

- Continuous-active runs: each camera cumulative admitted inference >=95% of requested FPS, and >=90% in each measured 10-second window after warmup. Report selection, admission, completion and skipped work separately. Motion-quiet runs have different expected inference cadence; do not fail intentional quiet behavior.
- Controlled-source frame freshness: proposed p95 <=2 seconds and p99 <=5 seconds, with no increasing source backlog. Explicit best-candidate/temporal confirmation windows are accounted for separately; a fire confirmation window is not a model execution delay.
- Use externally instrumented replay timestamps/frame markers to verify source progression. Runtime PTS-to-wall estimates, zero-clamped latency and last-frame-receive age alone cannot certify freshness.
- Every accepted package has valid IDs, dimensions, finite normalized vectors where required, retrievable promised media and matching source camera/frame/subject/time. Drain and reconcile exact package IDs at a common endpoint before asserting losses.
- No unexplained crash, CPU model/decode fallback, cross-camera/session leakage or unbounded pending work. Unsupported hardware/input/metrics must be visible rather than fabricated.
- Recovery, memory and pressure results retain numeric ranges even when a verdict passes. A short memory plateau is not proof of leak freedom or a 24/7 SLA.

## A. Host isolation and reproducibility

| ID | Test | Evidence |
|---|---|---|
| A01 | Inventory CPU topology, RAM/swap, Intel GPU/NPU models, drivers/firmware, OpenVINO/DLS, OS/kernel, container engines, device permissions, storage and network | Hardware/software inventory and compatibility decision; do not assume this host matches compute-a |
| A02 | Inventory processes, services, containers, orchestration jobs and accelerator clients | Record competing workload PIDs/owners/services and restart commands |
| A03 | Stop competing application workloads, including automatic restart sources, using graceful stop before targeted force termination | Stop log; preserve SSH, networking, essential OS services, accelerator drivers and the container runtime used for testing; no blanket kill-all |
| A04 | Measure idle CPU/RAM/swap, GPU engines/video decode, NPU, disk/network and throttling | At least a short idle baseline; detect reappearing background jobs |
| A05 | Transfer/verify frozen image, models, schemas and fixtures; record available source reachability | Hash manifest, configuration, actual commands and source specifications; never log URL credentials/tokens |
| A06 | Validate telemetry independently | Compare health with cgroups/OS/device counters; PSS/shared RSS deduplication; node vs process attribution; unsupported counters marked unavailable |

## B. Contract, startup and model/decode placement

| ID | Test cases | Expected checks |
|---|---|---|
| B01 | Existing runtime suite and both V8 validators in the target environment | Preserve all results, dependency failures and canonical/baked schema differences |
| B02 | 1/8/16/50-camera schema/config acceptance; default FPS, mixed explicit FPS, invalid IDs/geometry/bindings/profile/input | Correct validation and diagnostics; schema 50-camera acceptance is not measured capacity |
| B03 | Valid config; missing/unreadable/malformed Secret/config; stale/duplicate/new revision; add/remove camera and binding | No silent initialization failure; applied revision, camera state and use-case changes correct; unaffected peers continue |
| B04 | Cold/warm start and three repeated stop/start cycles | Startup readiness, compile/warmup time, worker lifecycle and resource cleanup |
| B05 | Actual device placement for every model and hardware decode | Verify actual GPU/NPU/VA execution; absent/inaccessible devices produce explicit errors; no silent CPU fallback |
| B06 | Native group size/model sharing, request counts, batch settings | One camera-local tracker/policy per camera; shared compiled instances where intended; no duplicate model explosion |

## C. Input and media cases

| ID | Test cases | Measurements/checks |
|---|---|---|
| C01 | RTSP, RTSPS, TCP MPEG-TS; local fixtures; HTTP/HTTPS acceptance/native-path behavior | Codec/source validation and diagnostics. HTTP native support is a known gap to verify, not presumed working |
| C02 | H.264 and H.265 where each transport supports them; mismatched/unsupported codec | Actual VA decoder and negotiated source dimensions; input failures explicit |
| C03 | 720p,1080p,1440p; odd dimensions/aspect ratios/portrait and one beyond-supported-size case | Detector letterbox/source mapping, ROI clipping, geometry and explicit supported/unsupported outcome |
| C04 | 15/25/30-source FPS; requested FPS below/equal/above input; mixed camera rates and variable arrival | Distinguish source-limited input from compute-limited processing; no fabricated duplicated inference |
| C05 | Missing/corrupt/truncated packets, jitter, delay, slow source, no frames, timestamp reset/jump/loop and reconnect | Correct counters, clock/session handling, bounded queues and recoverability |
| C06 | Several distinct camera IDs reading the same TCP endpoint and genuinely independent endpoints | Fan-out/source-server limits separate from image capacity; no cross-camera state sharing |

## D. Performance and capacity

First qualify one-camera execution; then grow workload until the passing/failing boundary is found. Do not exhaustively run the full Cartesian product.

| ID | Workload | Test strategy |
|---|---|---|
| D01 | Quiet/empty and stationary subjects | Motion wake/quiet/sweep cadence, scene sampling, actual inference and decoder costs separately |
| D02 | Sparse/dense people, sparse/dense vehicles, mixed traffic and lighting/motion variation | Full-use-case demand, actual person/vehicle/ROI counts and per-stage work rates |
| D03 | Same content at720/1080/1440, matching encoding/FPS | Fixed 4-camera8/10-FPS points; original native-resolution footage separately; upscaling is a load test, not added image detail |
| D04 | Camera scaling at8FPS and10FPS | Start1/2/4/8, then12/16/20 and near-boundary increments only while safe/relevant; stop a failing branch, bracket the limit; validate50 config separately |
| D05 | Different use-case bundles | Primary-only diagnostic, count/presence, identity, ANPR, scene, fire and all-use-case production; only all-enabled active results qualify full-pipeline capacity |
| D06 | One busy camera mixed with quiet peers; mixed resolutions/FPS and independent scenes | Per-camera fairness, starvation, noisy-neighbor effect and shared-model queue wait |
| D07 | Comparison at a few informative points | Same scene/FPS/bindings: opt76 vs opt79; shared-mask rollback0 vs1. No gate tuning to improve scores |
| D08 | Overload and recovery | Exceed sustainable rate/count deliberately, measure source age/queues/drops/state, then reduce load and confirm backlog clears |

Capture separately: incoming compressed bytes/PTS; raw decoded output; selected, motion-admitted and completed FPS; real vs predicted tracking observations; queue wait; preprocessing/model execution/postprocessing; policy; crop/JPEG; handoff/agent/remote acknowledgement; source-to-output delay. Report p50/p95/p99/max and per-camera time series where sample counts permit. Never sum overlapping parent/child stage times as elapsed latency.

## D.1 Mandatory scene-search floor and camera/use-case balance

User clarification: scene search is required on every camera. All deployment-capacity and use-case-balance results therefore include scene search on every camera, with its JPEG evidence and 768D embedding. Do not disable scene, drop its images, or slow cadence to make a higher camera count pass. Decode-only/model-only runs remain diagnostics, never deployment-capacity claims.

Freeze active scene interval at the current configured 2 seconds for the principal profiles. The existing quiet-motion safety sweep is approximately5seconds: measure/report it explicitly, not as unconditional2second sampling. Report achieved per-camera interval distributions, missed/coalesced/skipped samples and reasons, source time and handoff completeness. If the image cannot meet an expected interval, classify a cadence failure even when detectorFPS passes. Do not count an aggregate scene rate as proof every camera is covered.

| Profile | Required on every camera | Additional configured work |
|---|---|---|
| S | Scene search, JPEG and vector | No other use-case bindings; measure any detector/face work still executed internally |
| P | Scene search, JPEG and vector | Person/vehicle presence, zones, dwell, counts and crossings |
| I | Scene search, JPEG and vector | Presence plus face/body/gait identity |
| V | Scene search, JPEG and vector | Vehicle presence/count/entry-exit plus ANPR |
| H | Scene search, JPEG and vector | Fire/smoke monitoring |
| A | Scene search, JPEG and vector | All supported use cases, active people and vehicles |
| M | Scene search, JPEG and vector | Mixed camera roles: identity on selected cameras, ANPR on others, hazards where configured, remaining cameras scene/presence |

For M, measure representative25%/50%/100% identity-enabled and25%/50%/100% ANPR-enabled camera shares, plus a balanced deployment example. Change only declared test configurations, never production gate defaults. Preserve a record of which camera gets which role. A scene-only camera still counts in total decode, scene encoding, JPEG and delivery demand.

Principal result tables cross camera count,720/1080/1440p and mixed resolutions,8/10 requestedFPS,activity/ROI density and the profiles above. Search the count boundary progressively; do not run every Cartesian combination. At the boundary compare alternatives on identical content: number of additional cameras gained/lost when identity or ANPR is enabled on part/all of the fleet, while keeping scene enabled everywhere. Verify that removing a binding actually reduces relevant model/crop work rather than merely suppressing outputs; report fixed graph costs where it does not.

At2second scene interval, N active cameras request about N/2 scene encodes, JPEGs and vectors per second before extra configured triggers. Include measured mean/p95 JPEG bytes, vector payload/metadata, package overhead, network/storage throughput, queue wait and sustained agent/Management acknowledgements. Scene cost is a per-camera floor; identity/ANPR cost also depends on people/vehicle density, qualification and improvements.

Final capacity tables must give observed failing boundary, repeatable passing point and a conservative deployment recommendation for each tested profile. Recommendations require FPS, freshness, scene coverage, complete required outputs and resource headroom together. Document chosen headroom and limitations instead of converting a brief peak into a maximum-camera promise. No single unqualified 'supports N cameras' answer.

## E. Functional production behavior and output associations

| ID | Use case/scenarios | Checks |
|---|---|---|
| E01 | Motion: quiet -> motion -> quiet, stationary person/vehicle, brief movement, camera shake, low light | Actual native votes and temporal gates; detections retain activity; safety sweep; scene active/quiet cadence; quiet is not negative hazard evidence |
| E02 | Tracking/presence: new/stable subject, boundary appearance, occlusion, ID change, two similar candidates, disappearance/recovery/unknown | No predicted boxes treated as real observations; identity-contamination safeguards; local presence continuity; no fabricated exit on loss |
| E03 | Zones: first seen inside, positive entry/exit, boundary jitter, overlapping zones/shared bindings, whole frame/no zone | Episode IDs, use-case binding, transitions and correct source geometry |
| E04 | Counts:0, increase/decrease, unchanged occupancy, occlusion, outage, count-only binding | Visible-now semantics, debounce/heartbeat, stale predictions excluded; outage is not a healthy zero or cumulative count |
| E05 | Dwell: entry, milestones/refresh, exit, short and long gaps, recovery and repeated visits | Source-time episode history and uncertainty; completion only with positive exit; Management derivation rather than inventing a separate unsupported event |
| E06 | Line crossing: both directions, finite endpoint, jitter/deadband, insufficient movement, legitimate repeat | Geometric intersection, stable observations, direction/cooldown/state and evidence IDs |
| E07 | Face: frontal/profile,small/large,blur,occlusion,no face,better later,one subject present for minutes | Pose/quality/size gates, best-window/improvement/cooldown, original contextual crop, correct transform and512D vector associations |
| E08 | Body: face absent, partial/tiny/occluded body,better later,multiple similar people | Independent qualification; original body crop;384D vector tied to correct local presence/sample group |
| E09 | Gait:0/1/many people,motion vs stationary,short sequence,occlusion,track loss/recovery,multiple sessions | Every-third-selected-frame collection; exactly one MOG update per eligible camera/frame; independent crops/history/timestamps; cache reuse/cleanup/isolation;4096D qualified output only |
| E10 | Shared-mask quality sanity and old/new diagnostic | Inspect actual person silhouettes and sequence montages, empty/fragmented/background masks, motion/usable frames and mask differences. The authorized learning change is not old-mask parity; recognition accuracy needs labelled subjects |
| E11 | ANPR: no vehicle/no plate,small/partial/skewed/blurred plate,unreadable then readable,conflicts/improvements/exit | Existing collection/selection/consensus/correction/cooldown logic; explicit unsuccessful outcomes; original vehicle/plate inputs, frame+bbox evidence and proper presence linkage |
| E12 | Scene: no detected objects,full frame/geometry,periodic/triggered/coalesced samples,quiet/sweep/pressure | Independent scene behavior, rectangular geometry crop semantics, frame+768D vector and correct time/IDs; no claim of temporal action recognition |
| E13 | Fire/smoke: positives,isolated false detections,spatial changes,qualified negative sequence,occlusion/outage | Suspected/clear temporal rules, independent cadence, no clear from unevaluated frames; this is model observation, not safety certification |
| E14 | Combined modalities and cross-camera handoff | Face/body/gait linked to correct camera/presence; global identity is Management-owned, not equality of local track/presence IDs |

Every producer check includes event type, use_case_id, geometry scope, source dimensions/bbox, timestamps, presence/episode/sample links, crop bytes, JPEG dimensions, embedding profile/dimension/normalization, reason for emission/rejection and expected frequency. Absence is classified as gate rejection, insufficient evidence, unsupported input, failed inference or failed delivery.

## F. Delivery, failures and recovery

| ID | Fault/scenario | Checks |
|---|---|---|
| F01 | Compatible agent-API receiver, complete package/byte/ack checks | User clarified that the required boundary ends at this API. Test schemas, hashes, parent links, promised images/vectors, retries and idempotency; do not equate a reference receiver's201 with production-agent database durability |
| F02 | Downstream Management ingestion and snapshot retrieval | OUT OF SCOPE under the user's explicit clarification. No endpoint/credential request or downstream deployment is required |
| F03 | Agent absent/not ready/slow/restart;socket permissions;duplicate/retry/corrupt package | Bounded admission,identical retry IDs/bytes,accepted-package completeness, useful error telemetry |
| F04 | Network outage,timeout,429/4xx/5xx,dropped response after acceptance,partial upload | Per-component retry/ack state,restart resume,duplicates,no invented success or lost promised evidence |
| F05 | Kill/restart camera worker,native group,CV container and agent at delivery boundaries | Fault scope,peer impact,durability,deduplication,cleanup and session boundaries; no host power cut |
| F06 | Disconnect one/all cameras for5/15/30seconds;restore;repeat | Time to unhealthy/recovered,peer continuity,reconnect/backoff,camera/frame/clock/cache/session correctness |
| F07 | Controlled model timeout/native failure injection and missing model | Supervisor response,blocked peers,health liveness and recovery; mock injections distinguished from real native hangs |
| F08 | Bounded disk-full/read-only/permission,CPU/memory pressure andqueue/outbox limits | Confine to test volumes/cgroups,not whole-host exhaustion;no silent loss,proper pressure/health and recovery |
| F09 | SIGTERM and abrupt container termination during active gait/evidence/handoff | Frame-cache lifetime,unfinished jobs,graceful shutdown deadline and restart consistency |

## G. Stability, reporting and stop rules

- Most functional/capacity cases use 2–3-minute runs after measured warmup, with repeated boundary points/cold starts. The user has now explicitly requested a mandatory one-hour stress test: run 60 continuous minutes of busy production workload after warmup at the selected operating point, with scene search on every camera. Run deliberate interruption/overload tests separately so they do not replace the hour of sustained operation. This supersedes the earlier no-hour-long-soak preference; it does not establish indefinite reliability.
- Track RSS/PSS/cgroup memory,native/VA/NPU allocations where observable,foreground masks,sequence/candidate buffers,FDs,threads,connections,pending jobs/packages and oldest age. Determine plateaus/growth versus accumulated observations.
- Stop runaway cases at preset time/queue/memory/disk guardrails; preserve failure evidence. The stopping thresholds are recorded before each resource test. Do not continue filling the host to find a crash point.
- Record PASS/FAIL/BLOCKED/UNSUPPORTED/NOT RUN with reasons. Use a source manifest and coverage matrix; do not claim all positive paths ran simply because bindings were enabled. Keep functional and labelled accuracy results distinct.
- Final deliverables: frozen/imported image and hashes;host/isolation record;fixtures/configs/commands;per-case logs and metric time series;stage latency/queue/memory report;per-use-case output/evidence examples;fault/recovery timeline;capacity curves by resolution/activity/FPS;known defects and operating recommendations;remote and local handoff mirrors.
- Maintain `/home/admin1/Documents/sentry-validation/` on the remote host and the existing local `OPTIMIZATION_HANDOFF.md`. Every test has an immutable run ID linking image/source/config/metrics. No new optimization or threshold edits during the measurement campaign; failures are documented before separate fixes.


## H. Mandatory instrumentation: resource use and every timing boundary

The following are separate test deliverables, sampled throughout startup, warmup, every short test, the full hour and post-stop recovery. Prefer 1-second system sampling and retain raw samples; choose/report aggregation intervals if a device counter cannot update that fast. Report per-camera and fleet p50/p95/p99/max where meaningful sample counts exist, and time-series plots. Unsupported counters are unavailable, not zero.

| ID | Measurement/test | Required detail |
|---|---|---|
| H01 | Source and decode timing | Sender frame ID/PTS/time marker, compressed arrival, decode start/completion where observable, raw decoder FPS, decoding errors; selected-FPS counter must not be labelled raw decode |
| H02 | Selection and motion timing | FPS selection cost/drops, motion evaluation cost/votes/mode, admission wait, quiet/sweep frames and time to wake/release |
| H03 | Primary model | Preprocess, request queue, GPU execution/call wall time, postprocess and metadata readiness; request concurrency distinct from batch size |
| H04 | Tracking/state | Association and real/predicted updates, age/hits, fragmentation/recovery, zone/count/line/dwell policy costs, per-frame and per-subject work |
| H05 | Face chain | ROI selection, SCRFD, alignment/AdaFace, SixDRepNet, quality/selection window, crop conversion/encoding and sample production; preserve actual input/evidence provenance |
| H06 | Body/gait | Body crop/resize/queue/model/normalization; gait MOG updates/reuses, ROI morphology, silhouette normalization, sequence accumulation, qualification, montage/model and4096D normalization separately |
| H07 | ANPR | Vehicle/plate crop,plate preprocessing/detection,OCR queue/preprocess/execution/decode,history/consensus/window/correction and final evidence; per vehicle and attempts |
| H08 | Scene/hazard | Scene sampling schedule/queue/preprocess/SigLIP2/vector/JPEG and skipped intervals; hazard preprocess/model and temporal confirmation/clear delay separately |
| H09 | Output timeline | Gate decision -> chosen source crop/frame -> JPEG -> package -> agent durable ack -> remote object acks -> actual retrieval; failed/duplicate/pending paths |
| H10 | Latency validity | Distinguish source age,queue delay,device/model latency,production latency and receipt/retrieval latency; validate clocks/PTS with independent markers; measure drift and increasing backlog |
| H11 | CPU and scheduling | Container CPU cores and % with denominator, per-process/thread CPU, run queue/context switches,system/kernel time, page faults and steal/throttle where applicable; background processes monitored |
| H12 | GPU | Compute/render/video-decode/video-processing activity,frequency,power/memory where available; node vs process attribution explicit; model-wall sums are not utilization |
| H13 | NPU | Busy counters,frequency,power/memory where exported,model request rates/queue/execution,errors; no fabricated per-camera hardware usage for a shared model pool |
| H14 | RAM and memory ownership | Host available RAM,container current/peak,process RSS/PSS/shared mappings,swap/page faults,model warmup allocations,VA surfaces/frame ring,BGR/crops,JPEGs,gait MOG state/masks/history,candidates and handoff retained bytes |
| H15 | Memory lifecycle | Measure idle -> compiled -> first frame -> steady -> increased cameras -> removed cameras -> restart -> stopped; live object/buffer counters and post-warmup growth slope; allocator-retained RSS distinguished from live leaks |
| H16 | Bounded resources | Per-model/per-camera queue depth/wait/oldest age,outstanding requests,FDs,threads,sockets,retained frames/surfaces and pending admissions; check limits and stale-result cleanup |
| H17 | Disk and network | Read/write throughput/latency,free/used space,test-volume growth,JPEG/payload bytes per camera,ingest bandwidth,connection errors,retries/acks,backpressure and drain time |
| H18 | Sampling overhead | Measure observer/metric collector CPU and storage; keep expensive traces for targeted diagnosis, not permanently enabled in capacity comparisons |

Per-camera CPU can be measured for separate workers; shared native worker CPU and accelerator activity cannot simply be copied into every camera row and summed. Provide directly measured fleet use plus model/camera work counters; label any attribution estimates. Intel UMA memory is shared system memory, not an extra independent GPU RAM total to add again.

## I. Temperature, power and thermal stability

| ID | Test | Required observations |
|---|---|---|
| I01 | Idle thermal/power baseline | Ambient if known,CPU package/core temperatures,GPU temperature,NPU sensor if actually exported,fan state,power mode and limits |
| I02 | Startup/warmup ramp | Compilation/first-frame temperature,power,frequency,memory peaks and stabilization time |
| I03 | Sustained busy load | CPU/GPU/NPU frequencies,temperatures,power/energy,thermal/power-limit flags and their correlation with per-cameraFPS/latency/queue growth |
| I04 | Burst/near-boundary pressure | Repeat load increase after the host is warm; compare cold versus hot sustainable performance without changing power/governor settings |
| I05 | Recovery/cooldown | Temperature/frequency/resource recovery after reducing/stopping load; verify no persistent degraded native/device state |
| I06 | Sensor integrity | Record sensor names/units/scope and missing counters; CPU package temperature is not an NPU temperature reading; package energy may include GPU/NPU and must not be double counted |

Use manufacturer-reported thermal/critical limits where available. Record stop margins before a stress run; do not invent one temperature ceiling for unknown hardware. Capacity must hold after reaching the actual thermal operating state, not just during a cool startup.

## J. Mandatory one-hour stress test and separate stress/recovery cases

| ID | Run | Duration/conditions |
|---|---|---|
| J01 | Full-pipeline sustained run | 60 continuous minutes after warmup at a repeatably passing busy operating point. Scene search on every camera; identity/ANPR/hazard enabled according to frozen test profile; representative active people/vehicles. Record actual work and reject an accidentally quiet fixture as full-load evidence |
| J02 | Camera-role balance under sustained load | During short qualification identify all-use-case and mixed-role passing points. J01 uses the primary deployment profile; report the other profiles as short-test-qualified unless an additional hour is actually run |
| J03 | Load ramp and bounded overload | Separate run: normal -> near boundary -> above capacity -> normal. Confirm source-age/queue pressure visibility, bounded memory and timely recovery; no silent scene reduction |
| J04 | Scene-output/delivery pressure | Scene on all cameras plus configured triggers, larger JPEG evidence volume, slow agent/remote acknowledgements; measure coalescing, promised-evidence preservation and recovery |
| J05 | High-density/identity pressure | Many people simultaneously, larger/smaller subjects, face/body/gait samples qualifying together; per-camera fairness and MOG single-update invariants maintained |
| J06 | Vehicle/OCR pressure | Many simultaneous vehicles/plate candidates,slow/conflicting OCR,queue bounds and correction/outcome delivery; no starvation of scene/identity or other cameras |
| J07 | Repeated lifecycle stress | Repeated camera disconnect/reconnect,add/remove/revision and worker/container restarts; record peer impact,session/cache/resource cleanup |
| J08 | Controlled resource restriction | CPU quota,memory cap,test-volume storage cap andnetwork throttling as separate cases with declared safe limits; never global host exhaustion |
| J09 | Shutdown and final reconciliation | Drain with bounded timeout,compare exact accepted package/object IDs against durable/remote states at one final endpoint,retrieve promised evidence; stop and inspect resource leftovers |

J01 checkpoints: baseline,after warmup,and every10minutes through60; compare per-cameraFPS/freshness,scene coverage,model work rates,p50/p95/p99,temperatures/clocks,power,RAM/buffer growth,queues,errors and delivery. Keep the complete1-second traces. The hour passes only if the selected profile remains within agreed throughput/freshness/coverage/resource rules; continuing to run is insufficient. Fault tests are separate from J01 so intentional outages do not hide sustained-performance defects. Capture failure and stop when guardrails require it; a terminated run is FAIL/ABORTED, not a completed-hour pass.

## K. Regression checks carried forward from the optimization history

These preserve prior findings rather than repeating rejected tuning or changing the frozen image. Use focused diagnostics/comparisons at selected points, with current defaults documented.

| ID | Historical concern | Required regression/diagnosis |
|---|---|---|
| K01 | Apparent16-camera capacity with almost no inference | Report decoded/selected/admitted/completedFPS,active/quiet work and emitted JPEG/vector counts together; replay historical quiet case separately |
| K02 | Source backlog behind an FPS pass | Inspect content/source-time progression,queue ages and independently marked latency; healthy/recent frame arrival alone does not pass |
| K03 | Native OpenCL resource error/missing output tensor/callback stall | Model completion/timeouts,bus errors,worker traces,supervisor recovery and impact on peer cameras; preserve native stack when obtainable |
| K04 | Per-camera model duplication / shared request pool | Count processes,instances,requests,model allocations and memory as cameras scale; verify shared execution with camera-local policy |
| K05 | Batching / asynchronous result routing | Covered regression tests and actual default batch/request settings;no old-session/out-of-order/cross-camera result applied;no enabling experimental batching just for a score |
| K06 | Leaky queues and admitted-frame loss | Account for every admission/completion/drop and timestamps;no downstream loss after tracker/policy state advanced without explicit outcome |
| K07 | TCP source fanout,second probe connection and byte-stream reconnect | Same-source and independent-source runs;decoder connection lifecycle,MPEG-TS parser errors,read timeout/reconnect;source-server limits classified separately |
| K08 | ROI conversion changed pixels/crops | Current source-coordinate and pixel/crop provenance tests;do not silently enable rejected VA ROI conversion;record selective BGR conversion cost and counts |
| K09 | JPEG/scene work reuse versus hashing overhead | Verify allowed identical-frame reuse,independent event/evidence IDs,no stale JPEG/vector across bindings;retained bytes bounded |
| K10 | Gait model invoked despite guaranteed gate rejection | Preserve history/motion updates;count actual model calls versus avoided calls;no unchanged-feature event flood |
| K11 | Full-frame morphology/thread/buffer experiments | Current local morphology correctness and preprocessing timings;no claiming old buffer/thread trials as new improvements;no changed OpenCV threading between paired capacity runs |
| K12 | Body prefetch/session tickets | Vector order,input transforms,bounded outstanding job,cancel/close behavior and job-specific timing;qualify performance without misattributing queue time to NPU utilization |
| K13 | Once-per-frame foreground change | Exact match to the new reference,per-camera/frame MOG counts,reuse ratio,original-coordinate crops,cleanup/rollback and mask/sequence sanity;old-mask differences expected under explicit authorization |
| K14 | Experimental gait worker | Record it OFF for main campaign;unit regressions retain bounded ordering/cleanup. Do not enable it or change the baseline mid-campaign |
| K15 | Stale versions/protocol/logs | Verify baked model/profile dimensions,primary class mapping,version/digest,contractV8/wireV7/endpoints,structuredcamera/session/PID/time logs and credential redaction |
| K16 | Health counter/attribution mistakes | Compare API/cgroup/native/source observations;keep health updating on camera/model/agent failure;correctly label raw decode,model wall time,node accelerator use and sharedRSS |

## L. Required final report tables

1. Tested hardware/software/image/model/schema inventory and isolation log.
2. Coverage matrix with exact case IDs and PASS/FAIL/BLOCKED/UNSUPPORTED/NOT RUN.
3. Camera capacity by resolution,FPS,source/activity anduse-case mix,with scene search on every camera.
4. Per-stage/per-use-case latency distributions and per-camera source freshness.
5. CPU/GPU/NPU/RAM/disk/network resource curves,shared-memory accounting and memory-growth analysis.
6. Temperature/power/frequency/throttling curves correlated with throughput and latency.
7. Motion-mode/cadence,scene coverage,MOG updates/reuse and emitted-artifact rates.
8. One-hour stress checkpoints,traces,pass/fail reasons and post-run cleanup.
9. Fault/reconnect/restart/delivery recovery timelines and package reconciliation.
10. Evidence/embedding association samples,known defects and supported operating recommendations with explicit headroom.

Hardware, access, sources and sensors were unverified when this plan was prepared; use the execution report for observed results. Downstream Management is outside scope. Labelled recognition accuracy remains separate. No historical shared-host result is counted as a new isolated-host pass. Credentials are never included in saved artifacts.
