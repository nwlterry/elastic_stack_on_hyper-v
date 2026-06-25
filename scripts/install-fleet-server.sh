#!/usr/bin/env bash
# Install Elastic Agent as Fleet Server on RHEL 8.10 (tar.gz archive)
set -euo pipefail

VERSION="${VERSION:-8.18.4}"
ES_HOST="${ES_HOST:-ismelkesnode01.ocplab.net}"
SERVICE_TOKEN="${SERVICE_TOKEN:-}"
POLICY_ID="${POLICY_ID:-}"
CA_FILE="${CA_FILE:-/opt/elastic-setup/certs/http_ca.crt}"

usage() {
  echo "Usage: $0 --service-token TOKEN --policy-id ID [--version 8.18.4] [--es-host HOST] [--ca-file PATH]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --es-host) ES_HOST="$2"; shift 2 ;;
    --service-token) SERVICE_TOKEN="$2"; shift 2 ;;
    --policy-id) POLICY_ID="$2"; shift 2 ;;
    --ca-file) CA_FILE="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown: $1" >&2; usage ;;
  esac
done

[[ -n "$SERVICE_TOKEN" && -n "$POLICY_ID" ]] || usage
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

LOCK_FILE="/var/run/elastic-fleet-install.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another fleet install is already running. Exiting." >&2
  exit 0
fi

echo "=== Installing Fleet Server (elastic-agent ${VERSION} archive) ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "${SCRIPT_DIR}/prepare-fleet-memory.sh" || true
# shellcheck source=elastic-archive-install.sh
source "${SCRIPT_DIR}/elastic-archive-install.sh"
# shellcheck source=elastic-agent-ca.sh
source "${SCRIPT_DIR}/elastic-agent-ca.sh"

remove_elastic_agent "$VERSION"
install_elastic_agent_from_archive "$VERSION"
AGENT_BIN="$(elastic_agent_binary "$VERSION")"

# Install Elasticsearch http CA as custom CA for agent + fleet-server → ES TLS.
CA_FILE="$(resolve_es_ca_source "$CA_FILE")"
install_es_ca_for_agent "$CA_FILE"
build_elastic_agent_ca_args "fleet-server"

write_capabilities() {
  mkdir -p /etc/elastic-agent
  cat > /etc/elastic-agent/capabilities.yml <<'EOF'
capabilities:
  - rule: allow
    input: fleet-server
  - rule: allow
    input: "*/metrics"
  - rule: allow
    input: "*/logs"
  - rule: deny
    input: "*"
EOF
}

write_capabilities

# install recreates /etc/elastic-agent; keep capabilities + CA present during enroll.
(
  while true; do
    write_capabilities
    preserve_agent_ca
    sleep 2
  done
) &
CAP_WATCH_PID=$!
trap 'kill "${CAP_WATCH_PID}" 2>/dev/null || true' EXIT

echo "Enrolling Fleet Server -> https://${ES_HOST}:9200 policy=${POLICY_ID}"
echo "Using custom CA: ${ELASTIC_AGENT_CA_FILE}" >&2
# Trust ES via custom CA (no --insecure for ES). --insecure only needed for HTTP fleet endpoints.
"$AGENT_BIN" install --non-interactive --force \
  --fleet-server-es="https://${ES_HOST}:9200" \
  --fleet-server-service-token="${SERVICE_TOKEN}" \
  --fleet-server-policy="${POLICY_ID}" \
  --fleet-server-timeout=90m \
  "${ELASTIC_AGENT_CA_ARGS[@]}" 2>&1 | tee -a /var/log/fleet-install.log

kill "${CAP_WATCH_PID}" 2>/dev/null || true
write_capabilities
preserve_agent_ca

MEMORY_MAX="${FLEET_MEMORY_MAX:-8G}"
MEMORY_HIGH="${FLEET_MEMORY_HIGH:-6G}"
mkdir -p /etc/systemd/system/elastic-agent.service.d
cat > /etc/systemd/system/elastic-agent.service.d/memory.conf <<EOF
[Service]
MemoryMax=${MEMORY_MAX}
MemoryHigh=${MEMORY_HIGH}
EOF
systemctl daemon-reload

systemctl enable elastic-agent
systemctl start elastic-agent

echo "Fleet Server running on https://$(hostname -I | awk '{print $1}'):8220 (MemoryMax=${MEMORY_MAX})"