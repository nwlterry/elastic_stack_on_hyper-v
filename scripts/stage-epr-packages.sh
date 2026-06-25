#!/usr/bin/env bash
# Stage Fleet integration zips for air-gapped local EPR (system, elasticsearch, kibana, etc.).
set -euo pipefail

REMOTE_DIR="${REMOTE_DIR:-/opt/elastic-setup}"
EPR_DIR="${EPR_DIR:-${REMOTE_DIR}/epr-packages}"
BUNDLED="/usr/share/kibana/node_modules/@kbn/fleet-plugin/target/bundled_packages"
EPR_BASE="${EPR_BASE:-https://epr.elastic.co/epr}"

mkdir -p "$EPR_DIR"

declare -A PACKAGES=(
  [fleet_server]=1.6.0
  [elastic_agent]=2.3.0
  [system]=1.60.0
  [elasticsearch]=1.12.0
  [kibana]=2.3.1
)

fetch_or_copy() {
  local name="$1"
  local ver="$2"
  local zip="${name}-${ver}.zip"
  local dest="${EPR_DIR}/${zip}"

  if [[ -f "$dest" ]]; then
    echo "OK ${zip} (already staged)"
    return 0
  fi

  if [[ -f "${REMOTE_DIR}/epr-packages/${zip}" ]]; then
    cp -f "${REMOTE_DIR}/epr-packages/${zip}" "$dest"
    echo "OK ${zip} (from upload)"
    return 0
  fi

  if [[ -f "${REMOTE_DIR}/packages/epr/${zip}" ]]; then
    cp -f "${REMOTE_DIR}/packages/epr/${zip}" "$dest"
    echo "OK ${zip} (from packages/epr)"
    return 0
  fi

  if [[ -f "${BUNDLED}/${zip}" ]]; then
    cp -f "${BUNDLED}/${zip}" "$dest"
    echo "OK ${zip} (from Kibana bundled_packages)"
    return 0
  fi

  if command -v curl >/dev/null 2>&1; then
    local url="${EPR_BASE}/${name}/${zip}"
    if curl -fsSL --connect-timeout 20 --max-time 600 -o "$dest" "$url"; then
      echo "OK ${zip} (downloaded)"
      return 0
    fi
    rm -f "$dest"
  fi

  echo "MISSING ${zip}" >&2
  return 1
}

missing=0
for name in fleet_server elastic_agent system elasticsearch kibana; do
  fetch_or_copy "$name" "${PACKAGES[$name]}" || missing=$((missing + 1))
done

echo "Staged packages in ${EPR_DIR}:"
ls -la "$EPR_DIR"/*.zip 2>/dev/null || true

if [[ $missing -gt 0 ]]; then
  echo "WARN: ${missing} package(s) missing — copy zips to ${EPR_DIR} or ${REMOTE_DIR}/packages/epr/" >&2
  exit 1
fi

echo "All EPR packages staged."