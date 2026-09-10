#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SURVEILLANCE_VERSION="${SURVEILLANCE_IMAGE_VERSION:-2026.09.10-v2}"
TRAFFIC_VERSION="${TRAFFIC_IMAGE_VERSION:-2026.09.10-v2}"
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

CONTAINER_ENGINE="$ENGINE" \
APEXFABRIC_IMAGE_VERSION="$SURVEILLANCE_VERSION" \
  ./scripts/build_surveillance_layered_image.sh

CONTAINER_ENGINE="$ENGINE" \
APEXFABRIC_IMAGE_VERSION="$TRAFFIC_VERSION" \
  ./scripts/build_traffic_layered_image.sh
