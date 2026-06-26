#!/usr/bin/env bash
# Create or update the Fleet integration monitoring user in Elasticsearch.
set -euo pipefail

MONITORING_USER="${MONITORING_USER:-elastic_monitoring}"
MONITORING_PASS="${MONITORING_PASS:-}"
ELASTIC_USER="${ELASTIC_USER:-elastic}"
ELASTIC_PASS="${ELASTIC_PASS:-}"
ES_URL="${ES_URL:-https://localhost:9200}"
PASSWORD_FILE="/root/.elastic-stack/monitoring-password"

[[ -n "$MONITORING_PASS" && -n "$ELASTIC_PASS" ]] || {
  echo "Set MONITORING_PASS and ELASTIC_PASS" >&2
  exit 1
}

BODY="$(MONITORING_USER="$MONITORING_USER" MONITORING_PASS="$MONITORING_PASS" python3 <<'PY'
import json, os
print(json.dumps({
    "password": os.environ["MONITORING_PASS"],
    "roles": ["monitoring_user", "remote_monitoring_collector", "kibana_user"],
    "full_name": "Fleet stack monitoring",
    "metadata": {"purpose": "fleet-integration-monitoring"},
}))
PY
)"

out="$(curl -sk -u "${ELASTIC_USER}:${ELASTIC_PASS}" \
  -X PUT "${ES_URL}/_security/user/${MONITORING_USER}" \
  -H 'Content-Type: application/json' \
  -d "$BODY" 2>&1)" || true

echo "$out" | tail -5
if ! echo "$out" | grep -qE '"created"|"updated"|"acknowledged"'; then
  echo "Failed to upsert monitoring user ${MONITORING_USER}" >&2
  exit 1
fi

mkdir -p "$(dirname "$PASSWORD_FILE")"
printf '%s\n' "$MONITORING_PASS" > "$PASSWORD_FILE"
chmod 600 "$PASSWORD_FILE"
echo "MONITORING_USER=${MONITORING_USER}"
echo "Saved monitoring password to ${PASSWORD_FILE}"