#!/usr/bin/env bash
# Run bundled-package registry mock for air-gapped Fleet.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EPR_PORT="${EPR_PORT:-8080}"
UNIT="/etc/systemd/system/local-epr.service"

cat > "$UNIT" <<EOF
[Unit]
Description=Local Elastic Package Registry (air-gap)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${SCRIPT_DIR}/local-epr-server.py
Environment=EPR_PORT=${EPR_PORT}
Environment=EPR_PACKAGES=/opt/elastic-setup/epr-packages
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable local-epr
systemctl restart local-epr

for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${EPR_PORT}/health" >/dev/null 2>&1; then
    echo "Local EPR ready on port ${EPR_PORT} (poll ${i})"
    exit 0
  fi
  sleep 2
done
echo "Local EPR did not respond on port ${EPR_PORT}" >&2
journalctl -u local-epr -n 20 --no-pager >&2 || true
exit 1