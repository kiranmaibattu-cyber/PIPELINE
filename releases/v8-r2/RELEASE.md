# Sentry V8 r2 recovery repair

Published image: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:v8-r2`

Registry digest: `sha256:e8baec1ad054312f0081deb4bf6d34c7c8cc3bfb38516a3582d3d2fed32b4629`

The runtime could report successful branch restarts while video remained stopped. This repair keeps VA decoding and shared inference warm, reconnects the RTSP session with persistent video-pad wiring, and verifies fresh output before declaring recovery. A separate supervisor replaces native workers that cannot recover. Readiness reflects current processing; structured native errors and stage snapshots retain camera/session, process and attempt context with recursive credential redaction.

The V8 contract, models, model configuration and production quality gates are unchanged. The frozen QAFT TLSfix2 base and all 73 model/contract/schema/policy artifact hashes were compared. Nine runtime files are changed or added. Default RTSPS certificate behavior remains the previously authorized legacy-compatible mode; strict mode remains available. No deployment is restarted by publication.

Validation on the final image:

- Two independent 18-second live RTSPS interruptions recovered; the peer stayed healthy and the shared worker PID was preserved. Each interruption required its own fresh-frame recovery acknowledgement.
- An injected blocked inference callback triggered supervised worker replacement; both cameras resumed. A separate subprocess test covers native code retaining the Python GIL.
- TCP MPEG-TS regression passed. Events, evidence bytes and embeddings passed schema, digest and association validation at the capture agent API.
- 167 applicable baked runtime tests and 30 baked policy scenarios passed; canonical and embedded V8 contract validation passed. Repository-only Opt80 tests were excluded because this image retains the QAFT/Opt79 runtime.

These are recovery and compatibility results, not maximum-capacity, recognition-accuracy, production-agent durability or remote Management delivery claims. The original first BLR interruption trigger was not established by the old logs. The new diagnostics preserve stage evidence for a recurrence. Cam15 authentication requires valid source credentials separately.

See [MANAGEMENT_BLR_DECODER_STALL.md](../../MANAGEMENT_BLR_DECODER_STALL.md) for the diagnosis, log-event guide, repair behavior and retained invalid-fixture records. `REPAIR_RESULTS.json` and `registry-verification.json` distinguish the tested local image from the published registry manifest. Raw qualification metadata may still say not-published: it records the state at test time, before this release.

Build: [Dockerfile](../../docker/Dockerfile.sentinel-v8-reconnect) and [build script](../../scripts/build_sentinel_v8_reconnect_image.sh). The short tag is an alias of the tested image; its embedded version label retains `2026.11.06-v8-qaft-int8-reconnect2` for provenance. Model assets are inherited from the frozen base and remain available through the [QAFT model/source release](../../model_updates/2026-11-05-qaft-int8/RELEASE.md).

The validation download contains summaries, source snapshots, regression logs and selected native recovery diagnostics. It excludes raw package observations, camera recordings, image pixels, TLS private keys and test dependency copies. Full local logs remain in the handoff's artifact locations.
