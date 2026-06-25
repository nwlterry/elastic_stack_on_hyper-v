#!/usr/bin/env bash
# Install Elasticsearch 8.18.4 on RHEL 8.9 with data path on /var/lib/elasticsearch
set -euo pipefail

VERSION="8.18.4"
NODE_NAME=""
CLUSTER_NAME="ism-elk-cluster"
DATA_PATH="${DATA_PATH:-/data/elasticsearch}"
LOG_PATH="/var/log/elasticsearch"
REPO_FILE="/etc/yum.repos.d/elasticsearch.repo"

usage() {
  cat <<EOF
Usage: $0 [--version 8.18.4] [--node NODE_NAME] [--cluster CLUSTER_NAME]
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --node) NODE_NAME="$2"; shift 2 ;;
    --cluster) CLUSTER_NAME="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

if [[ -z "$NODE_NAME" ]]; then
  NODE_NAME=$(hostname -s)
fi

echo "=== System tuning for Elasticsearch ==="
cat > /etc/sysctl.d/99-elasticsearch.conf <<'SYSCTL'
vm.max_map_count=262144
vm.swappiness=1
SYSCTL
sysctl --system

if ! grep -q '^elasticsearch soft memlock' /etc/security/limits.d/elasticsearch.conf 2>/dev/null; then
  cat > /etc/security/limits.d/elasticsearch.conf <<'LIMITS'
elasticsearch soft memlock unlimited
elasticsearch hard memlock unlimited
LIMITS
fi

echo "=== Installing Elasticsearch ${VERSION} ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=elastic-rpm-install.sh
source "${SCRIPT_DIR}/elastic-rpm-install.sh"

if ! rpm -q "elasticsearch-${VERSION}" &>/dev/null; then
  rm -rf /etc/elasticsearch/certs /etc/elasticsearch/elasticsearch.keystore 2>/dev/null || true
  if ! install_elastic_rpm_local elasticsearch "$VERSION"; then
    rpm --import https://artifacts.elastic.co/GPG-KEY-elasticsearch
    cat > "$REPO_FILE" <<EOF
[elasticsearch]
name=Elasticsearch repository for 8.x packages
baseurl=https://artifacts.elastic.co/packages/8.x/yum
gpgcheck=1
gpgkey=https://artifacts.elastic.co/GPG-KEY-elasticsearch
enabled=0
type=rpm-md
EOF
    dnf install -y --disablerepo='*' --enablerepo=elasticsearch "elasticsearch-${VERSION}"
  fi
else
  echo "elasticsearch-${VERSION} already installed — skipping package install"
fi

# Ensure data and log directories exist on the mounted 500GB volume
mkdir -p "$DATA_PATH" "$LOG_PATH"
chown elasticsearch:elasticsearch "$DATA_PATH" "$LOG_PATH"

ES_YML="/etc/elasticsearch/elasticsearch.yml"
cp -a "$ES_YML" "${ES_YML}.bak.$(date +%Y%m%d%H%M%S)"

# Patch only — preserve RPM security autoconfiguration (TLS certs/keystore)
set_config() {
  local key="$1" value="$2"
  if grep -q "^${key}:" "$ES_YML" 2>/dev/null; then
    sed -i "s|^${key}:.*|${key}: ${value}|" "$ES_YML"
  elif grep -q "^#${key}:" "$ES_YML" 2>/dev/null; then
    sed -i "s|^#${key}:.*|${key}: ${value}|" "$ES_YML"
  else
    echo "${key}: ${value}" >> "$ES_YML"
  fi
}

set_config "cluster.name" "${CLUSTER_NAME}"
set_config "node.name" "${NODE_NAME}"
set_config "path.data" "${DATA_PATH}"
set_config "path.logs" "${LOG_PATH}"
set_config "network.host" "0.0.0.0"
set_config "http.port" "9200"
set_config "transport.port" "9300"

# JVM heap: 50% of RAM, capped at 31g
TOTAL_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
HEAP_MB=$(( TOTAL_MB / 2 ))
if (( HEAP_MB > 31744 )); then HEAP_MB=31744; fi
if (( HEAP_MB < 1024 )); then HEAP_MB=1024; fi

JVM_OPTIONS="/etc/elasticsearch/jvm.options.d/heap.options"
mkdir -p "$(dirname "$JVM_OPTIONS")"
cat > "$JVM_OPTIONS" <<EOF
-Xms${HEAP_MB}m
-Xmx${HEAP_MB}m
EOF

systemctl daemon-reload
systemctl enable elasticsearch

echo ""
echo "Elasticsearch ${VERSION} installed. Do NOT start yet on multi-node clusters."
echo "  Node:    ${NODE_NAME}"
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Heap:    ${HEAP_MB}m"
echo ""
echo "Next: run bootstrap-cluster.sh from the first node, or start manually on node01."