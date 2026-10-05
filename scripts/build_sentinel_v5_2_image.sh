#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

command -v podman >/dev/null 2>&1 || { echo "ERROR: Podman is required" >&2; exit 1; }

BASE_TAG="${INTEL_TRAFFIC_V4_BASE_TAG:-localhost/apexfabric-intel-traffic-runtime-base:intel-285h-2026.09.18-v2}"
IMAGE_VERSION="${SENTINEL_IMAGE_VERSION:-2026.10.05-v5.2-dev4}"
IMAGE_REPOSITORY="${SENTINEL_IMAGE_REPOSITORY:-localhost/sentinel-cv-runtime}"
IMAGE_TAG="${IMAGE_REPOSITORY}:intel-285h-${IMAGE_VERSION}"

if ! podman image inspect "$BASE_TAG" >/dev/null 2>&1; then
  echo "ERROR: base image not found: $BASE_TAG" >&2
  exit 1
fi

base_id="$(podman image inspect "$BASE_TAG" --format '{{.Id}}')"
podman build --platform linux/amd64 \
  --build-arg "INTEL_TRAFFIC_RUNTIME_BASE=${BASE_TAG}" \
  --build-arg "INTEL_TRAFFIC_RUNTIME_BASE_DIGEST=${base_id}" \
  --build-arg "IMAGE_VERSION=${IMAGE_VERSION}" \
  -f docker/Dockerfile.sentinel-v5-2 \
  -t "$IMAGE_TAG" .

echo "built runtime base: $BASE_TAG@$base_id"
echo "built workload:     $IMAGE_TAG"
