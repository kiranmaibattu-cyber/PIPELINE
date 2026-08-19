# PIPELINE Edge Graph Platform

Standalone edge graph project. It does not import from `RE_ID_E` or
`traffic-pilot`; runtime code needed by an edge image is copied/adapted here.

## Phase 1

This phase builds the control plane inside the edge box:

- app manifests for surveillance and traffic solution packs
- desired-state loader
- camera graph compiler
- hardware probe for CPU/GPU/NPU
- planner that writes one runtime plan per solution pack
- Docker scaffolding for edge agent, surveillance runtime, and traffic runtime

Run locally:

```bash
PYTHONPATH=. python -m edge_runtime.agent.edge_agent \
  --root "$PWD" \
  --host-root "$PWD" \
  --desired-state configs/desired_state.example.json \
  --output-dir run/plans
```

Outputs:

```text
run/plans/compiled_graph.json
run/plans/management_api_tags.json
run/plans/surveillance.runtime_plan.json
run/plans/traffic.runtime_plan.json
run/plans/management_outbox.jsonl
```

`management_api_tags.json` is the phase-1 management contract. It tags every
camera input, per-app analytics output, optional frame/snapshot output, camera
metric stream, service health stream, and runtime metric stream with:

```text
api_version
edge_id
revision
solution_pack
camera_id/app_id/service when applicable
direction
payload_type
tag
topic
```

The management server can later subscribe/post by these tags instead of knowing
8090 or Traffic Pilot internals.

Generate solution-pack runtime config locally:

```bash
PYTHONPATH=. python -m edge_runtime.solution_packs.surveillance.runtime.main \
  --plan run/plans/surveillance.runtime_plan.json \
  --output-dir run/generated/surveillance

PYTHONPATH=. python -m edge_runtime.solution_packs.traffic.runtime.main \
  --plan run/plans/traffic.runtime_plan.json \
  --output-dir run/generated/traffic
```

Generated surveillance files:

```text
run/generated/surveillance/streams.generated.yaml
run/generated/surveillance/runtime_usecases.generated.json
run/generated/surveillance/camera_features.generated.json
```

Generated traffic files:

```text
run/generated/traffic/cameras.generated.json
```

Prepare the copied headless runtimes:

```bash
PYTHONPATH=. python -m edge_runtime.solution_packs.surveillance.runtime_8090.launch \
  --plan run/plans/surveillance.runtime_plan.json \
  --generated-dir run/generated/surveillance_8090 \
  --state-dir run/state/surveillance \
  --models-dir /models/surveillance \
  --prepare-only

PYTHONPATH=. python -m edge_runtime.solution_packs.traffic.runtime_pilot.launch \
  --plan run/plans/traffic.runtime_plan.json \
  --generated-dir run/generated/traffic_pilot \
  --state-dir run/state/traffic \
  --models-dir /models/traffic/openvino \
  --prepare-only
```

Or run the full local validation path:

```bash
PYTHONPATH=. python -m edge_runtime.agent.local_validate --root "$PWD"
PYTHONPATH=. python -m unittest discover -s tests
```

Apply the graph to runtime containers:

```bash
# default is safe dry-run; it prints docker stop/start commands
PYTHONPATH=. python -m edge_runtime.agent.edge_agent \
  --root "$PWD" \
  --desired-state configs/desired_state.example.json \
  --output-dir run/plans

# on a Docker-capable edge box, actually restart required solution-pack containers
PYTHONPATH=. python -m edge_runtime.agent.edge_agent \
  --root "$PWD" \
  --host-root "$PWD" \
  --desired-state configs/desired_state.example.json \
  --output-dir run/plans \
  --apply
```

If the edge agent itself runs inside a container, `--root` is the path inside the
agent container and `--host-root` is the same PIPELINE directory as seen by the
host container engine.

## Design Rule

Apps do not own cameras. Apps require data products. The graph compiler expands
those requirements, shares common producers, and lets the planner place services
on available hardware.

## Docker Images

Phase 1 defines three images:

```text
pipeline-edge-agent
surveillance-edge-runtime
traffic-edge-runtime
```

The edge agent writes runtime plans to `/plans`. The solution-pack images read
their plan and generate runtime-specific config under `/generated`.

The production UI belongs to the management server, not these edge images. The
old 8090 and Traffic Pilot dashboards are not copied into the image contracts.

`surveillance-edge-runtime` contains:

- common graph/runtime support from `edge_runtime`
- surveillance app manifests
- copied headless 8090 runtime source under
  `edge_runtime/solution_packs/surveillance/runtime_8090`
- launcher that writes 8090-compatible stream/use-case config and starts
  `PLATF.app`

`traffic-edge-runtime` contains:

- common graph/runtime support from `edge_runtime`
- traffic app manifests
- copied Traffic Pilot all-OpenVINO worker under
  `edge_runtime/solution_packs/traffic/runtime_pilot`
- launcher that writes Traffic Pilot `cameras.json` plus OpenVINO-only
  `worker.json`, then starts `stream_fleet_openvino.py`

Not baked into the images:

- model weights: mounted under `/models/surveillance` or `/models/traffic/openvino`
- runtime state/galleries/history: mounted under `/state/...`
- old app UIs
- old datasets, demo videos, reports, caches, or `.venv`

Build images on a Docker-capable edge box:

```bash
./scripts/build_images.sh
```

## External Model Bundles

Option 1 is the active deployment model:

```text
Docker image = runtime code + dependencies
Model bundle = external files under PIPELINE/models
State/gallery/history = external persistent volume under PIPELINE/state
```

The model registry lives at:

```text
edge_runtime/model_registry/models.yaml
```

It records the model id, version, expected runtime mount, file list, and SHA-256
checksums. The edge agent validates only the models required by the compiled graph
before it starts or prints container commands.

Management can deliver an immutable model archive through desired state. The
archive must contain the contents of one solution-pack model directory at its
root, not an additional `surveillance/` or `traffic/` parent directory:

```json
"model_bundles": {
  "surveillance": {
    "version": "surveillance-intel-v1",
    "url": "https://management.example/api/v1/model-bundles/surveillance-intel-v1.tar.gz",
    "sha256": "<64-character archive SHA-256>",
    "archive_format": "tar.gz",
    "auth_token_env": "PIPELINE_MODEL_TOKEN"
  }
}
```

The edge agent downloads the archive, verifies its archive checksum, safely
extracts it into an immutable versioned cache, verifies every graph-required
model against `models.yaml`, and only then starts the runtime. The activated
bundle is mounted at `/models/<solution_pack>:ro`. A failed download, checksum,
extraction, or required-model check prevents that revision from starting.

Create an archive for upload to the management model API with:

```bash
./scripts/package_model_bundle.sh surveillance intel-v1 dist/model-bundles
```

The management API stores both generated files, serves the `.tar.gz` URL, and
places the digest from `.sha256` in desired state. Authentication tokens are
provided to the edge agent through the named environment variable; they are not
stored in desired-state JSON.

For a host-run edge agent:

```bash
PYTHONPATH=. python -m edge_runtime.agent.edge_agent \
  --root "$PWD" \
  --host-root "$PWD" \
  --models-root "$PWD/models" \
  --desired-state configs/desired_state.example.json \
  --output-dir run/plans
```

For a containerized edge agent, mount a writable model cache and provide both its
agent-visible path and the same directory's host-visible path. The latter is used
when the agent asks the host Docker/Podman daemon to start a solution runtime:

```bash
podman run --rm --network=host \
  --device /dev/dri:/dev/dri \
  --device /dev/accel:/dev/accel \
  -v "$PWD/configs/desired_state.example.json:/configs/desired_state.json:ro" \
  -v "$PWD/models:/models-cache" \
  -v "$PWD/run/plans:/plans" \
  pipeline-edge-agent:latest \
  --root /opt/pipeline \
  --host-root "$PWD" \
  --models-root /models-cache \
  --host-models-root "$PWD/models" \
  --desired-state /configs/desired_state.json \
  --output-dir /plans
```

See `configs/desired_state.management-models.example.json` for the complete
management-delivery schema. Existing desired states without `model_bundles` keep
using local `models/<solution_pack>` directories for development and migration.

Install Docker on Ubuntu when running from a root-capable shell:

```bash
sudo ./scripts/install_docker_ubuntu.sh
```

This current shell may not be root-capable even if `sudo` exists. In that case
image build must be run from the normal host terminal or provisioned image.
