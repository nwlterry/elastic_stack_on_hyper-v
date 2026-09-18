#!/usr/bin/env python3
import re, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)

def q(ip, cmd, timeout=60):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PWD, timeout=30)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    o.channel.recv_exit_status()
    t = (o.read()+e.read()).decode()
    c.close()
    return t

# elastic password
es = paramiko.SSHClient()
es.set_missing_host_key_policy(paramiko.AutoAddPolicy())
es.connect("10.44.40.31", username="root", password=PWD, timeout=30)
_, o, e = es.exec_command("/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
out = (o.read()+e.read()).decode()
ep = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
es.close()
print(f"elastic={ep}")

print("\n=== fleet install log full ===")
print(q("10.44.40.42", "cat /var/log/fleet-install.log"))

print("\n=== enroll network ===")
print(q("10.44.40.42", "ss -tnp | grep 1689 || ss -tnp | grep elastic-agent | head -20"))

print("\n=== kibana fleet settings ===")
print(q("10.44.40.41", f"curl -s -u elastic:{ep} -H 'kbn-xsrf:true' http://localhost:5601/api/fleet/fleet_server_hosts 2>/dev/null"))
print(q("10.44.40.41", f"curl -s -u elastic:{ep} -H 'kbn-xsrf:true' http://localhost:5601/api/fleet/agent_policies 2>/dev/null | head -c 1200"))

print("\n=== ES fleet indices ===")
print(q("10.44.40.31", f"curl -sk -u elastic:{ep} 'https://localhost:9200/_cat/indices/.fleet*?v' 2>/dev/null"))
print(q("10.44.40.31", f"curl -sk -u elastic:{ep} 'https://localhost:9200/.fleet-agents/_search?size=3&pretty' 2>/dev/null | head -80"))

print("\n=== kibana.yml fleet snippet ===")
print(q("10.44.40.41", "grep -i fleet /etc/kibana/kibana.yml 2>/dev/null || echo no_fleet_config"))