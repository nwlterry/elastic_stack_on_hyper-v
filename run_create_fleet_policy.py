#!/usr/bin/env python3
import shlex
import sys
import time

from deploy_ordered_stack import NODES, REMOTE, connect, copy_scripts, fleet_server_is_healthy, get_elastic_password, run

FLEET_POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"

es = connect(NODES["es01"][0])
pwd = get_elastic_password(es)
es.close()
print(f"elastic={pwd}", flush=True)

if fleet_server_is_healthy():
    kb = connect(NODES["kibana"][0])
    auth = shlex.quote(f"elastic:{pwd}")
    has_pkg = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        "'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50' "
        f"| python3 -c \"import sys,json; d=json.load(sys.stdin); "
        f"print(any(p.get('policy_id')=={FLEET_POLICY_ID!r} "
        f"and p.get('package',{{}}).get('name')=='fleet_server' "
        f"for p in d.get('items',[])))\"",
        check=False,
        timeout=60,
    ).strip()
    kb.close()
    if has_pkg == "True":
        print(
            f"fleet_server policy already exists on {FLEET_POLICY_ID} "
            "and Fleet Server is HEALTHY — skipping.",
            flush=True,
        )
        sys.exit(0)

kb = connect(NODES["kibana"][0])
copy_scripts(kb, roles=("kibana",))
run(
    kb,
    f"ELASTIC_PASS={shlex.quote(pwd)} bash {REMOTE}/create-fleet-server-policy.sh",
    timeout=300,
)
time.sleep(45)
print(run(kb, "journalctl -u kibana -n 12 --no-pager | grep -iE 'deploy_agent|fleet_server|error' || true", check=False))
kb.close()

fl = connect(NODES["fleet"][0])
for i in range(12):
    status = run(fl, "elastic-agent status 2>&1 | head -22", check=False)
    print(f"\n=== poll {i} ===\n{status}")
    if "Waiting on fleet-server input" not in status:
        break
    time.sleep(20)
fl.close()