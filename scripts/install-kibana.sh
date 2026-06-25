#!/usr/bin/env bash
# Install Kibana 8.18.4 on RHEL 8.9 and enroll with Elasticsearch cluster.
set -euo pipefail

VERSION="8.18.4"
ES_HOST="10.44.40.31"
ENROLLMENT_TOKEN=""
REPO_FILE="/etc/yum.repos.d/elasticsearch.repo"

usage() {
  cat <<EOF
Usage: $0 [--version 8.18.4] [--es-host IP] [--enrollment-token TOKEN]
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --es-host) ES_HOST="$2"; shift 2 ;;
    --enrollment-token) ENROLLMENT_TOKEN="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1" >&2; usage ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

echo "=== Installing Kibana ${VERSION} ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=elastic-rpm-install.sh
source "${SCRIPT_DIR}/elastic-rpm-install.sh"

if ! install_elastic_rpm_local kibana "$VERSION"; then
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
  dnf install -y --disablerepo='*' --enablerepo=elasticsearch "kibana-${VERSION}"
fi

KIBANA_YML="/etc/kibana/kibana.yml"
cp -a "$KIBANA_YML" "${KIBANA_YML}.bak.$(date +%Y%m%d%H%M%S)"

cat > "$KIBANA_YML" <<EOF
server.host: "0.0.0.0"
server.port: 5601
server.name: "$(hostname -s)"
elasticsearch.hosts: ["https://${ES_HOST}:9200"]
EOF

systemctl daemon-reload
systemctl enable kibana

if [[ -z "$ENROLLMENT_TOKEN" ]]; then
  echo ""
  echo "Kibana installed but not enrolled."
  echo "Get an enrollment token from node01:"
  echo "  /usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana"
  echo ""
  echo "Then enroll:"
  echo "  /usr/share/kibana/bin/kibana-setup --enrollment-token <TOKEN>"
  echo ""
  echo "Or re-run: $0 --es-host ${ES_HOST} --enrollment-token <TOKEN>"
  exit 0
fi

echo "=== Enrolling Kibana with Elasticsearch ==="
/usr/share/kibana/bin/kibana-setup --enrollment-token "$ENROLLMENT_TOKEN"

systemctl start kibana

echo ""
echo "Kibana started. Access: https://$(hostname -I | awk '{print $1}'):5601"
echo "Login with elastic user and the password set during cluster bootstrap."