#!/usr/bin/env bash
# Install Fleet integration packages via local EPR / Fleet API.
set -euo pipefail

ELASTIC_PASS="${ELASTIC_PASS:-}"
KB="${KB:-http://127.0.0.1:5601}"

[[ -n "$ELASTIC_PASS" ]] || { echo "Set ELASTIC_PASS" >&2; exit 1; }

auth() {
  curl -s --max-time 300 -u "elastic:${ELASTIC_PASS}" -H "kbn-xsrf: true" -H "Content-Type: application/json" "$@"
}

declare -A PACKAGES=(
  [fleet_server]=1.6.0
  [elastic_agent]=2.3.0
  [system]=1.60.0
  [elasticsearch]=1.12.0
  [kibana]=2.3.1
)

installed="$(auth "${KB}/api/fleet/epm/packages/installed" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(','.join(sorted(p.get('name','') for p in d.get('items',[]))))
" 2>/dev/null || true)"
echo "Currently installed: ${installed:-none}"

for name in fleet_server elastic_agent system elasticsearch kibana; do
  ver="${PACKAGES[$name]}"
  if echo ",${installed}," | grep -q ",${name},"; then
    echo "SKIP ${name}@${ver} (already installed)"
    continue
  fi
  echo "INSTALL ${name}@${ver} ..."
  resp="$(auth -X POST "${KB}/api/fleet/epm/packages/${name}/${ver}" 2>&1 || true)"
  echo "  ${resp:0:300}"
done

echo "Final installed:"
auth "${KB}/api/fleet/epm/packages/installed" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for p in d.get('items', []):
    print('  {}@{}'.format(p.get('name'), p.get('version')))
"