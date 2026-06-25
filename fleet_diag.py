#!/usr/bin/env python3
import os, paramiko
PWD = os.environ["SSH_PASS"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
cmds = [
    "ps aux | grep -E 'elastic-agent|install' | grep -v grep",
    "systemctl status elastic-agent --no-pager 2>&1 | head -20",
    "elastic-agent status 2>&1 | head -20",
    "journalctl -u elastic-agent -n 40 --no-pager 2>&1",
    "ss -tlnp | grep 8220 || echo '8220 not listening'",
    "ls -la /opt/Elastic/Agent/ 2>&1 | head -10",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=30)
    o.channel.recv_exit_status()
    print(f"\n=== {cmd} ===")
    print((o.read() + e.read()).decode()[-3000:])
c.close()