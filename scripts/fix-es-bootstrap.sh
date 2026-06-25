#!/bin/bash
# Fix cluster bootstrap on node01 when peers are offline (503 on reset-password).
set -euo pipefail

NODE_IP="${NODE_IP:-$(hostname -I | awk '{print $1}')}"

python3 - "$NODE_IP" <<'PY'
import pathlib, re, sys
path = pathlib.Path("/etc/elasticsearch/elasticsearch.yml")
text = path.read_text()
# Remove YAML list and JSON one-liner bootstrap stanzas
text = re.sub(r"(?m)^cluster\.initial_master_nodes:.*(\n(?:  - .*\n)*)?", "", text)
text = re.sub(r"(?m)^discovery\.seed_hosts:.*(\n(?:  - .*\n)*)?", "", text)
node_name = ""
for line in text.splitlines():
    if line.startswith("node.name:"):
        node_name = line.split(":", 1)[1].strip()
        break
if not node_name:
    import socket
    node_name = socket.getfqdn()
seed_ip = sys.argv[1]
block = f"""cluster.initial_master_nodes:
  - {node_name}
discovery.seed_hosts:
  - {seed_ip}:9300
"""
path.write_text(text.rstrip() + "\n" + block)
print(f"bootstrap: node={node_name} seed={seed_ip}")
PY

grep -E -A1 '^(cluster.initial_master_nodes|discovery.seed_hosts)' /etc/elasticsearch/elasticsearch.yml || true
systemctl restart elasticsearch