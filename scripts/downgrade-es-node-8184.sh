#!/usr/bin/env bash
# Downgrade a single Elasticsearch node to 8.18.4 (wipe data; keep config).
set -euo pipefail
TARGET="${1:-8.18.4}"
RPM_DIR="${ELASTIC_RPM_DIR:-/opt/elastic-setup/rpms}"
RPM="${RPM_DIR}/elasticsearch-${TARGET}-x86_64.rpm"

echo "=== pre ==="
rpm -q elasticsearch || true
systemctl stop elasticsearch 2>/dev/null || true
sleep 2
pkill -9 -f org.elasticsearch 2>/dev/null || true
sleep 1

echo "=== remove ==="
dnf remove -y elasticsearch || rpm -e --nodeps elasticsearch || true
if rpm -q elasticsearch &>/dev/null; then
  echo "FAIL: elasticsearch still installed after remove"
  rpm -q elasticsearch
  exit 11
fi

[[ -f "$RPM" ]] || { echo "FAIL missing $RPM"; ls -la "$RPM_DIR"; exit 12; }
if [[ -f "${RPM_DIR}/GPG-KEY-elasticsearch" ]]; then
  rpm --import "${RPM_DIR}/GPG-KEY-elasticsearch" || true
fi

echo "=== install ${TARGET} ==="
dnf install -y "$RPM"
rpm -q "elasticsearch-${TARGET}" || rpm -q elasticsearch

echo "=== wipe data ==="
systemctl stop elasticsearch 2>/dev/null || true
for d in /data/elasticsearch /var/lib/elasticsearch; do
  if [[ -d "$d" ]]; then
    echo "wiping $d"
    rm -rf "${d:?}/"*
    mkdir -p "$d"
    chown -R elasticsearch:elasticsearch "$d" || true
  fi
done
mkdir -p /var/log/elasticsearch
chown -R elasticsearch:elasticsearch /var/log/elasticsearch || true

mkdir -p /mnt/es-snapshots
if ! mountpoint -q /mnt/es-snapshots; then
  mount -t nfs 10.44.40.41:/export/es-snapshots /mnt/es-snapshots 2>/dev/null || \
  mount -t nfs 10.44.40.41:/mnt/es-snapshots /mnt/es-snapshots 2>/dev/null || true
fi
timeout 5 ls /mnt/es-snapshots >/dev/null 2>&1 && echo NFS_OK || echo NFS_WARN

YML=/etc/elasticsearch/elasticsearch.yml
if [[ -f "$YML" ]]; then
  if ! grep -q 'ismelkesnode01' "$YML" 2>/dev/null; then
    cat >>"$YML" <<'EOF'

discovery.seed_hosts:
  - ismelkesnode01.ocplab.net
  - ismelkesnode02.ocplab.net
  - ismelkesnode03.ocplab.net
  - ismelkesnode04.ocplab.net
EOF
  fi
  if ! grep -q 'cluster.initial_master_nodes' "$YML"; then
    cat >>"$YML" <<'EOF'
cluster.initial_master_nodes:
  - ismelkesnode01.ocplab.net
  - ismelkesnode02.ocplab.net
  - ismelkesnode03.ocplab.net
EOF
    echo "added initial_master_nodes"
  fi
fi

echo "=== start ==="
systemctl daemon-reload || true
systemctl enable elasticsearch
systemctl start elasticsearch
sleep 8
systemctl is-active elasticsearch
rpm -q elasticsearch
curl -sk -m 8 https://localhost:9200/ | head -c 400 || true
echo
echo "=== DONE node $(hostname -f) ==="
