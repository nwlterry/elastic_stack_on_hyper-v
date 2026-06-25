#!/usr/bin/env bash
# Ensure Kibana listens on all interfaces and firewall allows 5601.
set -euo pipefail

KIBANA_YML="/etc/kibana/kibana.yml"

if grep -q '^server\.host:' "$KIBANA_YML"; then
  sed -i 's/^server\.host:.*/server.host: "0.0.0.0"/' "$KIBANA_YML"
else
  echo 'server.host: "0.0.0.0"' >> "$KIBANA_YML"
fi

if ! grep -q '^server\.port:' "$KIBANA_YML"; then
  echo 'server.port: 5601' >> "$KIBANA_YML"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "${SCRIPT_DIR}/configure-kibana-security.sh"
bash "${SCRIPT_DIR}/configure-firewall.sh" kibana || true

systemctl enable kibana
systemctl restart kibana

for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5601 2>/dev/null || true)"
  if [[ "$code" =~ ^(200|302|401|403)$ ]]; then
    echo "Kibana ready on http://$(hostname -I | awk '{print $1}'):5601 (HTTP ${code})"
    ss -tlnp | grep 5601 || true
    exit 0
  fi
  sleep 5
done

echo "Kibana not responding on 5601" >&2
journalctl -u kibana -n 20 --no-pager || true
exit 1