# ApexFabric V1 Intel Solution Images

This document describes the current ApexFabric V1 delivery. It targets only
`linux/amd64` on Intel Core Ultra 9 285H; Jetson Orin and Metis are excluded.

## Images

| Image | Applications | Baked models |
|---|---|---|
| `surveillance-edge-runtime:intel-285h-2026.08.20-v2` | Re-ID, face recognition, intrusion, people counting | `/models/surveillance` |
| `traffic-edge-runtime:intel-285h-2026.08.20-v2` | ANPR, wrong way, vehicle count, pedestrian count, illegal parking | `/models/traffic/openvino` |

Both images include the graph compiler, app manifests, copied headless source
runtime, Python dependencies, FFmpeg/VAAPI support, Intel GPU userspace, Intel
NPU userspace/compiler, model files, and model checksum metadata. They run as
UID/GID `10001`, require `/dev/dri` and `/dev/accel` for configured camera
workloads, and do not include either historical UI.

The fixed hardware placement remains aligned with the source pipelines:

| Pack | Stage | Device |
|---|---|---|
| Surveillance | Decode | Intel iGPU VAAPI |
| Surveillance | Person detector | Intel GPU |
| Surveillance | Body Re-ID | Intel NPU |
| Surveillance | Face model | Intel GPU |
| Surveillance | Gait | Intel NPU |
| Traffic | Decode | Intel iGPU VAAPI |
| Traffic | Vehicle detector | Intel GPU |
| Traffic | Plate detector | Intel NPU |
| Traffic | OCR | Intel GPU/NPU |

There is no CPU decode or CPU detector fallback in the V1 Intel configuration.
If required hardware is unavailable, graph compilation or runtime startup fails
instead of silently changing pipeline placement.

## Inputs

ApexFabric mounts the desired state at:

```text
/configs/desired_state.json
```

Each camera source is a `file:` reference under:

```text
/run/secrets/apexfabric/<camera-id>.rtsp
```

The compiler validates the desired state, Secret reference, available Intel
devices, capacity, and every required baked model checksum. The generated plan
retains the `file:` reference; resolved RTSP values are used only in memory by
the runtime and are excluded from generated files, events, and logs.

The same image supports the required init-container command:

```bash
python -m edge_runtime.agent.edge_agent \
  --desired-state /configs/desired_state.json \
  --output-dir /plans \
  --models-root /models
```

## Outputs

The foreground runtime listens on `0.0.0.0:8080`:

| Endpoint | Format | Purpose |
|---|---|---|
| `GET /healthz` | JSON | Process and child-runtime health |
| `GET /readyz` | JSON | Plan, model, and worker readiness |
| `GET /metrics` | Documented JSON | Runtime and event transport status |
| `GET /events` | SSE | Normalized analytics events and heartbeat |
| `GET /snapshots/<ref>` | JPEG/PNG | Event image from persistent runtime state |

The surveillance image exposes only the management-facing subset of the copied
runtime API for search, gallery enrollment/roster/group management, and related
read models. Stream creation and historical UI ownership remain outside the
image contract. Analytics events use schema version `1.0`, UTC timestamps,
unique IDs, configured camera IDs, documented application/event names, and
redacted payloads. SSE sends a heartbeat every five seconds when idle.

## Storage And Services

Models are baked into each image, so there are no model mounts or downloads.
ApexFabric supplies desired state, Secret files, device access, temporary
`/plans` and `/tmp`, a persistent volume at `/state`, and a Service for port
`8080`. The runtimes do not require Redis, a management callback, Docker socket,
Kubernetes API, or another runtime service.

For surveillance, `/state/surveillance` contains the face gallery, persistent
ReID rejoin state, history, event JSONL, crops, snapshots, and mutable face-group
labels. Management reads and changes that state through the image HTTP API; it
does not need to mount the PVC directly.

## Build And Delivery

```bash
./scripts/build_apexfabric_v1_intel_images.sh
./scripts/package_apexfabric_v1_intel_images.sh
```

Each directory under `delivery/apexfabric-v1/intel-285h/` contains the required
versioned image archive (or Git-safe parts), archive SHA-256, desired-state
schema/example, analytics schema, image contract, and README. The source contract is
`apexfabric-solution-image-contract-v1.md`.

The full surveillance versioned archive remains a local delivery artifact.
GitHub stores it as versioned `.part-aa` and `.part-ab` files because the full
archive exceeds the Git LFS per-file limit. Its delivery README contains the
exact reassembly and verification commands.
