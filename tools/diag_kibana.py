#!/usr/bin/env python3
from deploy_ordered_stack import NODES, connect, run

kb = connect(NODES["kibana"][0])
for cmd in [
    "systemctl is-active kibana; systemctl status kibana --no-pager | tail -15",
    "journalctl -u kibana -n 40 --no-pager",
    "grep -E '^xpack\.fleet|^elasticsearch\.ssl' /etc/kibana/kibana.yml | tail -20",
    "curl -s -o /dev/null -w 'status:%{http_code}\n' http://127.0.0.1:5601/api/status || true",
]:
    print(f"\n=== {cmd} ===\n{run(kb, cmd, check=False)}")
kb.close()