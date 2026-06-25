#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
cmds = [
    "date; uptime; free -h; swapon --show",
    "ss -tnp | grep -E '9200|5601|8220|5334' | head -15",
    "find /var/lib/elastic-agent -name '*.ndjson' -exec tail -15 {} \\; 2>/dev/null",
    "tail -15 /var/log/fleet-install.log",
    "wc -c /var/log/fleet-install.log",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=60)
    o.channel.recv_exit_status()
    print(f"\n=== {cmd} ===\n{(o.read()+e.read()).decode()[-4000:]}")
c.close()