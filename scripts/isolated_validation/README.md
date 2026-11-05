# Isolated Opt79 validation tools

Campaign completed 2026-11-05. Read ../../ISOLATED_VALIDATION_REPORT.md and the final handoff checkpoint. Controllers are one-time campaign tools, not currently running services; do not restart them against the completed ledger. Reproduce selected cases using the recorded commands and frozen conditions.

These are test controllers and offline diagnostics, not runtime changes. The frozen image is imported unchanged. No script in this directory is copied by Dockerfile.sentinel-v8.

The active target is /home/admin1/Documents/sentry-validation on192.168.1.64. campaign.py performs sequential capacity, functional, API fault, source-outage, live-input and60-minute tests. extended_campaign.py waits for that phase and then performs the remaining checks. Read STATUS.json, ledger.json and per-case logs; a process existing is not a completed test. Do not rerun transition_driver.py: it is a historical one-off checkpoint helper.

All deployment profiles retain scene search. Profile generation filters scene triggers to bindings that remain enabled; --dump-desired permits validation before launching. allow_abbrev=False preserves --recording forwarding. Invalid historical profiles and attempt1 source-selection errors remain archived and excluded from capacity.

The root observer samples at1Hz. intel_gpu_top and turbostat are additional independent observers. GPU GT non-RC6 residency is not engine compute utilization. The health field named decoded_fps is selected FPS; native accepted+dropped PTS-gate counters allow a separate raw decode estimate. Legacy memory fields named mb divide by1024², so report them asMiB. Window averages/maxima do not establish per-kernel latency percentiles.

The reference receiver checks V8 package/wire schemas, image sizes/hashes and parent links before acknowledgement. It tests the CV-to-agent API boundary; it does not establish production-agent SQL durability or downstream Management behavior. HTTP401 is an unexpected-status test, not a new authentication requirement. prepare_error_payloads.py keeps error bodies inside the contract schema. API common-endpoint reconciliation pauses producers, drains the scheduler and compares unique package/object IDs and sequences before resuming.

Marker tests use pre-encoded frame-ID/checksum pixels, source video-PES PTS/send timestamps and agent receipt timestamps. The hour uses marked crowd1080p30 and vehicle720p10, with all scene JPEGs captured and other gallery categories sampled. prepare_hour_marker_driver.py updates only the test sender/receiver. Repeated120-second markers are aligned in camera/session/frame order; long ambiguous gaps remain unavailable measurements. This measures first-video-PES-byte-to-agent acceptance, excluding physical camera capture/encoding and work after the agent API.

Stock-power cold starts repeatedly tripped the preset thermal guard. The a4 series is explicitly conditional on CPU turbo disabled, PL2=65W and sustained PL1=45W. Original settings and restoration values are in inventory/. Do not merge these results with stock-power results. Restore temporary host settings after the campaign. Do not change production gates or rebuild the image during comparisons.

Kubernetes checks are post-performance only, on a disposable data directory/API port, with privileged device access and a reference receiver sidecar. They do not claim production device-plugin/nonprivileged deployment qualification. Never point the helper at the user's original kubeconfig.

Keep raw evidence remote if local disk space is insufficient. Generate analysis/plots and the final coverage report only from completed artifacts, preserve failed tests, and distinguish invalid test inputs, unsupported inputs, expected fault outcomes and actual runtime failures.
