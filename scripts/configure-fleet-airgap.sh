#!/usr/bin/env bash
# Enable Fleet air-gapped mode in Kibana (offline / no epr.elastic.co).
set -euo pipefail

KIBANA_YML="${KIBANA_YML:-/etc/kibana/kibana.yml}"
FLEET_HOST="${FLEET_HOST:-ismelkflnode01.ocplab.net}"
ES_CA_FILE="${ES_CA_FILE:-/etc/elasticsearch/certs/http_ca.crt}"

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

cp -a "$KIBANA_YML" "${KIBANA_YML}.bak.airgap.$(date +%Y%m%d%H%M%S)"

# Do not set xpack.fleet.agents.elasticsearch.* when xpack.fleet.outputs is already defined.
set_yaml "xpack.fleet.isAirGapped" "true"
set_yaml "xpack.fleet.registryUrl" "\"http://127.0.0.1:8080\""
if ! grep -q '^xpack\.fleet\.agents\.fleet_server\.hosts:' "$KIBANA_YML"; then
  set_yaml "xpack.fleet.agents.fleet_server.hosts" "[\"https://${FLEET_HOST}:8220\"]"
fi
# Remove conflicting legacy keys if a previous run added them.
sed -i '/^xpack\.fleet\.agents\.elasticsearch\.hosts:/d' "$KIBANA_YML"
sed -i '/^xpack\.fleet\.agents\.elasticsearch\.ca_sha256:/d' "$KIBANA_YML"

# Install bundled packages at startup (no epr.elastic.co).
if ! grep -q '^xpack\.fleet\.packages:' "$KIBANA_YML"; then
  cat >> "$KIBANA_YML" <<'EOF'
xpack.fleet.packages:
  - name: fleet_server
    version: 1.6.0
  - name: elastic_agent
    version: 2.3.0
EOF
fi



if [[ -f "$ES_CA_FILE" ]]; then
  mkdir -p /etc/kibana/certs
  cp -f "$ES_CA_FILE" /etc/kibana/certs/http_ca.crt
  chmod 644 /etc/kibana/certs/http_ca.crt
  set_yaml "elasticsearch.ssl.certificateAuthorities" "[\"/etc/kibana/certs/http_ca.crt\"]"
fi

echo "Fleet air-gap settings applied to ${KIBANA_YML}"
grep -E '^xpack\.fleet\.(isAirGapped|agents\.)' "$KIBANA_YML" || true

systemctl restart kibana
echo "Kibana restarted; waiting for /api/status..."
for i in $(seq 1 120); do
  if curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 http://127.0.0.1:5601/api/status 2>/dev/null | grep -qE '^(200|302|401|503)$'; then
    sleep 5
    echo "Kibana responding (poll ${i})"
    exit 0
  fi
  sleep 5
done
echo "Kibana did not become ready within 10 minutes (may still be starting)" >&2
exit 1