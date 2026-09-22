# Sporada Secure v17

Build date: 2026-09-22

## Contents

- Runtime: `edge_runtime/solution_packs/sporada_secure/runtime_v17/`
- Contract: `delivery/apexfabric-v1/intel-285h/traffic-v17/`
- Podman build: `scripts/build_traffic_v17_layered_image.sh`
- Local image: `localhost/traffic-pilot-runtime:intel-285h-2026.09.22-v17`
- Local image ID: `sha256:a5cda4547d75eb071ed93da6fa997ef5ef94735056aeea2af2fa41be5e356cb8`
- Canonical image: `ghcr.io/kiranmaibattu-cyber/sporada:intel-285h-2026.09.22-v17`
- Registry digest: `sha256:8d5e0d3d9297cd60aa0d95eb34ab2f644901a5d49a18eaa56b7e0d3f3e4ba8a9`
- Offline archive: `latest-images-20260922/sporada-intel-285h-2026.09.22-v17.tar`
- Archive SHA-256: `9372e1cb5f3cda6bd7531dabc0c66075df5b04090e380c839d20b939a45d09f1`

V17 removes Management-owned detector classes from entry/exit desired state,
keeps the vehicle filter inside CV, makes `vehicle.class` open-ended and safe,
and explicitly routes crossing events only through the acknowledged multipart
outbox. It also exposes the crossing delivery metrics required by the contract.

## Verification

- `80` source and contract tests passed.
- All v17 schemas and examples validated.
- Two PIPELINE-image live runs against `traffic1` observed real `in` and `out`
  crossings across car, truck, bus, and motorcycle detections.
- Multipart JSON and evidence were acknowledged, the outbox drained to zero,
  and `vehicle_entry_exit_crossed` was absent from SSE.
- The canonical standalone image also produced 346 vehicle-count events, 346
  pedestrian-count events, one ANPR event, a durable face crop and embedding,
  a `face_seen` SSE event without a raw vector, and nine positive fire/smoke
  events from a supported HTTP stream. All outputs validated against v17.

The canonical image is published to the public GHCR package. The PIPELINE
runtime remains a source/build mirror; deployments should pull the canonical
`sporada` image above rather than publish a duplicate package.
