#!/usr/bin/env python3
from deploy_ordered_stack import NODES, connect, run
kb = connect(NODES["kibana"][0])
for cmd in [
    "systemctl status local-epr --no-pager | tail -20",
    "journalctl -u local-epr -n 15 --no-pager",
    "ss -tlnp | grep 8080 || echo NO_8080",
    "python3 /opt/elastic-setup/local-epr-server.py & sleep 2; curl -s http://127.0.0.1:8080/health; pkill -f local-epr-server || true",
]:
    print(f"\n=== {cmd[:60]} ===\n{run(kb, cmd, check=False, timeout=30)}")
kb.close()