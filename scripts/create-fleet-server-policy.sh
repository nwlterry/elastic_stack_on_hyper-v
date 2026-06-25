#!/usr/bin/env bash
# Run on Kibana node: create fleet_server package policy using installed bundled package.
set -euo pipefail

ELASTIC_PASS="${ELASTIC_PASS:-}"
POLICY_ID="${POLICY_ID:-9be39452-a297-4b8b-9fae-b12ab3cb9315}"
KB="${KB:-http://127.0.0.1:5601}"

[[ -n "$ELASTIC_PASS" ]] || { echo "Set ELASTIC_PASS" >&2; exit 1; }

auth() {
  curl -s --max-time 300 -u "elastic:${ELASTIC_PASS}" -H "kbn-xsrf: true" -H "Content-Type: application/json" "$@"
}

VER="${FLEET_SERVER_VERSION:-}"
if [[ -z "$VER" ]]; then
  RAW="$(auth --max-time 30 "${KB}/api/fleet/epm/packages/fleet_server" || true)"
  VER="$(printf '%s' "$RAW" | python3 -c "
import sys,json
raw=sys.stdin.read().strip()
if not raw:
    print('1.6.0')
    raise SystemExit(0)
try:
    print(json.loads(raw)['item']['version'])
except Exception:
    print('1.6.0')
" 2>/dev/null)"
fi
echo "fleet_server version=${VER}"

EXISTING="$(auth "${KB}/api/fleet/package_policies?perPage=200" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for p in d.get('items',[]):
    if p.get('policy_id')=='${POLICY_ID}' and p.get('package',{}).get('name')=='fleet_server':
        print(p['id']); break
")"

if [[ -n "$EXISTING" ]]; then
  echo "fleet_server integration already exists: ${EXISTING}"
  exit 0
fi

try_create() {
  local label="$1"
  local url="$2"
  local body="$3"
  local resp
  resp="$(auth -X POST "${url}" -d "${body}")"
  echo "try ${label} resp=${resp:0:500}"
  local id
  id="$(printf '%s' "$resp" | python3 -c "
import sys,json
try:
    r=json.load(sys.stdin)
    print(r.get('item',{}).get('id',''))
except Exception:
    print('')
" 2>/dev/null)"
  if [[ -n "$id" ]]; then
    echo "CREATED ${id}"
    exit 0
  fi
}

ARRAY_BODY="$(cat <<EOF
{
  "name": "fleet_server-1",
  "description": "Fleet Server",
  "namespace": "default",
  "policy_id": "${POLICY_ID}",
  "enabled": true,
  "package": {"name": "fleet_server", "version": "${VER}"},
  "inputs": [
    {
      "type": "fleet-server",
      "policy_template": "fleet_server",
      "enabled": true
    }
  ]
}
EOF
)"
try_create "array-format" "${KB}/api/fleet/package_policies" "${ARRAY_BODY}"

LEGACY_BODY="$(cat <<EOF
{
  "name": "fleet_server-1",
  "description": "Fleet Server",
  "namespace": "default",
  "policy_id": "${POLICY_ID}",
  "enabled": true,
  "package": {"name": "fleet_server", "version": "${VER}"},
  "inputs": {
    "fleet_server-fleet-server": {
      "enabled": true
    }
  }
}
EOF
)"
try_create "legacy-format" "${KB}/api/fleet/package_policies?format=legacy" "${LEGACY_BODY}"
echo "Failed to create fleet_server package policy" >&2
exit 1