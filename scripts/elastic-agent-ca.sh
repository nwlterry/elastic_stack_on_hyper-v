#!/usr/bin/env bash
# Install Elasticsearch http_ca.crt for elastic-agent enrollment (custom CA trust).
set -euo pipefail

ELASTIC_AGENT_CA_DIR="${ELASTIC_AGENT_CA_DIR:-/etc/elastic-agent/certs}"
ELASTIC_AGENT_CA_FILE="${ELASTIC_AGENT_CA_FILE:-${ELASTIC_AGENT_CA_DIR}/http_ca.crt}"
ES_CA_SOURCE="${ES_CA_SOURCE:-/etc/elasticsearch/certs/http_ca.crt}"

# Resolve pre-staged Elasticsearch http_ca.crt (orchestrator or local ES node).
resolve_es_ca_source() {
  local explicit="${1:-}"
  local candidate
  for candidate in \
    "$explicit" \
    /opt/elastic-setup/certs/http_ca.crt \
    /etc/elastic-agent/certs/http_ca.crt \
    /etc/elasticsearch/certs/http_ca.crt; do
    if [[ -n "$candidate" && -f "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  echo "Elasticsearch CA not found; stage http_ca.crt before enrollment" >&2
  return 1
}

install_es_ca_for_agent() {
  local source="${1:-$ES_CA_SOURCE}"

  mkdir -p "$ELASTIC_AGENT_CA_DIR"

  if [[ ! -f "$source" ]]; then
    echo "Elasticsearch CA must be pre-staged at ${source}" >&2
    return 1
  fi
  cp "$source" "$ELASTIC_AGENT_CA_FILE"

  chmod 644 "$ELASTIC_AGENT_CA_FILE"
  chown root:root "$ELASTIC_AGENT_CA_FILE"

  if ! openssl x509 -in "$ELASTIC_AGENT_CA_FILE" -noout -subject >/dev/null 2>&1; then
    echo "Invalid Elasticsearch CA at ${ELASTIC_AGENT_CA_FILE}" >&2
    return 1
  fi

  echo "Elasticsearch CA ready: ${ELASTIC_AGENT_CA_FILE}" >&2
  openssl x509 -in "$ELASTIC_AGENT_CA_FILE" -noout -subject -fingerprint -sha256 >&2
}

preserve_agent_ca() {
  [[ -f "$ELASTIC_AGENT_CA_FILE" ]] || return 0
  mkdir -p "$ELASTIC_AGENT_CA_DIR"
  local dest="${ELASTIC_AGENT_CA_DIR}/http_ca.crt"
  if [[ "$ELASTIC_AGENT_CA_FILE" != "$dest" ]]; then
    cp -f "$ELASTIC_AGENT_CA_FILE" "$dest"
  fi
  chmod 644 "$dest"
}

es_ca_fingerprint_sha256() {
  openssl x509 -in "$ELASTIC_AGENT_CA_FILE" -noout -fingerprint -sha256 2>/dev/null \
    | awk -F= '{print $2}' | tr -d ':' | tr '[:upper:]' '[:lower:]'
}

# Populates ELASTIC_AGENT_CA_ARGS array. mode: fleet-server | agent
build_elastic_agent_ca_args() {
  local mode="${1:-agent}"
  ELASTIC_AGENT_CA_ARGS=()

  [[ -f "$ELASTIC_AGENT_CA_FILE" ]] || {
    echo "CA file missing for enrollment: ${ELASTIC_AGENT_CA_FILE}" >&2
    return 1
  }

  ELASTIC_AGENT_CA_ARGS+=(--certificate-authorities="${ELASTIC_AGENT_CA_FILE}")

  if [[ "$mode" == "fleet-server" ]]; then
    ELASTIC_AGENT_CA_ARGS+=(--fleet-server-es-ca="${ELASTIC_AGENT_CA_FILE}")
    local fp
    fp="$(es_ca_fingerprint_sha256)"
    if [[ -n "$fp" ]]; then
      ELASTIC_AGENT_CA_ARGS+=(--fleet-server-es-ca-trusted-fingerprint="${fp}")
    fi
  fi
}