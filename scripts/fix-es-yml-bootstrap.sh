#!/bin/bash
# Clean elasticsearch.yml: remove bootstrap master list, keep a single discovery.seed_hosts.
set -euo pipefail

YML="/etc/elasticsearch/elasticsearch.yml"
SEED_HOSTS="${SEED_HOSTS:-10.44.40.31:9300,10.44.40.32:9300,10.44.40.33:9300}"

[[ -f "$YML" ]] || { echo "Missing ${YML}" >&2; exit 1; }

CHANGED=$(python3 - "$YML" "$SEED_HOSTS" <<'PY'
import re
import sys

path = sys.argv[1]
seed_hosts = [h.strip() for h in sys.argv[2].split(",") if h.strip()]
with open(path, "r", encoding="utf-8") as fh:
    lines = fh.read().splitlines()

out = []
skip = False
removed_masters = 0
removed_seeds = 0
for line in lines:
    if re.match(r"^cluster\.initial_master_nodes:", line):
        skip = True
        removed_masters += 1
        continue
    if re.match(r"^discovery\.seed_hosts:", line):
        skip = True
        removed_seeds += 1
        continue
    if skip:
        if re.match(r"^\s+-\s+", line):
            continue
        skip = False
    if re.match(r"^#.*cluster\.initial_master_nodes:", line):
        continue
    if re.match(r"^#.*discovery\.seed_hosts:", line):
        continue
    out.append(line)

seed_block = ["discovery.seed_hosts:"] + ["  - %s" % h for h in seed_hosts]
new_text = "\n".join(out).rstrip() + "\n" + "\n".join(seed_block) + "\n"

with open(path, "r", encoding="utf-8") as fh:
    old_text = fh.read()

if new_text == old_text:
    print("unchanged")
else:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    print("changed removed_masters=%d removed_seeds=%d" % (removed_masters, removed_seeds))
PY
)

grep -E -A3 '^(discovery.seed_hosts)' "$YML" || true
grep -E '^(cluster.initial_master_nodes)' "$YML" || echo "cluster.initial_master_nodes=none"

if [[ "$CHANGED" == *unchanged* ]]; then
  echo "elasticsearch_yml=unchanged skip_restart"
  exit 0
fi

chown -R root:elasticsearch /etc/elasticsearch
chmod 2770 /etc/elasticsearch
if [[ -f /etc/elasticsearch/service_tokens ]]; then
  chmod 660 /etc/elasticsearch/service_tokens
fi
if [[ -f /etc/elasticsearch/elasticsearch.keystore ]]; then
  chown elasticsearch:elasticsearch /etc/elasticsearch/elasticsearch.keystore
fi
chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch 2>/dev/null || true

systemctl restart elasticsearch
for i in $(seq 1 36); do
  if curl -sk --connect-timeout 2 -u elastic:"${ELASTIC_PASS:-}" "https://127.0.0.1:9200/" -o /dev/null 2>/dev/null; then
    echo "elasticsearch_active=poll_${i}"
    exit 0
  fi
  sleep 5
done
echo "elasticsearch_restart=timeout" >&2
systemctl is-active elasticsearch || true
exit 1