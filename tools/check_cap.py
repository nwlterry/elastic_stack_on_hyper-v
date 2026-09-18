#!/usr/bin/env python3
import os
import re
import paramiko

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
cmds = [
    "cat /etc/elastic-agent/capabilities.yml 2>&1 || echo MISSING",
    "ps aux | grep -E 'sleep 2|install-fleet' | grep -v grep",
    "ls -la /etc/elastic-agent/ 2>&1",
    "ls -t /var/log/elastic-agent/*.ndjson 2>/dev/null | head -1 | xargs tail -40 2>/dev/null || echo NO_LOG",
    "ss -tlnp | grep 8220 || echo NO_8220",
    "free -h | head -2",
    "ps aux | grep 'elastic-agent.*enroll' | grep -v grep | awk '{print $3,$4,$6,$11}'",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=60)
    text = (o.read() + e.read()).decode().strip()
    print(f"=== {cmd} ===\n{text[-3000:]}\n")
c.close()