#!/usr/bin/env bash
# Fix dashboard / Stack Monitoring search: set monitoring UI ES credentials (non-superuser).
set -euo pipefail

KIBANA_YML="${KIBANA_YML:-/etc/kibana/kibana.yml}"
ES_HOST="${ES_HOST:-ismelkesnode01.ocplab.net}"
MONITORING_USER="${MONITORING_USER:-elastic_monitoring}"
MONITORING_PASS="${MONITORING_PASS:-}"

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f "$KIBANA_YML" ]] || { echo "Missing $KIBANA_YML" >&2; exit 1; }
[[ -n "$MONITORING_PASS" ]] || { echo "MONITORING_PASS required" >&2; exit 1; }

set_yaml() {
  local key="$1"
  local val="$2"
  if grep -q "^${key}:" "$KIBANA_YML"; then
    sed -i "s|^${key}:.*|${key}: ${val}|" "$KIBANA_YML"
  else
    printf '%s: %s\n' "$key" "$val" >> "$KIBANA_YML"
  fi
}

# Kibana 8.x rejects elastic superuser for monitoring.ui.elasticsearch.username.
sed -i '/^monitoring\.ui\.elasticsearch\.username: "elastic"$/d' "$KIBANA_YML"
sed -i '/^monitoring\.ui\.elasticsearch\.password:/d' "$KIBANA_YML"

CHANGED=0
if ! grep -q "^monitoring.ui.elasticsearch.username: \"${MONITORING_USER}\"$" "$KIBANA_YML" 2>/dev/null; then
  CHANGED=1
fi

set_yaml "monitoring.ui.enabled" "true"
set_yaml "monitoring.ui.elasticsearch.hosts" "[\"https://${ES_HOST}:9200\"]"
set_yaml "monitoring.ui.elasticsearch.username" "\"${MONITORING_USER}\""
set_yaml "monitoring.ui.elasticsearch.password" "\"${MONITORING_PASS}\""

grep -E '^monitoring\.ui\.' "$KIBANA_YML" || true

if [[ "$CHANGED" -eq 1 ]]; then
  echo "kibana_yml_changed=monitoring_ui_creds"
  systemctl reset-failed kibana 2>/dev/null || true
  systemctl restart kibana
  for i in $(seq 1 90); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 http://127.0.0.1:5601/api/status 2>/dev/null || echo 000)
    if echo "$code" | grep -qE '^(200|302|401|503)$'; then
      echo "Kibana ready (poll ${i})"
      exit 0
    fi
    sleep 5
  done
  echo "Kibana restart timeout" >&2
  exit 1
fi

echo "kibana_yml=unchanged skip_restart"