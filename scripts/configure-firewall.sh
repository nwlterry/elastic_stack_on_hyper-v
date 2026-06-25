#!/usr/bin/env bash
# Open Elasticsearch/Kibana/Fleet ports on RHEL 8.
set -euo pipefail

ROLE="${1:-elasticsearch}"

if ! systemctl is-active firewalld &>/dev/null; then
  echo "firewalld not active — skipping"
  exit 0
fi

case "$ROLE" in
  elasticsearch)
    firewall-cmd --permanent --add-port=9200/tcp
    firewall-cmd --permanent --add-port=9300/tcp
    ;;
  kibana)
    firewall-cmd --permanent --add-port=5601/tcp
    ;;
  fleet)
    firewall-cmd --permanent --add-port=8220/tcp
    ;;
esac
firewall-cmd --reload
echo "Firewall configured for role: $ROLE"
firewall-cmd --list-ports