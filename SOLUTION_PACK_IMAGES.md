# ApexFabric Edge Images

This repository builds three Intel `intel-285h` edge images. The images contain
the application code and Intel userspace runtimes. Camera assignments, models,
generated configuration, and persistent application data are supplied through
mounted paths when a container is deployed.

## Image summary

| Image | Purpose | Platform API |
|---|---|---|
| `pipeline-edge-agent:latest` | Compiles management desired state into hardware-aware runtime plans and supervises solution-pack containers | Not exposed |
| `surveillance-edge-runtime:intel-285h` | Runs Re-ID, face recognition, intrusion, people counting, and related surveillance use cases using the copied headless 8090 runtime | Container port `8080` |
| `traffic-edge-runtime:intel-285h` | Runs ANPR, wrong-way driving, vehicle count, pedestrian count, illegal parking, and related traffic use cases using the copied Traffic Pilot OpenVINO runtime | Container port `8080` |

The build also creates local convenience tags
`surveillance-edge-runtime:latest` and `traffic-edge-runtime:latest`. ApexFabric
deployments should use the hardware-specific `intel-285h` tags or immutable
registry digests.

## Common runtime contents

The three images inherit from `pipeline-ubuntu-python:24.04`, which includes:

- Ubuntu 24.04 and Python 3.
- FFmpeg and VA-API support for Intel iGPU hardware decode.
- Intel media, OpenCL, Level Zero, and GPU userspace libraries.
- Intel NPU Level Zero userspace and the Intel NPU compiler.
- OpenVINO and the Python dependencies required by each runtime.

Only host kernel drivers and device nodes are expected from the edge box. The
platform grants `/dev/dri` for the Intel GPU/media engine and `/dev/accel` for
the Intel NPU. The container does not install drivers at deployment time and
does not require internet access at runtime.

## Edge agent image

`pipeline-edge-agent:latest` contains the platform control-plane code that runs
on an edge box:

- desired-state loading and validation;
- app manifest and dependency resolution;
- per-camera DAG graph compilation;
- hardware probing and capacity planning;
- GPU/NPU/CPU service placement;
- runtime-plan generation;
- solution-pack container command generation and supervision.

The agent reads management desired state from `/configs/desired_state.json` and
writes compiled plans to `/plans`. In production these paths are supplied by
the platform. The solution-pack images consume the resulting plans.

The edge agent does not contain camera models, galleries, or application state.

## Surveillance image

`surveillance-edge-runtime:intel-285h` contains:

- the graph-plan adapter and ApexFabric process wrapper;
- the copied headless 8090/PLATF/MTMC runtime;
- person detection and tracking orchestration;
- body Re-ID, face embedding/recognition, gait, identity fusion, intrusion,
  and people-counting runtime code;
- management event and alert snapshot generation;
- management API proxying for search, persons, history, Re-ID/face galleries,
  face enrollment, alerts, counting, live view, cameras, zones, and use cases.

Default accelerator placement preserves the 8090 pipeline assignment:

| Stage | Device |
|---|---|
| Video decode | Intel iGPU/media engine |
| Person detector | Intel GPU |
| Body embedding | Intel NPU |
| Face model | Intel GPU |
| Gait embedding | Intel NPU |
| Segmentation | Intel GPU |

### Surveillance mounts

| Container path | Access | Contents |
|---|---|---|
| `/plans/surveillance.runtime_plan.json` | Read-only input | Cameras, apps, graph services, revision, and edge ID |
| `/models/surveillance` | Read-only input | OpenVINO detector, body Re-ID, face, gait, and segmentation model files |
| `/generated/surveillance` | Read-write | Generated streams and use-case runtime configuration |
| `/state/surveillance` | Read-write, persistent | Face gallery, Re-ID gallery, history, crops, snapshots, and event outbox |

Models are not baked into the image. The required model volume must contain the
matching `.xml` and `.bin` files before cameras are assigned.

### Surveillance management interface

| Endpoint | Format | Purpose |
|---|---|---|
| `/healthz` | JSON | Process liveness; independent of camera health |
| `/readyz` | JSON | Plan/runtime readiness; zero-camera configuration is valid |
| `/metrics` | JSON | Runtime, camera, event, snapshot, and management API status |
| `/events` | Server-Sent Events | Alerts and analytics metadata with snapshot references |
| `/snapshots/<ref>` | JPEG/PNG | Alert snapshot or crop stored in the mounted state volume |
| `/api/...` | JSON/image | Proxied 8090 search, gallery, enrollment, history, identity, and use-case APIs |

Management should store camera credentials in a Secret and put an `env:`,
`file:`, or `secret:` source reference in the runtime plan. Face and Re-ID
gallery updates made through the management API persist under
`/state/surveillance`.

## Traffic image

`traffic-edge-runtime:intel-285h` contains:

- the graph-plan adapter and ApexFabric process wrapper;
- the copied Traffic Pilot OpenVINO runtime without the Metis execution path;
- VA-API/FFmpeg hardware decode;
- OpenVINO vehicle detection, plate detection, and OCR;
- tracking and traffic analytics for ANPR, wrong-way driving, vehicle count,
  pedestrian count, and illegal parking;
- local management event and alert snapshot generation.

The current Intel path uses the iGPU media engine for decode, the Intel GPU for
the primary vehicle detector, and graph-plan-selected GPU/NPU placement for
plate detection and OCR.

### Traffic mounts

| Container path | Access | Contents |
|---|---|---|
| `/plans/traffic.runtime_plan.json` | Read-only input | Cameras, apps, graph services, revision, and edge ID |
| `/models/traffic/openvino` | Read-only input | Vehicle, license-plate, and OCR OpenVINO `.xml`/`.bin` files |
| `/generated/traffic` | Read-write | Generated camera and worker configuration |
| `/state/traffic` | Read-write, persistent | Rules, events, snapshots, crops, videos, and runtime history |

Models are mounted and are not baked into the traffic image.

### Traffic management interface

| Endpoint | Format | Purpose |
|---|---|---|
| `/healthz` | JSON | Process liveness; independent of camera health |
| `/readyz` | JSON | Plan/runtime readiness; zero-camera configuration is valid |
| `/metrics` | JSON | Runtime, event, and snapshot status |
| `/events` | Server-Sent Events | ANPR and traffic analytics events |
| `/snapshots/<ref>` | JPEG/PNG | Event image stored in the mounted state volume |

## Deployment example

The ApexFabric scheduler supplies equivalent mounts and device access. The
repository's `docker/docker-compose.yml` is intended for local edge testing.

```text
management desired state
        -> edge agent
        -> compiled runtime plan
        -> solution-pack container
        -> cameras and mounted models
        -> events, metrics, snapshots, galleries, and API results
        -> management server
```

Build all images with:

```bash
./scripts/build_images.sh
```

Set `CONTAINER_ENGINE=podman` when Podman should be used instead of Docker.

After authenticating the selected container engine to GHCR, publish all three
images with:

```bash
./scripts/push_images.sh
```

The publish script verifies the repository source label and forces Docker
schema v2 when using Podman so GitHub links these packages to the `PIPELINE`
repository. The published image references are:

```text
ghcr.io/kiranmaibattu-cyber/surveillance-edge-runtime:intel-285h
ghcr.io/kiranmaibattu-cyber/traffic-edge-runtime:intel-285h
ghcr.io/kiranmaibattu-cyber/pipeline-edge-agent:latest
```

## Current contract limitations

The current images implement the ApexFabric endpoint and mount contract, but
the following items still require production hardening:

- Pin the Ubuntu base image by digest and lock all apt/pip dependency versions
  for fully reproducible builds.
- Move the remaining surveillance runtime config/symlink writes out of
  `/opt/pipeline` and into mounted generated/state paths.
- Split durable gallery/config storage from reconstructable snapshot/cache
  storage into separate platform volumes.
- Enforce Secret references by rejecting credential-bearing camera URLs in
  runtime-plan files.
- Make readiness depend on successful model compilation, accelerator access,
  and camera pipeline initialization rather than only child-process state.
- Benchmark the GPU/NPU-only images before treating the declared 16-camera
  surveillance and 26-camera traffic values as qualified capacity.
