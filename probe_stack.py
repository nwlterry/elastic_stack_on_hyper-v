#!/usr/bin/env python3
"""Quick probe of ELK stack SSH + Fleet state."""
import re
import shlex
from pathlib import Path

import paramiko

from deploy_ordered_stack import NODES, connect, get_elastic_password

ROOT = Path(__file__).parent
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()).group(1)

IPS = {
    "es01": "10.44.40.31",
    "kibana": "10.44.40.41",
    "fleet": "10.44.40.42",
}


def ssh(ip: str, cmd: str, timeout=60) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PWD, timeout=25)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = (o.read() + e.read()).decode()
    c.close()
    return out


print("=== SSH ===")
for name, ip in IPS.items():
    try:
        out = ssh(ip, "hostname; uptime | head -1", timeout=20)
        print(f"{name} {ip}: OK\n{out.strip()}")
    except Exception as exc:
        print(f"{name} {ip}: FAIL {exc}")

print("\n=== ES elastic password (read-only) ===")
try:
    es = connect(NODES["es01"][0])
    elastic = get_elastic_password(es)
    es.close()
    print("elastic password loaded (see secrets/elastic-password or: python show_elastic_password.py)")
except Exception as exc:
    print(f"FAIL {exc}")
    raise SystemExit(1)

auth = shlex.quote(f"elastic:{elastic}")
kb = IPS["kibana"]

print("\n=== Installed Fleet packages ===")
print(
    ssh(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/fleet/epm/packages/installed' | "
        f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
        f"print('\\n'.join(sorted(f\\\"{{p['name']}}@{{p['version']}}\\\" for p in d.get('items',[]))))\"",
        timeout=60,
    )
)

print("\n=== Agent policies ===")
print(
    ssh(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/fleet/agent_policies?perPage=20' | "
        f"python3 -c \"import sys,json; "
        f"d=json.load(sys.stdin); "
        f"print('\\n'.join(f\\\"{{p['name']}} {{p['id']}} rev={{p.get('revision',0)}}\\\" for p in d.get('items',[])))\"",
        timeout=60,
    )
)

print("\n=== Package policies ===")
print(
    ssh(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50' | "
        f"python3 -c \"import sys,json; "
        f"d=json.load(sys.stdin); "
        f"print('\\n'.join(f\\\"{{p.get('package',{{}}).get('name')}}@{{p.get('package',{{}}).get('version')}} policy={{p.get('policy_id')}}\\\" for p in d.get('items',[])))\"",
        timeout=60,
    )
)

print("\n=== Agents ===")
print(
    ssh(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/fleet/agents?perPage=20' | "
        f"python3 -c \"import sys,json; "
        f"d=json.load(sys.stdin); "
        f"print('\\n'.join(f\\\"{{a.get('local_metadata',{{}}).get('host',{{}}).get('name','?')}} active={{a.get('active')}} status={{a.get('status')}} policy={{a.get('policy_id')}} rev={{a.get('policy_revision')}}\\\" for a in d.get('items',[])))\"",
        timeout=60,
    )
)

print("\n=== EPR staged zips on Kibana ===")
print(ssh(kb, "ls -la /opt/elastic-setup/epr-packages/ 2>/dev/null || echo NONE; ls -la /usr/share/kibana/node_modules/@kbn/fleet-plugin/target/bundled_packages/ 2>/dev/null | head -15", timeout=30))

print("\n=== Fleet agent status (sample) ===")
print(ssh(IPS["fleet"], "elastic-agent status 2>&1 | head -25", timeout=30))