#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)

def q(ip, cmd):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PWD, timeout=30)
    _, o, e = c.exec_command(cmd, timeout=60)
    o.channel.recv_exit_status()
    t = (o.read()+e.read()).decode()
    c.close()
    return t

print("=== fleet processes ===")
print(q("10.44.40.42", "ps aux | grep elastic | grep -v grep"))

print("\n=== fleet-install.log tail ===")
print(q("10.44.40.42", "wc -l /var/log/fleet-install.log; tail -80 /var/log/fleet-install.log"))

print("\n=== connectivity from fleet ===")
print(q("10.44.40.42", "curl -s -o /dev/null -w 'kibana:%{http_code}\n' http://ismelkkbnnode01.ocplab.net:5601"))
print(q("10.44.40.42", "curl -sk --cacert /etc/elasticsearch/certs/http_ca.crt -o /dev/null -w 'es:%{http_code}\n' https://ismelkesnode01.ocplab.net:9200"))

print("\n=== kibana fleet health (from kibana) ===")
# get elastic pwd
es = paramiko.SSHClient()
es.set_missing_host_key_policy(paramiko.AutoAddPolicy())
es.connect("10.44.40.31", username="root", password=PWD, timeout=30)
_, o, e = es.exec_command("/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
out = (o.read()+e.read()).decode()
m = re.search(r"New (?:password|value):\s*(\S+)", out)
pwd = m.group(1)
print(f"elastic={pwd}")
es.close()

print(q("10.44.40.41", f"curl -s -u elastic:{pwd} http://localhost:5601/api/fleet/health 2>/dev/null | head -c 500"))
print(q("10.44.40.41", f"curl -s -u elastic:{pwd} http://localhost:5601/api/fleet/agents 2>/dev/null | head -c 800"))