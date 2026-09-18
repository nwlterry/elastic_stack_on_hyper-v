#!/usr/bin/env python3
import os, paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.32", username="root", password=os.environ["SSH_PASS"], timeout=30)
cmds = [
    "getent hosts ismelkesnode01.ocplab.net",
    "ping -c2 -W2 10.44.40.31 2>&1 | tail -3",
    "timeout 5 bash -c 'echo > /dev/tcp/10.44.40.31/9200' 2>&1 && echo PORT9200_OK || echo PORT9200_FAIL",
    "curl -sk --connect-timeout 5 https://10.44.40.31:9200 2>&1 | head -c 150",
    "systemctl is-active firewalld 2>&1 || true",
]
for cmd in cmds:
    _, o, e = c.exec_command(cmd, timeout=20)
    o.channel.recv_exit_status()
    print(f"=== {cmd} ===")
    print((o.read() + e.read()).decode())
c.close()