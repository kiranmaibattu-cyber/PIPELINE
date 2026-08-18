#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENGINE="${CONTAINER_ENGINE:-}"
if [ -z "$ENGINE" ]; then
  if command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  else
    echo "ERROR: docker or podman is required" >&2
    exit 1
  fi
fi

"$ENGINE" build -f docker/Dockerfile.base -t pipeline-ubuntu-python:24.04 .
"$ENGINE" build -f docker/Dockerfile.edge-agent -t pipeline-edge-agent:latest .
"$ENGINE" build -f docker/Dockerfile.surveillance -t surveillance-edge-runtime:latest .
"$ENGINE" build -f docker/Dockerfile.traffic -t traffic-edge-runtime:latest .

echo "built:"
echo "  pipeline-edge-agent:latest"
echo "  surveillance-edge-runtime:latest"
echo "  traffic-edge-runtime:latest"
