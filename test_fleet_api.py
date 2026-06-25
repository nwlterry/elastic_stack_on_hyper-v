#!/usr/bin/env python3
import os, re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
es = paramiko.SSHClient()
es.set_missing_host_key_policy(paramiko.AutoAddPolicy())
es.connect("10.44.40.31", username="root", password=PWD, timeout=30)
_, o, e = es.exec_command("/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
out = (o.read()+e.read()).decode()
ep = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
es.close()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.41", username="root", password=PWD, timeout=30)
_, o, e = c.exec_command(
    f"ELASTIC_PASS='{ep}' bash /opt/elastic-setup/setup-fleet-kibana.sh 2>&1",
    timeout=120,
)
# copy updated script first
from scp import SCPClient
with SCPClient(c.get_transport()) as scp:
    scp.put(str(Path("scripts/setup-fleet-kibana.sh")), "/opt/elastic-setup/setup-fleet-kibana.sh")
c.close()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.41", username="root", password=PWD, timeout=30)
_, o, e = c.exec_command(f"chmod +x /opt/elastic-setup/setup-fleet-kibana.sh; ELASTIC_PASS='{ep}' bash /opt/elastic-setup/setup-fleet-kibana.sh 2>&1", timeout=120)
o.channel.recv_exit_status()
print((o.read()+e.read()).decode())
c.close()