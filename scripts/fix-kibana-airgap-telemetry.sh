#!/usr/bin/env bash
# Silence air-gap Kibana log noise (telemetry, newsfeed, external artifact fetches).
set -euo pipefail

KIBANA_YML="${KIBANA_YML:-/etc/kibana/kibana.yml}"

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f "$KIBANA_YML" ]] || { echo "Missing ${KIBANA_YML}" >&2; exit 1; }

set_yaml() {
  local key="$1"
  local val="$2"
  if grep -q "^${key}:" "$KIBANA_YML"; then
    sed -i "s|^${key}:.*|${key}: ${val}|" "$KIBANA_YML"
  else
    printf '%s: %s\n' "$key" "$val" >> "$KIBANA_YML"
  fi
}

# Remove invalid keys from earlier attempts (Kibana 8.18 rejects unknown nested keys).
sed -i '/^xpack\.securitySolution\.telemetryEventsSender/d' "$KIBANA_YML"

NEEDS_CHANGE=0
for pair in "telemetry.enabled:false" "telemetry.optIn:false" "newsfeed.enabled:false"; do
  key="${pair%%:*}"
  val="${pair##*:}"
  if ! grep -q "^${key}: ${val}$" "$KIBANA_YML"; then
    NEEDS_CHANGE=1
    break
  fi
done

if [[ "$NEEDS_CHANGE" -eq 0 ]] && ! grep -q '^xpack\.securitySolution\.telemetryEventsSender' "$KIBANA_YML"; then
  echo "kibana_yml=unchanged skip_restart"
  grep -E '^(telemetry\.|newsfeed\.)' "$KIBANA_YML" || true
  exit 0
fi

cp -a "$KIBANA_YML" "${KIBANA_YML}.bak.telemetry.$(date +%Y%m%d%H%M%S)"

set_yaml "telemetry.enabled" "false"
set_yaml "telemetry.optIn" "false"
set_yaml "newsfeed.enabled" "false"

echo "Air-gap telemetry settings:"
grep -E '^(telemetry\.|newsfeed\.)' "$KIBANA_YML" || true

systemctl restart kibana
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 http://127.0.0.1:5601/api/status 2>/dev/null || echo 000)
  if echo "$code" | grep -qE '^(200|302|401|503)$'; then
    echo "Kibana ready (poll ${i}, http=${code})"
    exit 0
  fi
  sleep 5
done
echo "Kibana did not become ready within 7.5 minutes" >&2
exit 1