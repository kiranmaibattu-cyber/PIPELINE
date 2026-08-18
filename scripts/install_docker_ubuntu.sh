#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with root privileges: sudo $0" >&2
  exit 1
fi

apt-get update
apt-get install -y docker.io docker-compose-plugin
systemctl enable --now docker

if [ -n "${SUDO_USER:-}" ] && id "$SUDO_USER" >/dev/null 2>&1; then
  usermod -aG docker "$SUDO_USER"
  echo "Added $SUDO_USER to docker group. Log out/in before using docker without sudo."
fi

docker version
