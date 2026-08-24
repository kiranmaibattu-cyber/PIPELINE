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

for pack in surveillance traffic; do
  if [[ "$pack" == "surveillance" ]]; then
    version="$SURVEILLANCE_VERSION"
  else
    version="$TRAFFIC_VERSION"
  fi
  delivery="$ROOT/delivery/apexfabric-v1/intel-285h/$pack"
  image="${pack}-edge-runtime:intel-285h-${version}"
  archive_name="image-${version}.tar"
  archive="$delivery/$archive_name"
  checksum="$delivery/image-${version}.sha256"
  parts_checksum="$delivery/image-${version}.parts.sha256"
  "$ENGINE" save --format docker-archive -o "$archive" "$image"
  sha256sum "$archive" | sed 's#  .*/#  #' > "$checksum"
  if [[ "$pack" == "surveillance" ]]; then
    split --bytes=1800M --suffix-length=2 "$archive" "$delivery/${archive_name}.part-"
    (cd "$delivery" && sha256sum "${archive_name}.part-"*) > "$parts_checksum"
  fi
  echo "packaged $image -> $archive"
done
