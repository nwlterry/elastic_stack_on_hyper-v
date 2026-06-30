#!/usr/bin/env bash
# Upgrade Kibana from local RPM (requires downtime on this node).
set -euo pipefail

VERSION=""
RPM_DIR="${ELASTIC_RPM_DIR:-/opt/elastic-setup/rpms}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --rpm-dir) RPM_DIR="$2"; shift 2 ;;
    *) echo "Unknown: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$VERSION" ]] || { echo "Usage: $0 --version VER" >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

RPM="${RPM_DIR}/kibana-${VERSION}-x86_64.rpm"
[[ -f "$RPM" ]] || { echo "Missing RPM: $RPM" >&2; exit 1; }

echo "=== Stop Kibana ==="
systemctl stop kibana.service

echo "=== Install kibana-${VERSION} ==="
# shellcheck source=elastic-rpm-install.sh
source "$(dirname "$0")/elastic-rpm-install.sh"
import_elastic_gpg || true
dnf install -y "$RPM"

echo "=== Start Kibana ==="
systemctl start kibana.service
systemctl is-active kibana.service
echo "Kibana upgraded to ${VERSION}"