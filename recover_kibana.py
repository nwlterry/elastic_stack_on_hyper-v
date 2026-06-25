#!/usr/bin/env python3
"""Restore Kibana after bad air-gap config and re-apply minimal Fleet air-gap settings."""
import sys

from deploy_ordered_stack import NODES, REMOTE, connect, copy_scripts, run, wait_kibana_stable

kb = connect(NODES["kibana"][0])
if wait_kibana_stable(NODES["kibana"][0], max_attempts=3):
    print("Kibana already stable — skipping recovery restart.", flush=True)
    kb.close()
    sys.exit(0)
copy_scripts(kb, roles=("kibana",))

run(
    kb,
    "sed -i '/^xpack\\.fleet\\.agents\\.elasticsearch\\.hosts:/d' /etc/kibana/kibana.yml; "
    "sed -i '/^xpack\\.fleet\\.agents\\.elasticsearch\\.ca_sha256:/d' /etc/kibana/kibana.yml",
    check=False,
)
run(kb, f"FLEET_HOST={NODES['fleet'][1]} bash {REMOTE}/configure-fleet-airgap.sh", timeout=600)
print(run(kb, "systemctl is-active kibana; curl -s -o /dev/null -w 'status:%{http_code}\n' http://127.0.0.1:5601/api/status", check=False))
kb.close()