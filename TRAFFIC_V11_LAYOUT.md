# Traffic v11 Location

Traffic v11 is self-contained in this PIPELINE repository. It does not import,
mount, or build from `/home/admin1/traffic-pilot-main`.

## Source

- Runtime: `edge_runtime/solution_packs/traffic/runtime_v11/`
- Traffic models: `models/traffic-v11/openvino/`
- Face models: `models/traffic-v11/face/openvino/`
- Workload Dockerfile: `docker/Dockerfile.traffic-v11`
- Intel base boundary: `docker/Dockerfile.traffic-v11-base`
- Podman build: `scripts/build_traffic_v11_layered_image.sh`

## Delivery

- Contracts: `delivery/apexfabric-v1/intel-285h/traffic-v11/`
- Image archive: `delivery/apexfabric-v1/intel-285h/traffic-v11/image-2026.09.18-v11.tar`
- Archive checksum: `delivery/apexfabric-v1/intel-285h/traffic-v11/image-2026.09.18-v11.sha256`
- Previous v10 image: `delivery/apexfabric-v1/intel-285h/traffic-v11/archive-v10/`

The image contains vehicle counting, pedestrian counting, ANPR, fire/smoke
detection, and management-bound anonymous face samples. Existing PIPELINE
traffic and surveillance sources remain separate and unchanged.
