#!/usr/bin/env bash
# Enable Stack Monitoring self-monitoring (ES collection + Kibana collection).
set -euo pipefail

KIBANA_YML="/etc/kibana/kibana.yml"
ES_HOST="${ES_HOST:-ismelkesnode01.ocplab.net}"
ELASTIC_USER="${ELASTIC_USER:-elastic}"
ELASTIC_PASS="${ELASTIC_PASS:-}"

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f "$KIBANA_YML" ]] || { echo "Missing $KIBANA_YML" >&2; exit 1; }

set_kibana_yml() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}:" "$KIBANA_YML"; then
    sed -i "s|^${key}:.*|${key}: ${value}|" "$KIBANA_YML"
  else
    printf '%s: %s\n' "$key" "$value" >> "$KIBANA_YML"
  fi
}

echo "=== Kibana Stack Monitoring settings ==="
set_kibana_yml "monitoring.ui.enabled" "true"
set_kibana_yml "monitoring.kibana.collection.enabled" "true"
set_kibana_yml "monitoring.kibana.collection.interval" "10000"
set_kibana_yml "monitoring.ui.elasticsearch.hosts" "[\"https://${ES_HOST}:9200\"]"

if [[ -n "$ELASTIC_PASS" ]]; then
  echo "=== Elasticsearch monitoring collection (cluster settings) ==="
  body='{"persistent":{"xpack.monitoring.collection.enabled":true,"xpack.monitoring.elasticsearch.collection.enabled":true}}'
  out="$(curl -sk -u "${ELASTIC_USER}:${ELASTIC_PASS}" \
    -X PUT "https://${ES_HOST}:9200/_cluster/settings" \
    -H 'Content-Type: application/json' \
    -d "$body" 2>&1)" || true
  echo "$out" | tail -20

  echo "=== Verify monitoring collection enabled ==="
  curl -sk -u "${ELASTIC_USER}:${ELASTIC_PASS}" \
    "https://${ES_HOST}:9200/_cluster/settings?include_defaults=true&filter_path=**.xpack.monitoring.collection*" \
    2>/dev/null | head -20 || true
else
  echo "ELASTIC_PASS not set — skipped ES cluster monitoring settings API call" >&2
fi

if getent group kibana &>/dev/null; then
  chown root:kibana "$KIBANA_YML" 2>/dev/null || chown root:root "$KIBANA_YML"
else
  chown root:root "$KIBANA_YML"
fi
chmod 660 "$KIBANA_YML"
echo "Stack Monitoring self-monitoring configured"