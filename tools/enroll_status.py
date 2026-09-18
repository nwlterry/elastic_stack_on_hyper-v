#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
cmds = [
    "date; ps -p 7332 -o etime,pcpu,pmem,rss,cmd 2>/dev/null || ps aux | grep enroll | grep -v grep",
    "ss -tnp | grep 7332 || ss -tnp | head -20",
    "curl -sk --connect-timeout 3 https://ismelkesnode01.ocplab.net:9200 -o /dev/null -w 'es:%{http_code}\\n'",
    "tail -5 /var/lib/elastic-agent/data/elastic-agent-8.18.4-5307a6/logs/*.ndjson 2>/dev/null",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=30)
    o.channel.recv_exit_status()
    print(f"\n=== {cmd[:60]} ===\n{(o.read()+e.read()).decode()[-2000:]}")
c.close()