# Sentinel CV image contract v2

This is the deployable CV image contract for the management plane defined in
`docs/Sentry_HLD.md` and implemented under `control-plane/` and `deploy/k3s/`.

The runtime reads desired state at `/configs/desired_state.json`, camera URLs
from mounted secret files, and the management bearer token from
`SENTINEL_EDGE_TOKEN`. It submits to `sentinel-ingestor` in strict dependency
order: observation, evidence, then embeddings and scene understanding.

All durable records must be written to `/state` before their first request.
Retries reuse the exact ID, JSON payload and evidence bytes. HTTP 200/201 is an
acknowledgement; HTTP 409 is a dead-letter condition rather than a retry.

Face vectors are 512-dimensional, TransReID body vectors are 384-dimensional, and flattened GaitBase part descriptors are 4096-dimensional. Scene dimensions are
declared by the registered model space; the V18 SigLIP 2 Base space is 768D.
Management must reject a vector whose dimensions, model version or embedding
space do not exactly match its registry. V2 supports gait but deliberately excludes text
until Management has matching indexed storage and search support.

Live SSE is optional telemetry only. Anything needed for history, evidence,
identity candidates or search uses the durable API.
