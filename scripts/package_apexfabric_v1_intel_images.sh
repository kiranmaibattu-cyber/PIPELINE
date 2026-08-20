#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${APEXFABRIC_IMAGE_VERSION:-2026.08.20}"
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
  delivery="$ROOT/delivery/apexfabric-v1/intel-285h/$pack"
  image="${pack}-edge-runtime:intel-285h-${VERSION}"
  archive="$delivery/image.tar"
  rm -f "$archive" "$delivery/image.sha256"
  "$ENGINE" save --format docker-archive -o "$archive" "$image"
  sha256sum "$archive" | sed 's#  .*/#  #' > "$delivery/image.sha256"
  rm -f "$delivery"/image.tar.part-* "$delivery/image.parts.sha256"
  if [[ "$pack" == "surveillance" ]]; then
    split --bytes=1800M --suffix-length=2 "$archive" "$delivery/image.tar.part-"
    (cd "$delivery" && sha256sum image.tar.part-*) > "$delivery/image.parts.sha256"
  fi
  echo "packaged $image -> $archive"
done
