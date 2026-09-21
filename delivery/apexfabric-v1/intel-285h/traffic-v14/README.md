# Sporada Secure v14 five-application contract

CV-team entry point: [`CV-PIPELINE-HANDOFF.md`](CV-PIPELINE-HANDOFF.md).

Contract files:

- `image-contract.yaml`: container, model, endpoint, resource, retention, and privacy contract.
- `desired-state.schema.json` and `desired-state.example.json`: runtime configuration.
- `analytics-event.schema.json`: embedding-free `face_seen` telemetry contract.
- `face-artifact-response.schema.json`: durable crop acknowledgement contract.
- `face-sample.schema.json`: embedding submission referencing an acknowledged crop.
- `event.examples.json`: correlated telemetry/request/response examples.

The image implements ANPR, vehicle counting, pedestrian counting, smoke/fire detection, and face recognition over RTSP, RTSPS, HTTP, or HTTPS camera sources. The runtime never assigns persistent identities. It durably uploads each face crop before submitting its embedding; the management server owns media retention, clustering, naming, merging, `person_id`, and sightings.

## Runtime implementation

- Image: `localhost/traffic-pilot-runtime:intel-285h-2026.09.21-v14`
- Build: `scripts/build_traffic_v14_layered_image.sh` (Podman only)
- Product source: `edge_runtime/solution_packs/sporada_secure/runtime_v14`
- Product map: `SPORADA_SECURE.md` at the repository root
- Base: `apexfabric-intel-traffic-runtime-base:intel-285h-2026.09.18-v2`
- Persistent mount: `/state` for the event journal, snapshots, metrics, and the bounded face-delivery outbox
- Read-only mounts: `/configs/desired_state.json` and camera `.url` files under `/run/secrets/apexfabric`
- Management secret: `APEXFABRIC_FACE_IDENTITY_TOKEN`
- Optional endpoint overrides: `APEXFABRIC_FACE_ARTIFACT_URL_TEMPLATE` and `APEXFABRIC_FACE_IDENTITY_URL`

The outbox stores one immutable crop and embedding transaction per `sample_id`.
It uploads the crop first, validates and persists the artifact acknowledgement,
then submits the embedding with the returned artifact ID and checksum. Restart
resumes at the incomplete stage. Completion removes the delivery record but keeps
the local telemetry crop so the `face_seen` snapshot URL remains valid. The shared
snapshot-retention policy bounds that local copy to 24 hours and the configured
6-8 GiB watermarks. Permanent contract failures retain metadata-only diagnostics;
transient failures retain the same IDs and bytes until retry or bounded-outbox
eviction.

The original supplied `image-contract.yaml` listed HTTP 409 as retryable while
the detailed handoff classifies it as a permanent same-ID/different-bytes error.
This delivery follows the detailed handoff and declares 409 permanent.

## Verified candidate

Local image: `localhost/traffic-pilot-runtime:intel-285h-2026.09.21-v14`

Published image:

```text
ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime:intel-285h-2026.09.21-v14
ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime@sha256:8bd59ceb40a3a5dc8e8fa4101639061848426de2b61db4beb91bfd8b893b050e
```

The host runtime suite passes 62 tests. The final live test used the camera7
RTSP source and the actual image entrypoint with Intel GPU face detection and
NPU embedding. The published build accepted a 7,255-byte face crop, simulated an embedding
service outage, restarted the container, and resumed the same sample at the
embedding stage. The artifact was uploaded exactly once, the final vector had
512 dimensions, and no SSE client was connected. Evidence is retained under
`run/traffic-v14-face-delivery-1789973382/` and the repeatable harness is
`scripts/test_traffic_v14_face_delivery_live.py`.

The production image intentionally excludes test-only `pytest`; tests run from
the source tree and live acceptance runs through the deployed entrypoint.
