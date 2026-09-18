#!/usr/bin/env python3
import sys

from deploy_ordered_stack import AGENT_CLEANUP, NODES, connect, fleet_server_is_healthy, run

if fleet_server_is_healthy(NODES["fleet"][0]):
    print(
        "Fleet Server is HEALTHY on :8220 — skipping kill to avoid disrupting enrollment.",
        flush=True,
    )
    print("Use redeploy_fleet_only.py for a controlled Fleet redeploy.", flush=True)
    sys.exit(0)

c = connect("10.44.40.42")
run(c, AGENT_CLEANUP, check=False, timeout=120)
run(
    c,
    "for pid in $(ps aux | grep -E 'elastic-agent|install-fleet|fleet-server' | grep -v grep | awk '{print $2}'); do "
    "kill -9 \"$pid\" 2>/dev/null || true; done; "
    "sleep 2; "
    "systemctl stop elastic-agent 2>/dev/null; "
    "rm -rf /opt/Elastic /var/lib/elastic-agent /etc/elastic-agent",
    check=False,
    timeout=120,
)
print(run(c, "ps aux | grep -E 'elastic-agent|install-fleet|fleet-server' | grep -v grep || echo CLEAN", check=False))
c.close()