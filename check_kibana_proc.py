#!/usr/bin/env python3
import os
import re
import paramiko

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.41", username="root", password=PWD, timeout=20)
for cmd in [
    "ps aux | grep -E 'setup-fleet|python3' | grep -v grep",
    "curl -s -u elastic:$(grep -oP 'New value: \\K\\S+' /dev/null 2>/dev/null) http://localhost:5601/api/fleet/agent_policies 2>&1 | head -c 200",
]:
    _, o, e = c.exec_command(cmd, timeout=30)
    print(f"$ {cmd}\n{(o.read()+e.read()).decode()}\n")
c.close()