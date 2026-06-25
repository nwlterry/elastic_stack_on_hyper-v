#!/usr/bin/env bash
# Upload integration zips directly to Kibana Fleet (bypasses EPR parsing).
set -euo pipefail

ELASTIC_PASS="${ELASTIC_PASS:-}"
KB="${KB:-http://127.0.0.1:5601}"
EPR_DIR="${EPR_DIR:-/opt/elastic-setup/epr-packages}"

[[ -n "$ELASTIC_PASS" ]] || { echo "Set ELASTIC_PASS" >&2; exit 1; }

declare -A PACKAGES=(
  [system]=1.60.0
  [elasticsearch]=1.12.0
  [kibana]=2.3.1
)

installed="$(curl -s -u "elastic:${ELASTIC_PASS}" -H "kbn-xsrf: true" \
  "${KB}/api/fleet/epm/packages/installed" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(','.join(sorted(p.get('name','') for p in d.get('items',[]))))
" 2>/dev/null || true)"
echo "Currently installed: ${installed:-none}"

for name in system elasticsearch kibana; do
  ver="${PACKAGES[$name]}"
  zip="${EPR_DIR}/${name}-${ver}.zip"
  if echo ",${installed}," | grep -q ",${name},"; then
    echo "SKIP ${name}@${ver} (already installed)"
    continue
  fi
  if [[ ! -f "$zip" ]]; then
    echo "MISSING ${zip}" >&2
    continue
  fi
  echo "UPLOAD ${name}@${ver} ..."
  resp="$(curl -s -u "elastic:${ELASTIC_PASS}" -H "kbn-xsrf: true" \
    -X POST "${KB}/api/fleet/epm/packages" \
    -F "file=@${zip}" 2>&1 || true)"
  echo "  ${resp:0:400}"
done

echo "Final installed:"
curl -s -u "elastic:${ELASTIC_PASS}" -H "kbn-xsrf: true" \
  "${KB}/api/fleet/epm/packages/installed" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for p in d.get('items', []):
    print('  {}@{}'.format(p.get('name'), p.get('version')))
"