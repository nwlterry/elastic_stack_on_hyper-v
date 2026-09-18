#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
_, o, e = c.exec_command(
    "grep -c install-servers /opt/elastic-setup/install-fleet-server.sh 2>/dev/null || echo 0; "
    "grep -c flock /opt/elastic-setup/install-fleet-server.sh 2>/dev/null || echo 0; "
    "grep es-host /opt/elastic-setup/install-fleet-server.sh | head -1",
    timeout=30,
)
o.channel.recv_exit_status()
print((o.read() + e.read()).decode())
c.close()