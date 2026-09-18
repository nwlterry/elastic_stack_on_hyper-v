#!/usr/bin/env python3
import os, paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=os.environ["SSH_PASS"], timeout=30)
cmds = [
    "elastic-agent status 2>&1 | head -40",
    "journalctl -u elastic-agent -n 25 --no-pager",
    "ss -tlnp | grep -E '8220|5601|9200' || true",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=30)
    o.channel.recv_exit_status()
    print(f"=== {cmd} ===")
    print((o.read() + e.read()).decode()[-2500:])
c.close()