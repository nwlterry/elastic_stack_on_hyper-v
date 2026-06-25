#!/usr/bin/env python3
"""Fix ES service token persistence and verify fleet-server auth."""
import json
import os
import re
import time
import paramiko

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)


def es_cmd(cmd, timeout=120):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.44.40.31", username="root", password=PWD, timeout=30)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    text = (o.read() + e.read()).decode()
    c.close()
    return text


out = es_cmd("/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1")
pwd = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
print(f"elastic={pwd}")

checks = [
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cat/indices/.security*?v'",
    f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cluster/health?pretty'",
    f"curl -sk -u elastic:{pwd} -X POST 'https://localhost:9200/_security/service/elastic/fleet-server/credential/token/fix-token-test?pretty'",
    "/usr/share/elasticsearch/bin/elasticsearch-service-tokens list elastic/fleet-server 2>&1 | tail -10",
]
for cmd in checks:
    print(f"\n=== {cmd[:95]} ===")
    print(es_cmd(cmd)[:3500])

# parse API-created token
api_out = es_cmd(
    f"curl -sk -u elastic:{pwd} -X POST "
    f"'https://localhost:9200/_security/service/elastic/fleet-server/credential/token/fix-api-token?pretty'"
)
print("\n=== API token create ===")
print(api_out)
m = re.search(r'"value"\s*:\s*"(AAEAA[^"]+)"', api_out)
if m:
    tok = m.group(1)
    time.sleep(3)
    auth = es_cmd(
        f"curl -sk -H 'Authorization: Bearer {tok}' https://localhost:9200/_security/_authenticate?pretty"
    )
    print("\n=== API token auth ===")
    print(auth)
    meta = es_cmd(
        f"curl -sk -u elastic:{pwd} "
        f"'https://localhost:9200/_cluster/state/metadata?filter_path=**.service_accounts&pretty'"
    )
    print("\n=== cluster service_accounts ===")
    print(meta)