#!/usr/bin/env python3
import os, re, paramiko

PWD = os.environ["SSH_PASS"]
checks = [
    ("10.44.40.31", "curl -sk -u elastic:PWD 'https://localhost:9200/_cluster/health?pretty' 2>/dev/null | grep -E 'status|number_of_nodes'"),
    ("10.44.40.41", "systemctl is-active kibana; curl -s -o /dev/null -w 'kibana:%{http_code}' http://localhost:5601"),
    ("10.44.40.42", "systemctl is-active elastic-agent 2>&1; curl -sk -o /dev/null -w 'fleet:%{http_code}' https://localhost:8220 2>/dev/null || echo fleet:down"),
]
c0 = paramiko.SSHClient()
c0.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c0.connect("10.44.40.31", username="root", password=PWD, timeout=30)
_, o, e = c0.exec_command("/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
out = (o.read() + e.read()).decode()
m = re.search(r"New (?:password|value):\s*(\S+)", out)
elastic = m.group(1) if m else "?"
c0.close()
print(f"elastic={elastic}")
for ip, cmd in checks:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PWD, timeout=30)
    cmd2 = cmd.replace("PWD", elastic)
    _, o, e = c.exec_command(cmd2, timeout=30)
    o.channel.recv_exit_status()
    print(f"\n{ip}:")
    print((o.read() + e.read()).decode().strip())
    c.close()