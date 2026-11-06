#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BASE="${SENTINEL_REPAIR_BASE:-ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-tlsfix2}"
EXPECTED_BASE_ID=dbaeacc92973fc5d1e41521e3200cd64151aaa53a472404a79f87833bade3ef5
VERSION="${SENTINEL_IMAGE_VERSION:-2026.11.06-v8-qaft-int8-reconnect1}"
TAG="${SENTINEL_IMAGE_REPOSITORY:-ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime}:intel-285h-${VERSION}"
ACTUAL_BASE_ID="$(podman image inspect "$BASE" --format '{{.Id}}')"
if [[ "$ACTUAL_BASE_ID" != "$EXPECTED_BASE_ID" ]]; then
  echo 'ERROR: repair base differs from the frozen QAFT TLSfix2 image' >&2
  exit 1
fi
if podman image inspect "$TAG" >/dev/null 2>&1; then
  echo 'ERROR: output tag already exists; choose a new SENTINEL_IMAGE_VERSION' >&2
  exit 1
fi
podman build --pull=never --platform linux/amd64 \
  --build-arg "SENTINEL_REPAIR_BASE=${BASE}" \
  --build-arg "REPAIR_BASE_ID=${ACTUAL_BASE_ID}" \
  --build-arg "IMAGE_VERSION=${VERSION}" \
  -f docker/Dockerfile.sentinel-v8-reconnect -t "$TAG" .
podman image inspect "$TAG" --format 'built {{.Id}} {{.RepoTags}}'
