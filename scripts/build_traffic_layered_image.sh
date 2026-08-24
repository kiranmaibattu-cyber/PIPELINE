#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENGINE="${CONTAINER_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
  if command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  else
    echo "ERROR: docker or podman is required" >&2
    exit 1
  fi
fi

BASE_VERSION="${INTEL_TRAFFIC_BASE_VERSION:-2026.08.24-v1}"
IMAGE_VERSION="${APEXFABRIC_IMAGE_VERSION:-2026.08.24-v6}"
BASE_TAG="localhost/apexfabric-intel-traffic-runtime-base:intel-285h-${BASE_VERSION}"
IMAGE_TAG="localhost/traffic-edge-runtime:intel-285h-${IMAGE_VERSION}"
platform_args=(--platform linux/amd64)

"$ENGINE" build "${platform_args[@]}" \
  --build-arg TARGETARCH=amd64 \
  -f docker/Dockerfile.traffic-base \
  -t "$BASE_TAG" .

base_digest="$($ENGINE image inspect "$BASE_TAG" --format '{{.Digest}}')"
if [[ ! "$base_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "ERROR: could not resolve immutable digest for $BASE_TAG" >&2
  exit 1
fi

"$ENGINE" build "${platform_args[@]}" \
  --build-arg "INTEL_TRAFFIC_RUNTIME_BASE=${BASE_TAG}@${base_digest}" \
  --build-arg "INTEL_TRAFFIC_RUNTIME_BASE_DIGEST=${base_digest}" \
  --build-arg "IMAGE_VERSION=${IMAGE_VERSION}" \
  -f docker/Dockerfile.traffic \
  -t "$IMAGE_TAG" .

echo "built runtime base: $BASE_TAG@$base_digest"
echo "built workload:     $IMAGE_TAG"
