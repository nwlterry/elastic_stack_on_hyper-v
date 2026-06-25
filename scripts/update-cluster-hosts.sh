#!/usr/bin/env bash
# Add ELK cluster FQDNs to /etc/hosts on every node (no external DNS required).
set -euo pipefail

MARKER_START="# BEGIN ISM-ELK-CLUSTER"
MARKER_END="# END ISM-ELK-CLUSTER"
DOMAIN="${CLUSTER_DOMAIN:-ocplab.net}"

read -r -d '' HOST_BLOCK <<EOF || true
${MARKER_START}
10.44.40.31    ismelkesnode01.${DOMAIN}    ismelkesnode01
10.44.40.32    ismelkesnode02.${DOMAIN}    ismelkesnode02
10.44.40.33    ismelkesnode03.${DOMAIN}    ismelkesnode03
10.44.40.41    ismelkkbnnode01.${DOMAIN}    ismelkkbnnode01
10.44.40.42    ismelkflnode01.${DOMAIN}    ismelkflnode01
${MARKER_END}
EOF

if grep -q "$MARKER_START" /etc/hosts; then
  python3 - <<'PY'
import pathlib, re
path = pathlib.Path("/etc/hosts")
text = path.read_text()
text = re.sub(r"(?ms)# BEGIN ISM-ELK-CLUSTER.*?# END ISM-ELK-CLUSTER\n?", "", text)
path.write_text(text.rstrip() + "\n")
PY
fi

printf '%s\n' "$HOST_BLOCK" >> /etc/hosts
echo "Updated /etc/hosts with ELK cluster FQDNs"
grep -A6 "$MARKER_START" /etc/hosts