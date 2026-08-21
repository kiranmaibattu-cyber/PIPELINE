# PIPELINE Edge Graph Platform

Standalone Intel edge graph project. Runtime code required from the original
RE_ID_E and Traffic Pilot projects is copied and adapted here; deployed images
do not import or mount either source repository.

## Current Images

The active ApexFabric delivery targets `linux/amd64` on Intel Core Ultra 9
285H:

```text
surveillance-edge-runtime:intel-285h-2026.08.20-v2
traffic-edge-runtime:intel-285h-2026.08.20-v2
```

Each solution image is self-contained and includes:

- graph compiler and app manifests
- headless solution runtime
- Python and native runtime dependencies
- Intel GPU/NPU userspace libraries
- model metadata and model files
- health, readiness, metrics, events, snapshot, and management APIs

Models are baked into the images at `/models/surveillance` and
`/models/traffic`. There are no runtime model downloads or model volume mounts.
The images do not require a separate edge-agent container, Redis, a Docker
socket, host networking, or a management-server callback.

## Runtime Contract

Management supplies configuration and camera credentials through mounted
Kubernetes objects:

| Container path | Source | Lifecycle |
|---|---|---|
| `/configs/desired_state.json` | ConfigMap/managed configuration | Replaced by management |
| `/run/secrets/apexfabric/<camera-id>.rtsp` | Kubernetes Secret | Replaced by management |
| `/plans` | `emptyDir` or writable temporary filesystem | Recreated with the Pod |
| `/tmp/apexfabric` | `emptyDir` or writable temporary filesystem | Recreated with the Pod |
| `/state/surveillance` | PersistentVolumeClaim | Survives restarts/upgrades |
| `/state/traffic` | PersistentVolumeClaim | Survives restarts/upgrades |

The desired state contains only `file:` references to camera Secret files.
Resolved RTSP URLs are kept in memory and are not written into plans, events, or
logs.

At startup, the same solution image:

1. validates desired state, Secret references, hardware, capacity, and baked models;
2. compiles `/plans/<solution-pack>.runtime_plan.json`;
3. launches the selected headless runtime;
4. exposes the platform API on `0.0.0.0:8080`.

Required device access:

```text
/dev/dri    Intel iGPU decode and GPU inference
/dev/accel  Intel NPU inference
```

These are device mappings, not storage volumes. The Intel image does not use a
CPU detector/decode fallback.

## Surveillance State

The surveillance PVC is mounted at `/state/surveillance`:

```text
/state/surveillance/
├── events.jsonl
├── runtime/runtime_usecases.json
├── face_gallery/
│   ├── index.json
│   ├── vectors.npy
│   └── chips/
├── reid_gallery/
├── history/
├── crops/
└── snapshots/
```

Desired state remains authoritative for cameras, apps, ROI polygons, and
counting lines. Management-owned face enrollment and face-group labels are
changed through the HTTP API and persisted in this volume.

Management does not need direct PVC access. It subscribes to `/events`, uses
the gallery/enrollment APIs, and retrieves evidence through `/snapshots/...`.

## Public API

Both images expose:

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Process/runtime liveness |
| `GET /readyz` | Plan, model, and worker readiness |
| `GET /metrics` | JSON operational metrics |
| `GET /events` | Normalized SSE analytics stream |
| `GET /snapshots/<ref>` | Persistent event evidence |

The surveillance image additionally proxies the management-facing search,
face-gallery, enrollment, face-group, and related read APIs from its internal
runtime. The management server owns the UI.

## Hardware Placement

| Pack | Stage | Device |
|---|---|---|
| Surveillance | Decode | Intel iGPU VAAPI |
| Surveillance | Person detector | Intel GPU |
| Surveillance | Body ReID | Intel NPU |
| Surveillance | Face model | Intel GPU |
| Surveillance | Gait | Intel NPU |
| Traffic | Decode | Intel iGPU VAAPI |
| Traffic | Vehicle detector | Intel GPU |
| Traffic | Plate detector | Intel NPU |
| Traffic | OCR | Intel GPU/NPU |

## Build And Test

Run the repository checks:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'
```

Build the current Intel images:

```bash
./scripts/build_apexfabric_v1_intel_images.sh
```

Run the delivery Compose example:

```bash
docker compose -f docker/docker-compose.apexfabric-v1.yml up
```

Package immutable Docker archives:

```bash
./scripts/package_apexfabric_v1_intel_images.sh
```

The delivery contracts, schemas, examples, checksums, and archive instructions
are under `delivery/apexfabric-v1/intel-285h/`. See
`SOLUTION_PACK_IMAGES.md` for the complete image contents and device placement.
