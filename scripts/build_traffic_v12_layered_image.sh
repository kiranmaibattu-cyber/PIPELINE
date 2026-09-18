#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v podman >/dev/null 2>&1; then
  echo "ERROR: Podman is required" >&2
  exit 1
fi

BASE_TAG="${INTEL_TRAFFIC_V12_BASE_TAG:-localhost/apexfabric-intel-traffic-runtime-base:intel-285h-2026.09.18-v2}"
IMAGE_VERSION="${APEXFABRIC_IMAGE_VERSION:-2026.09.18-v12}"
IMAGE_TAG="localhost/traffic-pilot-runtime:intel-285h-${IMAGE_VERSION}"

if [[ "${BUILD_TRAFFIC_V12_BASE:-0}" == "1" ]]; then
  parent="${INTEL_TRAFFIC_PARENT_BASE:-localhost/apexfabric-intel-traffic-runtime-base:intel-285h-2026.08.24-v1}"
  podman build --platform linux/amd64 \
    --build-arg "INTEL_TRAFFIC_PARENT_BASE=${parent}" \
    -f docker/Dockerfile.traffic-v11-base \
    -t "$BASE_TAG" .
fi

if ! podman image inspect "$BASE_TAG" >/dev/null 2>&1; then
  echo "ERROR: base image not found: $BASE_TAG" >&2
  echo "Load the Intel base or run with BUILD_TRAFFIC_V12_BASE=1." >&2
  exit 1
fi

base_id="$(podman image inspect "$BASE_TAG" --format '{{.Id}}')"
podman build --platform linux/amd64 \
  --build-arg "INTEL_TRAFFIC_RUNTIME_BASE=${BASE_TAG}" \
  --build-arg "INTEL_TRAFFIC_RUNTIME_BASE_DIGEST=${base_id}" \
  --build-arg "IMAGE_VERSION=${IMAGE_VERSION}" \
  -f docker/Dockerfile.traffic-v12 \
  -t "$IMAGE_TAG" .

delivery="delivery/apexfabric-v1/intel-285h/traffic-v12"
archive="${delivery}/image-${IMAGE_VERSION}.tar"
checksum="${archive%.tar}.sha256"
mkdir -p "$delivery"
[[ ! -e "$archive" ]] || unlink "$archive"
[[ ! -e "$checksum" ]] || unlink "$checksum"
podman save --format docker-archive -o "$archive" "$IMAGE_TAG"
sha256sum "$archive" > "$checksum"

echo "built runtime base: $BASE_TAG@$base_id"
echo "built workload:     $IMAGE_TAG"
echo "saved archive:      $archive"
