# Opt76 matched-resolution comparison

Four cameras,10FPS requested each, full use-case bindings,120s steady, sequential shared-host runs. Both fixtures derived from the same crowd recording with matching H.264 encoding settings;1440p is upscaled720p content.

| Metric |720p|1440p|
|---|---:|---:|
|Harness throughput verdict|PASS|FAIL|
|Cumulative inference FPS per camera|9.85–10.08|4.41–4.48|
|Gait processing mean ms/call|28.14–29.36|141.13–143.68|
|Final container MB|3,668|5,301|
|Reported scene production delay mean|3.19–16.60ms|33.15–34.83s|
|Evidence / embeddings|1,389 /807|912 /518|

Both remained healthy with no captured fatal lines. Healthy status does not establish timely processing. Source-time progression also fell substantially at1440p. Timing uses runtime PTS/wall anchoring, not an independent physical clock. Different detection/gate activity and shared-host load prevent attributing the complete difference solely to one operation; gait preprocessing is a major measured contributor.

One earlier720p rolling window dipped below90%; final qualifying windows/cumulative rates passed. Data is short-run workload evidence, not a maximum-capacity guarantee. Mock-agent acknowledgements do not establish real Management durability. See comparison.json for counts/timings and OPTIMIZATION_HANDOFF.md section30 for limitations and next work. No runtime changes or new build.


## 1080p follow-up (2026-11-05 IST)

| Metric | 720p | 1080p | 1440p |
|---|---:|---:|---:|
|Cumulative inference FPS/camera|9.85–10.08|6.97–7.19|4.41–4.48|
|Gait processing mean ms/call|28–29|74–76|141–144|
|Final containerGB (decimal)|3.67|4.48|5.30|
|Reported scene output delay|milliseconds|16.4–16.9s|33.2–34.8s|
|Throughput verdict|PASS|FAIL|FAIL|

Identical requested4x10FPS/fullbindings; frozenopt76. Both higherresolutions areupscaled720p content. Sourceclocksestimated; differentstagecallcounts/sharedhostload preclude pixel-onlycausalclaim.1080p capture1,200evidence713embeddings,no capturedfatalerrors,4healthy despitelag. See handoffsection31 and raw archivedreports.
