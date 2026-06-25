#!/usr/bin/env python3
import os
import re
import paramiko

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.41", username="root", password=PWD, timeout=30)

cmds = [
    "date; hostname -f",
    "systemctl is-active kibana elastic-agent 2>&1",
    "ss -tlnp | grep -E '5601|8220' || echo NO_PORTS",
    "ps aux | grep -E 'elastic-agent|install-fleet' | grep -v grep || echo NO_PROC",
    "free -h | head -3; swapon --show",
    "tail -15 /var/log/fleet-install.log 2>/dev/null || echo NO_LOG",
    "elastic-agent status 2>&1 | head -20",
]
for cmd in cmds:
    print(f"\n=== {cmd[:60]} ===")
    _, o, e = c.exec_command(cmd, timeout=60)
    print((o.read() + e.read()).decode())

c.close()