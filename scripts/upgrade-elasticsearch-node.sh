#!/usr/bin/env bash
# Rolling-upgrade a single Elasticsearch node using a local RPM (air-gapped).
set -euo pipefail

VERSION=""
RPM_DIR="${ELASTIC_RPM_DIR:-/opt/elastic-setup/rpms}"
ES_AUTH=""
MAX_WAIT="${MAX_WAIT:-900}"

usage() {
  echo "Usage: $0 --version VER [--rpm-dir DIR] --es-auth elastic:password"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --rpm-dir) RPM_DIR="$2"; shift 2 ;;
    --es-auth) ES_AUTH="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown: $1" >&2; usage ;;
  esac
done

[[ -n "$VERSION" && -n "$ES_AUTH" ]] || usage
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

RPM="${RPM_DIR}/elasticsearch-${VERSION}-x86_64.rpm"
[[ -f "$RPM" ]] || { echo "Missing RPM: $RPM" >&2; exit 1; }

es_api() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local data=()
  if [[ -n "$body" ]]; then
    data=(-H 'Content-Type: application/json' -d "$body")
  fi
  curl -sk -u "$ES_AUTH" -X "$method" "https://localhost:9200${path}" "${data[@]}"
}

echo "=== Pre-upgrade: disable replica allocation ==="
es_api PUT /_cluster/settings \
  '{"persistent":{"cluster.routing.allocation.enable":"primaries"}}' >/dev/null

echo "=== Flush indices ==="
es_api POST /_flush >/dev/null || true

echo "=== Stop Elasticsearch ==="
systemctl stop elasticsearch.service

echo "=== Install elasticsearch-${VERSION} ==="
# shellcheck source=elastic-rpm-install.sh
source "$(dirname "$0")/elastic-rpm-install.sh"
import_elastic_gpg || true
dnf install -y "$RPM"

echo "=== Start Elasticsearch ==="
systemctl start elasticsearch.service

echo "=== Wait for node to join cluster ==="
deadline=$((SECONDS + MAX_WAIT))
while (( SECONDS < deadline )); do
  if es_api GET /_cluster/health | grep -q '"status"'; then
    if systemctl is-active --quiet elasticsearch.service; then
      name="$(hostname -f)"
      if es_api GET "/_cat/nodes?h=name,version" | grep -F "$name" | grep -F "$VERSION" >/dev/null; then
        echo "Node $name reports version $VERSION"
        break
      fi
    fi
  fi
  sleep 10
done

if ! es_api GET "/_cat/nodes?h=name,version" | grep -F "$(hostname -f)" | grep -F "$VERSION" >/dev/null; then
  echo "FAIL: node did not reach version $VERSION within ${MAX_WAIT}s" >&2
  journalctl -u elasticsearch.service -n 40 --no-pager >&2 || true
  exit 1
fi

echo "=== Wait for cluster green ==="
while (( SECONDS < deadline )); do
  status="$(es_api GET /_cluster/health | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo '')"
  if [[ "$status" == "green" || "$status" == "yellow" ]]; then
    echo "Cluster status: $status"
    break
  fi
  sleep 10
done

echo "=== Re-enable shard allocation ==="
es_api PUT /_cluster/settings \
  '{"persistent":{"cluster.routing.allocation.enable":null}}' >/dev/null

echo "=== Upgrade complete on $(hostname -f) -> ${VERSION} ==="