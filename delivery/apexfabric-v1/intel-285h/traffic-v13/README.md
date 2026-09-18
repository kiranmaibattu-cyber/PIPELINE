# Sporada Secure five-application contract

CV-team entry point: [`CV-PIPELINE-HANDOFF.md`](CV-PIPELINE-HANDOFF.md).

Contract files:

- `image-contract.yaml`: container, model, endpoint, resource, retention, and privacy contract.
- `desired-state.schema.json` and `desired-state.example.json`: runtime configuration.
- `analytics-event.schema.json`: embedding-free `face_seen` telemetry contract.
- `face-sample.schema.json`: internal embedding submission contract.
- `event.examples.json`: correlated telemetry/request/response examples.

The image implements ANPR, vehicle counting, pedestrian counting, smoke/fire detection, and face recognition. The runtime never assigns persistent face identities. The management server compares compatible embeddings, clusters unknown observations, owns `person_id`, and records sightings.

## Image

```text
ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime:intel-285h-2026.09.18-v13
ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime@sha256:8ecf7f80f99156a95f8f47f96b95cf68f453d9be1f8042937fb0b825a0aca2d7
```

The complete offline archive remains local at
`image-2026.09.18-v13.tar`. Its SHA-256 is
`5bc09d0222a25f65e88f5dc557c7712429dc891c81ef9d927caab023ff5475e4`.
The archive is intentionally excluded from Git because routine delivery uses
the layered GHCR image.

## Runtime interface

- Read-only desired state: `/configs/desired_state.json`
- Read-only camera secrets: `/run/secrets/apexfabric`
- Persistent journal, snapshots, and face outbox: `/state`
- Temporary compiled plans: `/plans`
- HTTP port: `8080`
- Health/readiness/metrics: `/healthz`, `/readyz`, `/metrics`
- Live SSE events: `/events`
- Snapshot retrieval: `/snapshots/<camera-id>/<filename>`
- Face samples: fixed authenticated `POST /internal/face-samples` management endpoint

The desired state selects `face_recognition`. A qualifying face produces a
`face_seen` analytics event with no embedding, plus a correlated internal face
sample containing the 512-dimensional vector. Both use the same event/sample
identity and reference the retained face crop. If management is unavailable,
the bounded persistent outbox under `/state` retains pending samples.

## Verification

The final image was tested with `rtsp://192.168.1.95:8554/ch9` using full-frame
face processing. The frozen run validated 797 face samples, 797 corresponding
analytics events, 1,594 snapshot assets, exact event/sample timestamp linkage,
and the declared 512-dimensional model space. SSE emitted `face_seen`, VA-API
decode used the Intel iGPU over RTSP/TCP, and the worker remained healthy.
All 44 runtime tests passed.

The final `ch9` run enabled only face recognition. The other four applications
remain present and covered by automated tests; an earlier v13 traffic run
produced 512 schema-valid events and retained worker PIDs across desired-state
hot reload. A full five-application live regression remains required before a
production rollout.
