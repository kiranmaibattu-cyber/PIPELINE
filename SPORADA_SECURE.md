# Sporada Secure

`Sporada Secure` is the product name and `sporada-secure` is the solution-pack
identifier used in desired state, events, contracts, and the runtime graph.

## Current release

- Runtime source: `edge_runtime/solution_packs/sporada_secure/runtime_v17/`
- Delivery contract: `delivery/apexfabric-v1/intel-285h/traffic-v17/`
- Workload Dockerfile: `docker/Dockerfile.traffic-v17`
- Build script: `scripts/build_traffic_v17_layered_image.sh`
- Local mirror: `localhost/traffic-pilot-runtime:intel-285h-2026.09.22-v17`
- Published image: `ghcr.io/kiranmaibattu-cyber/sporada:intel-285h-2026.09.22-v17`
- Published digest: `sha256:8d5e0d3d9297cd60aa0d95eb34ab2f644901a5d49a18eaa56b7e0d3f3e4ba8a9`
- Verification: `SPORADA_V17_RELEASE.md`

V17 is archived locally and published as an anonymously readable GHCR image.

## V18 Candidate

The isolated v18 source is under
`edge_runtime/solution_packs/sporada_secure/runtime_v18/`, with contract
`delivery/apexfabric-v1/intel-285h/traffic-v18/`. It adds bounded best-face
selection with fallback while keeping v17 immutable. See
`SPORADA_V18_RELEASE.md` and `SPORADA_V18_FACE_SELECTION.md`.

The runtime directory is a product-owned snapshot. The v17 Dockerfile does
longer reads application code from the older shared
`edge_runtime/solution_packs/traffic/runtime_v11/` path.

## Shared versioned dependencies

- `models/traffic-v11/openvino/`: INT8 YOLO26n person/vehicle detector, plate
  detector, OCR, and smoke/fire models. The person/vehicle model runs on the
  Intel GPU and retains the `[1,3,640,640] -> [1,300,6]` detector contract.
- `models/traffic-v11/face/openvino/`: face detector and embedding models.
- `apexfabric-intel-traffic-runtime-base:intel-285h-2026.09.18-v2`: Ubuntu,
  Intel GPU/NPU userspace, FFmpeg, Python, and OpenVINO dependencies.

These dependencies are copied into the workload image during the build. There
is no runtime link to `/home/admin1/traffic-pilot-main` or another source
repository.

## Deployment inputs

- `/configs`: desired state.
- `/run/secrets/apexfabric`: camera `.url` Secrets.
- `/state`: persistent events, snapshots, metrics, and face-delivery outbox.
- `/dev/dri` and `/dev/accel`: Intel GPU and NPU devices.

Successfully delivered face crops remain under `/state/snapshots` so SSE event
URLs continue to resolve. They are telemetry copies governed by the common
24-hour, 6-8 GiB snapshot-retention policy; management owns the durable biometric
artifact delivered before the embedding.
