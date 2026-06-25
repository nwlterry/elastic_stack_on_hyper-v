#!/usr/bin/env bash
# Bootstrap a 3-node Elasticsearch 8.18.4 cluster on RHEL 8.9 using enrollment tokens.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-es-cluster}"
NODE01_HOST="${NODE01_HOST:-ismelkesnode01}"
NODE02_HOST="${NODE02_HOST:-ismelkesnode02}"
NODE03_HOST="${NODE03_HOST:-ismelkesnode03}"

NODE01_IP="${NODE01_IP:-10.44.40.31}"
NODE02_IP="${NODE02_IP:-10.44.40.32}"
NODE03_IP="${NODE03_IP:-10.44.40.33}"

ES_BIN="/usr/share/elasticsearch/bin"
CURRENT_HOST=$(hostname -s | tr '[:upper:]' '[:lower:]')
NODE01_HOST=$(echo "$NODE01_HOST" | tr '[:upper:]' '[:lower:]')
NODE02_HOST=$(echo "$NODE02_HOST" | tr '[:upper:]' '[:lower:]')
NODE03_HOST=$(echo "$NODE03_HOST" | tr '[:upper:]' '[:lower:]')

wait_for_es() {
  local host=$1
  local max=60
  local i=0
  echo "Waiting for Elasticsearch on ${host}..."
  while (( i < max )); do
    if curl -sk --connect-timeout 2 "https://${host}:9200" &>/dev/null; then
      echo "  ${host} is up."
      return 0
    fi
    sleep 5
    ((i++))
  done
  echo "Timed out waiting for ${host}" >&2
  return 1
}

wait_for_api() {
  local max=60
  local i=0
  echo "Waiting for Elasticsearch HTTPS API..."
  while (( i < max )); do
    local resp
    resp=$(curl -sk --connect-timeout 2 "https://localhost:9200" 2>/dev/null || true)
    if [[ "$resp" == *"security_exception"* || "$resp" == *"tagline"* ]]; then
      echo "  HTTPS API responding"
      return 0
    fi
    sleep 5
    ((i++))
  done
  echo "Timed out waiting for Elasticsearch API" >&2
  return 1
}

wait_for_cluster_ready() {
  local max=60
  local i=0
  echo "Waiting for cluster to stabilise..."
  sleep 20
  while (( i < max )); do
    local resp
    resp=$(curl -sk --connect-timeout 2 "https://localhost:9200/_cluster/health" 2>/dev/null || true)
    if [[ "$resp" == *'"status":"green"'* || "$resp" == *'"status":"yellow"'* \
       || "$resp" == *"security_exception"* ]]; then
      echo "  Cluster/API ready"
      return 0
    fi
    sleep 5
    ((i++))
  done
  echo "Timed out waiting for cluster health" >&2
  return 1
}

ELASTIC_PASSWORD_FILE="/root/.elastic-stack/elastic-password"

save_elastic_password() {
  local pw="$1"
  mkdir -p "$(dirname "$ELASTIC_PASSWORD_FILE")"
  printf '%s\n' "$pw" > "$ELASTIC_PASSWORD_FILE"
  chmod 600 "$ELASTIC_PASSWORD_FILE"
  echo "Saved elastic password to ${ELASTIC_PASSWORD_FILE}"
}

reset_elastic_password() {
  local attempt=0
  while (( attempt < 20 )); do
    local out=""
    if out=$("$ES_BIN/elasticsearch-reset-password" -u elastic -b 2>&1); then
      local pw=""
      pw=$(echo "$out" | sed -n 's/.*New value: \([^[:space:]]*\).*/\1/p')
      if [[ -n "$pw" ]]; then
        save_elastic_password "$pw"
      fi
      echo "$out"
      return 0
    fi
    echo "$out" >&2
    sleep 15
    ((attempt++))
  done
  return 1
}

configure_first_node() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  NODE_IP="${NODE01_IP}" bash "${script_dir}/fix-es-bootstrap.sh"
  wait_for_es "localhost"
  wait_for_api
  wait_for_cluster_ready

  echo ""
  if [[ "${NONINTERACTIVE:-}" == "1" ]]; then
    echo "=== Auto-generated elastic password ==="
    reset_elastic_password
    echo ""
    echo "=== Node enrollment tokens ==="
    "$ES_BIN/elasticsearch-create-enrollment-token" -s node
    echo ""
    echo "=== Kibana enrollment token ==="
    "$ES_BIN/elasticsearch-create-enrollment-token" -s kibana
  else
    echo "=== Reset elastic superuser password (save this) ==="
    "$ES_BIN/elasticsearch-reset-password" -u elastic -i
    echo ""
    echo "=== Kibana enrollment token (valid 30 min) ==="
    "$ES_BIN/elasticsearch-create-enrollment-token" -s kibana
  fi
}

enroll_additional_node() {
  local token="${NODE_ENROLLMENT_TOKEN:-}"

  if [[ -z "$token" ]]; then
    echo "NODE_ENROLLMENT_TOKEN not set."
    echo "On es-node01, run:"
    echo "  $ES_BIN/elasticsearch-create-enrollment-token -s node"
    echo "Then on this node:"
    echo "  NODE_ENROLLMENT_TOKEN='<token>' bash $0"
    exit 1
  fi

  echo "Reconfiguring ${CURRENT_HOST} with enrollment token..."
  "$ES_BIN/elasticsearch-reconfigure-node" --enrollment-token "$token" <<< 'y'

  # Ensure cluster name matches
  sed -i "s/^cluster.name:.*/cluster.name: ${CLUSTER_NAME}/" /etc/elasticsearch/elasticsearch.yml

  systemctl start elasticsearch
  wait_for_es "localhost"
}

case "$CURRENT_HOST" in
  "$NODE01_HOST")
    echo "=== Bootstrapping first node: ${NODE01_HOST} ==="
    configure_first_node
    ;;
  "$NODE02_HOST"|"$NODE03_HOST")
    echo "=== Enrolling node: ${CURRENT_HOST} ==="
    enroll_additional_node
    ;;
  *)
    echo "Unknown host '${CURRENT_HOST}'. Set hostname to one of: ${NODE01_HOST}, ${NODE02_HOST}, ${NODE03_HOST}" >&2
    exit 1
    ;;
esac

echo ""
echo "=== Cluster health (from ${CURRENT_HOST}) ==="
# Password must be set in ELASTIC_PASSWORD env var for this check
if [[ -n "${ELASTIC_PASSWORD:-}" ]]; then
  curl -sk --cacert /etc/elasticsearch/certs/http_ca.crt \
    -u "elastic:${ELASTIC_PASSWORD}" \
    "https://localhost:9200/_cluster/health?pretty"
else
  echo "Set ELASTIC_PASSWORD and run:"
  echo "  curl -sk --cacert /etc/elasticsearch/certs/http_ca.crt -u elastic:\$ELASTIC_PASSWORD https://localhost:9200/_cluster/health?pretty"
fi