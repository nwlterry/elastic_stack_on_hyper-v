#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
for cmd in [
    "free -h",
    "ps aux | grep elastic-agent | grep -v grep",
    "tail -50 /var/log/elastic-agent/elastic-agent-20260624-7.ndjson",
    "find /var/lib/elastic-agent -name '*.log' -exec tail -20 {} \\; 2>/dev/null | tail -40",
]:
    _, o, e = c.exec_command(cmd, timeout=60)
    o.channel.recv_exit_status()
    print(f"\n=== {cmd} ===\n{(o.read()+e.read()).decode()[-5000:]}")
c.close()