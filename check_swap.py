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
    "free -h",
    "swapon --show",
    "dmesg -T 2>/dev/null | grep -iE 'oom|kill|out of memory' | tail -10 || echo NO_OOM",
    "ps aux | grep 'elastic-agent.*enroll' | grep -v grep || echo ENROLL_DONE",
    "ss -tlnp | grep 8220 || echo NO_8220",
    "systemctl is-active elastic-agent 2>&1",
    "tail -3 /var/log/fleet-install.log",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=60)
    print(f"=== {cmd} ===\n{(o.read()+e.read()).decode().strip()}\n")
c.close()