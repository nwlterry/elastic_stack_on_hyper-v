#!/usr/bin/env python3
from deploy_ordered_stack import NODES, connect, run

kb = connect(NODES["kibana"][0])
cmds = [
    "find /usr/share/kibana -path '*packages/fleet_server*' 2>/dev/null | head -20",
    "find /usr/share/kibana -path '*@kbn/fleet-packages*' 2>/dev/null | head -20",
    "find /usr/share/kibana -name 'manifest.yml' 2>/dev/null | grep fleet | head -10",
    "ls -la /usr/share/kibana/node_modules/@kbn/ 2>/dev/null | grep -i fleet",
    "curl -s http://127.0.0.1:5601/api/fleet/epm/packages/installed 2>/dev/null | head -c 1000 || echo NO_API",
]
for cmd in cmds:
    print(f"\n=== {cmd[:70]} ===\n{run(kb, cmd, check=False, timeout=30)}")
kb.close()