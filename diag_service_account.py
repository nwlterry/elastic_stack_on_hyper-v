#!/usr/bin/env python3
import os
import re
import time
import paramiko

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.31", username="root", password=PWD, timeout=30)

out = c.exec_command(
    "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120
)[1].read().decode()
pwd = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
print(f"elastic={pwd}")

cmds = [
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_security/service/elastic/fleet-server?pretty'",
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_security/service?pretty' | head -80",
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cluster/state/metadata?filter_path=**.service_accounts&pretty'",
    "/usr/share/elasticsearch/bin/elasticsearch-service-tokens create elastic/fleet-server diag-test 2>&1",
]
for cmd in cmds:
    print(f"\n=== {cmd[:90]} ===")
    _, o, e = c.exec_command(cmd, timeout=60)
    text = (o.read() + e.read()).decode()
    print(text[:3000])

# test fresh token auth
m = re.search(r"(AAEAA[\w+/=-]+)", text)
if m:
    tok = m.group(1)
    time.sleep(2)
    _, o, e = c.exec_command(
        f"curl -sk -H 'Authorization: Bearer {tok}' https://localhost:9200/_security/_authenticate?pretty",
        timeout=30,
    )
    print("\n=== authenticate fresh token ===")
    print((o.read() + e.read()).decode())

c.close()