#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <surveillance|traffic> <version> <output-directory>" >&2
  exit 2
fi

pack="$1"
version="$2"
output_dir="$3"
case "$pack" in
  surveillance|traffic) ;;
  *) echo "unsupported solution pack: $pack" >&2; exit 2 ;;
esac
if [[ ! "$version" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "version may contain only letters, digits, dot, underscore, and hyphen" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$repo_root/models/$pack"
archive="$output_dir/${pack}-${version}.tar.gz"
mkdir -p "$output_dir"

tar \
  --sort=name \
  --mtime='UTC 1970-01-01' \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  -C "$source_dir" \
  -czf "$archive" \
  .

sha256sum "$archive" | tee "$archive.sha256"
