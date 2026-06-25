#!/usr/bin/env python3
import os
import re
import paramiko

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.31", username="root", password=PWD, timeout=30)
out = c.exec_command("/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)[1].read().decode()
pwd = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
print(f"elastic={pwd}\n")

cmds = [
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cat/indices/.fleet*?v'",
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/.fleet-agents/_search?size=3&pretty' 2>/dev/null | head -60",
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cluster/health?pretty'",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=60)
    print(f"=== {cmd[:80]} ===\n{(o.read()+e.read()).decode()[-3000:]}\n")
c.close()