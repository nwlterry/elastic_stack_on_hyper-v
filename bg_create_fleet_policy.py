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
            f"fleet_server policy already on {FLEET_POLICY_ID}, Fleet HEALTHY — skipping.",
            flush=True,
        )
        sys.exit(0)

kb = connect(NODES["kibana"][0])
copy_scripts(kb, roles=("kibana",))
run(
    kb,
    f"nohup env ELASTIC_PASS={shlex.quote(pwd)} bash {REMOTE}/create-fleet-server-policy.sh "
    f"> /var/log/create-fleet-server-policy.log 2>&1 &",
    timeout=30,
)
for i in range(40):
    time.sleep(15)
    out = run(
        kb,
        "tail -5 /var/log/create-fleet-server-policy.log 2>/dev/null; "
        f"curl -s -u {shlex.quote(f'elastic:{pwd}')} -H 'kbn-xsrf:true' "
        "'http://127.0.0.1:5601/api/fleet/package_policies?perPage=20' | "
        "python3 -c \"import sys,json; d=json.load(sys.stdin); print('count',d.get('total',0),"
        "[(p.get('package',{}).get('name'),p.get('policy_id')) for p in d.get('items',[])])\"",
        check=False,
        timeout=60,
    )
    print(f"poll {i}: {out[-500:]}", flush=True)
    if "fleet_server" in out and "9be39452" in out:
        break
kb.close()

fl = connect(NODES["fleet"][0])
print(run(fl, "elastic-agent status 2>&1 | head -20", check=False))
fl.close()