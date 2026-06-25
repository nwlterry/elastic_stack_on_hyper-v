#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.31", username="root", password=PWD, timeout=30)

# latest token from fleet process
tok = "AAEAAWVsYXN0aWMvZmxlZXQtc2VydmVyL2ZsZWV0LXNydi0xNzgyMzE0MzI0OkNNWXZ2Tk54UjlDWG9BLXlmVmJ1UWc"
cmds = [
    f"curl -sk -H 'Authorization: Bearer {tok}' https://localhost:9200/_security/_authenticate?pretty",
    f"curl -sk -H 'Authorization: Bearer {tok}' https://ismelkesnode01.ocplab.net:9200/_cluster/health?pretty",
    "/usr/share/elasticsearch/bin/elasticsearch-service-tokens list elastic/fleet-server 2>&1 | tail -5",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=30)
    o.channel.recv_exit_status()
    print(f"=== {cmd[:80]} ===\n{(o.read()+e.read()).decode()}\n")
c.close()