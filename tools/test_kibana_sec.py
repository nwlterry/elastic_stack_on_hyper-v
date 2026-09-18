#!/usr/bin/env python3
import os
import re
import paramiko
from scp import SCPClient

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.41", username="root", password=PWD, timeout=20)
with SCPClient(c.get_transport()) as scp:
    scp.put("scripts/configure-kibana-security.sh", "/opt/elastic-setup/configure-kibana-security.sh")
    scp.put("scripts/enable-stack-monitoring.sh", "/opt/elastic-setup/enable-stack-monitoring.sh")
c.exec_command("chmod +x /opt/elastic-setup/configure-kibana-security.sh /opt/elastic-setup/enable-stack-monitoring.sh")
_, o, e = c.exec_command(
    "/usr/share/kibana/bin/kibana-encryption-keys generate 2>&1 | head -5; "
    "bash -x /opt/elastic-setup/configure-kibana-security.sh 2>&1; echo EXIT:$?",
    timeout=60,
)
print((o.read() + e.read()).decode())
c.close()