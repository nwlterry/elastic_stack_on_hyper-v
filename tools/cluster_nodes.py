#!/usr/bin/env python3
import os
import re
import paramiko

cfg = open("config.psd1").read()
PWD = os.environ.get("SSH_PASS") or re.search(r"RootPassword\s*=\s*'([^']+)'", cfg).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.31", username="root", password=PWD, timeout=30)

_, o, _ = c.exec_command(
    "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120
)
out = o.read().decode()
m = re.search(r"New (?:password|value):\s*(\S+)", out)
pwd = m.group(1) if m else "?"
print(f"elastic={pwd}")

for cmd in [
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cat/nodes?v'",
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cluster/health?pretty'",
]:
    _, o, _ = c.exec_command(cmd, timeout=30)
    print(o.read().decode())

c.close()