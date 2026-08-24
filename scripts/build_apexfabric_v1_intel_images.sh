#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SURVEILLANCE_VERSION="${SURVEILLANCE_IMAGE_VERSION:-2026.08.20-v2}"
TRAFFIC_VERSION="${TRAFFIC_IMAGE_VERSION:-2026.08.24-v6}"
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

platform_args=(--platform linux/amd64)
"$ENGINE" build "${platform_args[@]}" \
  --build-arg TARGETARCH=amd64 \
  -f docker/Dockerfile.base \
  -t pipeline-ubuntu-python:intel-285h-v1 .

"$ENGINE" tag pipeline-ubuntu-python:intel-285h-v1 pipeline-ubuntu-python:24.04

surveillance_tag="surveillance-edge-runtime:intel-285h-${SURVEILLANCE_VERSION}"
"$ENGINE" build "${platform_args[@]}" \
  --build-arg IMAGE_VERSION="$SURVEILLANCE_VERSION" \
  -f docker/Dockerfile.surveillance \
  -t "$surveillance_tag" .
echo "built $surveillance_tag"

CONTAINER_ENGINE="$ENGINE" \
APEXFABRIC_IMAGE_VERSION="$TRAFFIC_VERSION" \
  ./scripts/build_traffic_layered_image.sh
