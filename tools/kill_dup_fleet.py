#!/usr/bin/env python3
"""Kill duplicate fleet install (newer), keep older enroll running."""
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
# kill newer install started at 00:38 (PIDs 7823+)
cmds = [
    "kill -9 8028 8017 7823 2>/dev/null || true",
    "ps aux | grep install-fleet-server | grep -v grep",
    "ps aux | grep 'elastic-agent.*enroll' | grep -v grep",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=30)
    o.channel.recv_exit_status()
    print((o.read()+e.read()).decode())
c.close()