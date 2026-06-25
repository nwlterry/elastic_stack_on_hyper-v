#!/usr/bin/env bash
# Install Elastic Agent enrolled to Fleet Server (tar.gz archive)
set -euo pipefail

VERSION="${VERSION:-8.18.4}"
FLEET_URL="${FLEET_URL:-https://ismelkflnode01.ocplab.net:8220}"
ENROLLMENT_TOKEN="${ENROLLMENT_TOKEN:-}"
ES_HOST="${ES_HOST:-ismelkesnode01.ocplab.net}"
CA_FILE="${CA_FILE:-/opt/elastic-setup/certs/http_ca.crt}"

usage() {
  echo "Usage: $0 --enrollment-token TOKEN [--fleet-url URL] [--es-host HOST] [--ca-file PATH] [--version 8.18.4]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --fleet-url) FLEET_URL="$2"; shift 2 ;;
    --enrollment-token) ENROLLMENT_TOKEN="$2"; shift 2 ;;
    --es-host) ES_HOST="$2"; shift 2 ;;
    --ca-file) CA_FILE="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown: $1" >&2; usage ;;
  esac
done

[[ -n "$ENROLLMENT_TOKEN" ]] || usage
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

echo "=== Installing Elastic Agent ${VERSION} (archive) ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=elastic-archive-install.sh
source "${SCRIPT_DIR}/elastic-archive-install.sh"
# shellcheck source=elastic-agent-ca.sh
source "${SCRIPT_DIR}/elastic-agent-ca.sh"

remove_elastic_agent "$VERSION"
install_elastic_agent_from_archive "$VERSION"
AGENT_BIN="$(elastic_agent_binary "$VERSION")"

# CA pre-staged by orchestrator or present on ES nodes.
CA_FILE="$(resolve_es_ca_source "$CA_FILE")"
install_es_ca_for_agent "$CA_FILE"
build_elastic_agent_ca_args "agent"

# --insecure: Fleet Server 8220 uses a self-signed cert; ES trust uses custom CA above.
"$AGENT_BIN" install --non-interactive --force \
  --url="${FLEET_URL}" \
  --enrollment-token="${ENROLLMENT_TOKEN}" \
  --insecure \
  "${ELASTIC_AGENT_CA_ARGS[@]}"

preserve_agent_ca
systemctl enable elastic-agent
systemctl start elastic-agent
echo "Elastic Agent enrolled to ${FLEET_URL} (ES CA: ${ELASTIC_AGENT_CA_FILE})"