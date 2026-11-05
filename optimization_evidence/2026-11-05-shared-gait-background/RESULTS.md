# Opt79: one background update per gait-sampling frame

The user explicitly authorized a change from per-person background learning to one update per camera/frame. Each person crops the same foreground mask and retains its own motion history, sequence and event gates. Full-resolution source coordinates/evidence are retained. Cache identity includes camera, session/frame, shape/dtype and background model; frame-end cleanup releases it. No desired-state/schema changes or model replacements. The earlier gait worker remains disabled.

| Test (four cameras) | Opt76 | Opt79 | Opt79 verdict |
|---|---:|---:|---|
|1440p,10FPS requested|4.41–4.48 inferenceFPS/camera|7.33–7.52|FAIL target|
|1080p,10FPS requested|6.97–7.19|9.78–10.34 cumulative|FAIL rolling-window criteria|
|1080p,8FPS requested|Not rerun at8|7.85–8.19|PASS short test|

Above-target cumulative readings reflect snapshot alignment/buffering. Runs were sequential on a shared host with upscaled recordings; these are workload measurements, not causal speedup guarantees or maximum capacity.1440p output latency improved but still accumulated around15seconds in reported source-clock estimates.1080p10FPS reduced delay substantially but did not consistently pass sampled-window criteria.

75 runtime tests passed; canonical and embedded V8 validators passed. New tests exactly match a once-per-frame reference and verify camera/session/shape/model isolation, rollback and error cleanup. Actual1440p counters record286–294background updates/camera and679–703reuses, avoiding about70% of background-update opportunities. A diagnostic on150constructed normalized regions found149changed versus legacy, so this is not exact old-input parity or proof of recognition accuracy. Gates remain unchanged, but changed masks can change usable sequences/vectors.

Original-frame evidence and face/body/gait/scene outputs were produced. The8FPS run captured849evidence objects and506embeddings, including16gait. Temporary handoff backpressure occurred; no exhausted retries/protocol errors. Capture uses a mock agent, not real Management retrieval/durability. No push; old images retained. See handoff section32 for full results, image ID and limitations.
