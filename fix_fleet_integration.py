#!/usr/bin/env python3
"""Add fleet_server integration to Fleet Server policy (unblocks enrollment)."""
import shlex
import sys

from deploy_ordered_stack import NODES, REMOTE, connect, copy_scripts, fleet_server_is_healthy, get_elastic_password, run

FLEET_POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"

es = connect(NODES["es01"][0])
elastic_pwd = get_elastic_password(es)
es.close()

kb = connect(NODES["kibana"][0])
auth = shlex.quote(f"elastic:{elastic_pwd}")
existing = run(
    kb,
    f"curl -s -u {auth} -H 'kbn-xsrf:true' "
    f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50' "
    f"| python3 -c \"import sys,json; "
    f"d=json.load(sys.stdin); "
    f"print(any(p.get('policy_id')=={FLEET_POLICY_ID!r} "
    f"and p.get('package',{{}}).get('name')=='fleet_server' "
    f"for p in d.get('items',[])))\"",
    check=False,
    timeout=60,
).strip()
if existing == "True" and fleet_server_is_healthy():
    print(
        f"fleet_server integration already on policy {FLEET_POLICY_ID} "
        "and Fleet Server is HEALTHY — nothing to do.",
        flush=True,
    )
    kb.close()
    sys.exit(0)
copy_scripts(kb, roles=("kibana",))
fleet_host = NODES["fleet"][1]
out = run(
    kb,
    f"ELASTIC_PASS={shlex.quote(elastic_pwd)} FLEET_HOST={fleet_host} "
    f"SETUP_PHASE=fleet-server bash {REMOTE}/setup-fleet-kibana.sh",
    timeout=300,
)
kb.close()
print(out)