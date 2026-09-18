#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
cmds = [
    "find /var/lib/elastic-agent -name '*.ndjson' 2>/dev/null | head -5",
    "find /var/lib/elastic-agent -name '*.ndjson' -exec tail -40 {} \\; 2>/dev/null | tail -60",
    "dmesg | grep -iE 'oom|kill' | tail -10 || echo no_oom",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=60)
    o.channel.recv_exit_status()
    print(f"=== {cmd} ===\n{(o.read()+e.read()).decode()[-5000:]}")
c.close()