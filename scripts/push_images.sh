#!/usr/bin/env bash
set -euo pipefail

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

REGISTRY_NAMESPACE="${REGISTRY_NAMESPACE:-ghcr.io/kiranmaibattu-cyber}"
SOURCE_REPOSITORY="https://github.com/kiranmaibattu-cyber/PIPELINE"

IMAGES=(
  "surveillance-edge-runtime:intel-285h"
  "traffic-edge-runtime:intel-285h"
  "pipeline-edge-agent:latest"
)

for image in "${IMAGES[@]}"; do
  source_label="$($ENGINE image inspect "$image" \
    --format '{{index .Config.Labels "org.opencontainers.image.source"}}')"
  if [ "$source_label" != "$SOURCE_REPOSITORY" ]; then
    echo "ERROR: $image is missing the PIPELINE source label; rebuild it first" >&2
    exit 1
  fi

  remote_image="${REGISTRY_NAMESPACE}/${image}"
  "$ENGINE" tag "$image" "$remote_image"
  if [[ "$ENGINE" == *podman ]]; then
    # GHCR reads the source label from Docker schema v2 when linking a package.
    "$ENGINE" push --format v2s2 "$remote_image"
  else
    "$ENGINE" push "$remote_image"
  fi
done

echo "published and linked to $SOURCE_REPOSITORY:"
for image in "${IMAGES[@]}"; do
  echo "  ${REGISTRY_NAMESPACE}/${image}"
done
