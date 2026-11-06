# Isolated .64 image validation — ongoing

Snapshot UTC: 2026-11-05T21:37:24.355089+00:00. Remote evidence: `/home/admin1/Documents/sentry-validation-v8-tlsfix2-20261105`. This report is generated from saved measurements; it is not a claim that all282 catalog scenarios passed.

The candidate is `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-tlsfix2`, pulled by registry digest `sha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc`. Its runtime, models, production gates and V8 contract were frozen throughout this campaign. Explicit old-control cases use the cached Opt79 image; the CSV records the image for every row. No image rebuild or push was performed.

Current state: `running-native-case`, case `SX-mixedPV-6x8-budget`. Completed native measurement rows: 87. Phase return code0 means the phase runner completed, not that every test passed.

**REMOTE CONTACT LOST:** this is an offline report from the last verified21:31UTC snapshot. LAN SSH/ping to .64 failed around21:32UTC while the local gateway remained reachable. Later test progression, host condition and power/process cleanup are unverified. This is not proof that the image caused the loss. The requested sustained tests are not complete in the available evidence.

## Camera capacity: how to read the numbers

All cameras in the performance matrix have scene search enabled. These are the largest observed **short** passing points on the common crowded1920×1080,30-source-FPS encoded replay, with5/8/10selectedFPS per camera. They are neither hardware maxima nor production-qualified capacities. A failed point is not necessarily monotonic: output admission and startup failures can vary between runs.

| Requested FPS per camera | Scene only: largest short pass | All applications: largest short pass | Sustained qualification |
|---:|---:|---:|---|
|5|6|4|See pre-soak/hour results; global correctness failures remain open|
|8|6|3|See pre-soak/hour results; global correctness failures remain open|
|10|6|2|See pre-soak/hour results; global correctness failures remain open|

Pass requirements used for short marked tests: every camera reaches at least95% cumulative selected/completed/busy inference FPS, at least90% in each measured interval, selection no more than105% of target, independently marked scene age p95≤2s and p99≤5s, scene cadence at least90% of2-second demand with no gap above5s, healthy ending and no native/resource-abort errors. No single aggregate FPS value can hide a slow peer. Quiet-motion cases use different assertions.

Independent latency starts when the controlled TCP publisher transmits the first video PES packet for the marked source frame and ends at complete local agent-API acceptance. It excludes camera capture/encoding and downstream agent durability. Separate replay-progress analysis detects publisher backpressure before transmission. UTC event age is recorded signed; negative values are not clamped into a passing latency.

## Test conditions and resource scopes

Host: Intel Core Ultra9 285H,16 logical CPUs, about31.6GB RAM,no swap. The user-authorized competing blackbox/GPU observer processes were stopped with restart metadata preserved; essential host services stayed running. CV runs non-root10001 with explicit GPU/NPU device mounts. Tests use root Docker as launcher, not a Kubernetes device-plugin qualification.

Stock startup reached105°C and was aborted/excluded. Measured runs use recorded no-turbo,PL1=45W,PL2=65W. Startup CPU quota4cores is released to16 before steady measurement. These conditions must accompany any comparison; they are not unrestricted-stock numbers. Original power settings are restored by final cleanup.

Container cgroup memory is MiB, CPU is cores. Shared process RSS must not be summed once per camera. GPU engine busy from intel_gpu_top is node-scoped and separate from GT active residency. NPU utilization is also node-scoped. Stage wall time/queue wait is not accelerator utilization and overlapping stages cannot be summed into end-to-end latency. Full per-call Python-stage latency distributions cannot be reconstructed from lifetime averages alone.

## Known failures reproduced in this campaign

| Finding | Evidence and implication |
|---|---|
| Future event UTC | Events appeared7.5–11.18seconds ahead of local API acceptance despite fresh marked images and synchronized host clock. The image cannot be qualified for source event-time correctness. |
| Source Secret replacement | Parent revision2 and unchanged peer updated, but changed camera kept revision1/1920×1080 scene output instead of verified replacement1280×720 source. Ordinary revision updates separately passed. |
| Unwritable metrics storage |25 PermissionErrors and camera-thread exceptions; an observability write failure affected camera execution. |
| Rejected feature admission | Face/body/gait selection state advances despite refused handoff, suppressing a later identical qualified sample. Deterministic baked-producer diagnostic reproduced. |
| Nested log redaction | Synthetic credential/token error-path diagnostic failed. This does not establish that actual production credentials leaked. |
| Native startup abort | Five-camera all-application startup produced stack-smashing/longjmp errors. Root cause unknown; not proof of GPU exhaustion or a physical four-camera ceiling. |
| Scene admission/cadence | Some points sustained frame FPS but skipped/delayed scene output. Scene-only8×5 saw14 handoff backpressure refusals. |
| Device diagnostics | Missing-GPU/NPU cases stopped analytics with0 successful frames; expected explicit device cause was obscured by a generic secondary error. No successful CPU fallback observed. |
| Agent outage exhaustion |503/429 fault windows each ended with4 exhausted handoffs. Durable-accepted, duplicate, coalesced, exhausted and pending counts must be separated. |

The existing HTTP/HTTPS native-path acceptance mismatch, inactive experimental dynamic batching, and legacy adaptive-association/native-GVA distinction remain limitations; this campaign does not silently enable or repair them. Recognition/plate/identity accuracy is outside the current execution qualification.

## Reviewed functional and API controls

| Control | Verdict | Scope |
|---|---|---|
|SP-control-secret_url_rotation|FAIL|Higher revision2 applies new source; replacement publisher has verified MPEGTS data and1280x720 frames.|
|SP-control-config_updates|PASS|Ordinary higher-revision changes, invalid/same-revision rejection, Secret readability recovery and endpoints; source replacement and projected Kubernetes symlink semantics are separate.|
|SP-control-camera_membership|PASS|Two-to-one-to-two camera membership and corresponding output revisions; not arbitrary50-camera churn.|
|SP-control-kill_worker|PASS|See control-semantic-review.json|
|SP-control-api_reconciliation|PASS|See control-semantic-review.json|
|SP-control-state_readonly|FAIL|See control-semantic-review.json|

Native quiet/motion, ANPR read and unreadable output,720p/1440p source processing, hazard suspect/clear, live RTSPS, untrusted encrypted media and bad-first/bad-last peer cases have captured execution evidence. Output presence establishes execution only. NULL/unreviewed scenario results are not PASS.

The API receiver validates accepted complete packages, schemas, declared hashes, JPEG/vector bytes and IDs. Lost-ACK retries yielded144 duplicate ACKs with0 protocol errors; duplicate ACKs are not additional unique packages. Normal common-cutoff reconciliation passed. The receiver represents the user-agreed agent API boundary; real agent disk durability and downstream Management are not qualified.

Saved-gallery audit:95 sampled packages,85 JPEGs,81 vectors; face512D/body384D/gait4096D/scene768D with unit normalization passed. It is a sample, not every artifact or recognition accuracy. Runtime regression96passed with1Opt80-only deselection; producer scenarios30passed; native TLS matrix15passed; reference receiver checks8passed. Configuration validation63profile/FPS combinations establishes schema validity only.

## Fixed-input use-case budget

Budget tests reuse one120-second1080p30 fixture:60seconds crowded people then60seconds vehicle footage, with CRC frame markers. The vehicle segment is transformed from720p10FPS, not a naturally captured30FPS scene. Compare profiles at the same camera count/FPS. This fixed-count test is not a maximum-camera search for every profile. Enabled bindings are not proof that a positive hazard or useful face/gait sample existed.

| Case | Worst selected / inferred FPS | Worst scene p95 ms | RAM MiB | CPU cores | Narrow result / failure | Positive paths not observed |
|---|---:|---:|---:|---:|---|---|
|S-mixedPV-6x5-budget|4.99/4.89|387|2025|1.76|short pass|none in listed checks|
|SA-mixedPV-6x5-budget|4.30/4.27|8947|5833|3.91|FPS, busy inference, scene age, scene cadence|hazard_clear_observed, hazard_suspected_observed, vector:gaitbase-v18.1|
|SP-mixedPV-6x5-budget|4.99/4.89|366|2006|1.85|short pass|none in listed checks|
|SC-mixedPV-6x5-budget|5.00/4.89|346|1991|1.78|short pass|none in listed checks|
|SX-mixedPV-6x5-budget|5.00/4.89|374|1999|2.03|short pass|none in listed checks|
|SIF-mixedPV-6x5-budget|5.00/4.91|437|2727|2.12|short pass|none in listed checks|
|SIB-mixedPV-6x5-budget|5.00/4.89|461|2191|2.00|short pass|none in listed checks|
|SIFB-mixedPV-6x5-budget|5.00/4.91|473|2811|2.12|short pass|none in listed checks|
|SIFBG-mixedPV-6x5-budget|5.00/4.89|475|4181|2.42|short pass|vector:gaitbase-v18.1|
|SV-mixedPV-6x5-budget|4.99/4.90|3188|3378|2.69|FPS, busy inference, scene age|none in listed checks|
|SVX-mixedPV-6x5-budget|5.00/4.89|3833|3383|2.75|FPS, busy inference, scene age|none in listed checks|
|SH-mixedPV-6x5-budget|5.00/4.92|437|2456|2.09|short pass|hazard_clear_observed, hazard_suspected_observed|
|M-identity-25-mixedPV-6x5-budget|—/—|—|—|—|assessment incomplete|vector:adaface-ir101-v18.1, vector:gaitbase-v18.1, vector:siglip2-base-v1, vector:transreid-ssl-v18.1|
|M-identity-50-mixedPV-6x5-budget|4.99/4.88|410|3518|2.09|short pass|vector:gaitbase-v18.1|
|M-identity-75-mixedPV-6x5-budget|5.00/4.90|448|3708|2.26|short pass|vector:gaitbase-v18.1|
|M-vehicle-25-mixedPV-6x5-budget|4.99/4.89|533|2690|2.30|short pass|none in listed checks|
|M-vehicle-50-mixedPV-6x5-budget|5.00/4.89|726|2888|2.30|short pass|none in listed checks|
|M-vehicle-75-mixedPV-6x5-budget|5.00/4.89|914|3128|2.45|short pass|none in listed checks|
|M-balanced-mixedPV-6x5-budget|5.00/4.91|1152|4045|2.66|short pass|hazard_clear_observed, hazard_suspected_observed, vector:gaitbase-v18.1|
|S-mixedPV-6x8-budget|5.38/5.25|505|1999|2.18|FPS, busy inference, scene cadence, final health|none in listed checks|
|SA-mixedPV-6x8-budget|5.36/5.33|16138|5753|4.10|FPS, busy inference, scene age, scene cadence|hazard_clear_observed, hazard_suspected_observed|
|SP-mixedPV-6x8-budget|7.99/7.88|1033|2030|3.08|short pass|none in listed checks|
|SC-mixedPV-6x8-budget|8.00/7.89|461|1994|2.85|short pass|none in listed checks|

S=scene only;SP=scene+presence;SC=scene+counts;SX=presence/count/line roles;SIF/SIB/SIFB/SIFBG=face/body combinations;SV/SVX=vehicle/ANPR roles;SH=hazard;SA=all applications. M profiles assign expensive roles to a fraction of cameras; see each camera-role-map for actual integer fractions.

## Sustained run and cleanup

Pre-soak/hour phase has not completed. A failing ten-minute operating point is not promoted to an hour.

Power restoration recorded: **False**. The competing blackbox application remains stopped under the isolation instruction; its original restart metadata is saved. No automatic production restart is claimed.

Completed current-campaign budget/soak galleries may be relocated to compute-a only after every file SHA256 matches. EVIDENCE_RELOCATED.json records original case and verified destination; other remote logs/measurements remain. No old campaigns were deleted.

Full measurement rows: [REMOTE64_CAPACITY_RESULTS.csv](REMOTE64_CAPACITY_RESULTS.csv). Current machine-readable evidence: `optimization_evidence/2026-11-05-remote64-tlsfix2/`; original logs, commands, fixtures and actions remain at the remote evidence root. The282-case planning catalog is broader than the implemented campaign; unexecuted or unasserted cases must remain visible rather than being inferred from output counts.


## Recovery update — 2026-11-06T04:48:08.395321+00:00

`.64`isreachableafterrestart.Userconfirmedthehostfroze.Testsremainstopped;originalpowerresetvaluesverifiedafterreboot.The778-fileinterruptedcaptureisverifiedlocally;fullremainingarchivecopyisinprogressonaresumableuserservice.Theoldcontact-lost/cleanup-unverifiedparagraphsdescribetheearliercheckpoint.See[thecurrentfreeze review](REMOTE64_HOST_FREEZE_REVIEW.md)forevidenceandlimits.
