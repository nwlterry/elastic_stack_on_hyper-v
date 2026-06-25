#!/usr/bin/env python3
import shlex
from deploy_ordered_stack import NODES, connect, get_elastic_password, run

es = connect(NODES["es01"][0])
pwd = get_elastic_password(es)
es.close()
auth = shlex.quote(f"elastic:{pwd}")

kb = connect(NODES["kibana"][0])
cmds = [
    "grep -E 'isAirGapped|fleet.outputs|fleet_server' /etc/kibana/kibana.yml",
    "find /usr/share/kibana -maxdepth 4 -type d -name '*fleet*' 2>/dev/null | head -15",
    "find /usr/share/kibana -name 'fleet_server*' 2>/dev/null | head -10",
    "ls -la /usr/share/kibana/data/fleet 2>/dev/null | head -20 || echo NO_FLEET_DATA",
    f"timeout 15 curl -s -u {auth} -H 'kbn-xsrf:true' http://127.0.0.1:5601/api/fleet/epm/packages/fleet_server -w '\\nhttp:%{{http_code}}' || echo CURL_TIMEOUT",
    "journalctl -u kibana -n 20 --no-pager | grep -iE 'epr|fleet|airgap|registry' || true",
]
for cmd in cmds:
    print(f"\n=== {cmd[:80]} ===\n{run(kb, cmd, check=False, timeout=60)}")
kb.close()