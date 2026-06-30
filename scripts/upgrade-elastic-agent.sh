#!/usr/bin/env bash
# Upgrade enrolled Elastic Agent using local archive (air-gap safe, preserves enrollment).
set -euo pipefail

VERSION=""
ARCHIVE_DIR="${ELASTIC_ARCHIVE_DIR:-/opt/elastic-setup/archives}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --archive-dir) ARCHIVE_DIR="$2"; shift 2 ;;
    *) echo "Unknown: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$VERSION" ]] || { echo "Usage: $0 --version VER" >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

# shellcheck source=elastic-archive-install.sh
source "$(dirname "$0")/elastic-archive-install.sh"
ELASTIC_ARCHIVE_DIR="$ARCHIVE_DIR"

agent_binary_version() {
  local bin="$1"
  [[ -x "$bin" ]] || return 1
  "$bin" version --binary-only 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

echo "=== Upgrade Elastic Agent to ${VERSION} ==="
extract_elastic_agent_archive "$VERSION"
EXTRACT_DIR="$(elastic_agent_extract_dir "$VERSION")"
SOURCE_URI="file://${EXTRACT_DIR}"
INSTALLED_BIN="/opt/Elastic/Agent/elastic-agent"

if [[ -x "$INSTALLED_BIN" ]]; then
  CURRENT="$(agent_binary_version "$INSTALLED_BIN" || true)"
  if [[ "$CURRENT" == "$VERSION" ]]; then
    echo "Already on ${VERSION}"
    systemctl restart elastic-agent 2>/dev/null || true
    "$INSTALLED_BIN" version --binary-only
    exit 0
  fi

  echo "In-place upgrade ${CURRENT:-unknown} -> ${VERSION} via ${SOURCE_URI}"
  if "$INSTALLED_BIN" upgrade "$VERSION" --source-uri "$SOURCE_URI" -y 2>/dev/null \
    || "$INSTALLED_BIN" upgrade --source-uri "$SOURCE_URI" --version "$VERSION" -y; then
    systemctl restart elastic-agent 2>/dev/null || true
    sleep 8
    NEW_VER="$(agent_binary_version "$INSTALLED_BIN" || true)"
    if [[ "$NEW_VER" == "$VERSION" ]]; then
      "$INSTALLED_BIN" version --binary-only
      echo "Elastic Agent upgrade finished (${VERSION})"
      exit 0
    fi
    echo "Upgrade command succeeded but binary reports ${NEW_VER:-unknown}" >&2
  else
    echo "elastic-agent upgrade failed" >&2
  fi
else
  echo "No installed agent at ${INSTALLED_BIN}" >&2
fi

exit 1