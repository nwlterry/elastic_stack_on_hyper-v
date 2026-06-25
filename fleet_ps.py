#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
for cmd in [
    "ps aux | grep -E 'elastic-agent|install-fleet' | grep -v grep",
    "ss -tlnp | grep 8220 || echo NO_8220",
    "systemctl is-active elastic-agent",
    "elastic-agent status 2>&1 | head -25",
    "tail -20 /var/log/fleet-install.log",
]:
    _, o, e = c.exec_command(cmd, timeout=30)
    o.channel.recv_exit_status()
    print(f"\n=== {cmd} ===\n{(o.read()+e.read()).decode()}")
c.close()