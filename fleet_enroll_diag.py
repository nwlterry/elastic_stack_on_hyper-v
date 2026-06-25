#!/usr/bin/env python3
import os
import re
import paramiko

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)

for ip, name in [("10.44.40.42", "fleet"), ("10.44.40.41", "kibana"), ("10.44.40.31", "es01")]:
    print(f"\n{'='*60}\n{name} ({ip})\n{'='*60}")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PWD, timeout=30)
    cmds = [
        "date",
        "ps aux | grep -E 'elastic-agent.*enroll' | grep -v grep || echo NO_ENROLL",
        "grep -E 'fleet_server|xpack.fleet' /etc/kibana/kibana.yml 2>/dev/null | head -20 || echo NO_KIBANA_CFG",
        "curl -sk --connect-timeout 3 https://ismelkesnode01.ocplab.net:9200 2>&1 | head -c 120",
        "curl -s --connect-timeout 3 http://ismelkkbnnode01.ocplab.net:5601/api/status 2>&1 | head -c 200",
        "ls -t /var/log/elastic-agent/*.ndjson 2>/dev/null | head -1 | xargs tail -15 2>/dev/null || "
        "find /var/lib/elastic-agent -name '*.ndjson' 2>/dev/null | head -1 | xargs tail -15 2>/dev/null || echo NO_AGENT_LOG",
        "tail -5 /var/log/fleet-install.log 2>/dev/null || true",
    ]
    for cmd in cmds:
        _, o, e = c.exec_command(cmd, timeout=60)
        text = (o.read() + e.read()).decode().strip()
        if text:
            print(f"\n$ {cmd[:70]}\n{text[-2000:]}")
    c.close()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.31", username="root", password=PWD, timeout=30)
out = c.exec_command("/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)[1].read().decode()
pwd = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
_, o, _ = c.exec_command(
    f"curl -sk -u elastic:{pwd} 'http://10.44.40.41:5601/api/fleet/settings' 2>&1 | head -c 1500",
    timeout=30,
)
print(f"\n{'='*60}\nfleet settings via kibana API\n{'='*60}\n{o.read().decode()}")
c.close()