#!/usr/bin/env python3
import shlex
from deploy_ordered_stack import NODES, connect, get_elastic_password, run

es = connect(NODES["es01"][0])
pwd = get_elastic_password(es)
es.close()
auth = shlex.quote(f"elastic:{pwd}")

kb = connect(NODES["kibana"][0])
for path in [
    "/api/fleet/epm/packages/fleet_server/1.6.0",
    "/api/fleet/package_policies?perPage=50",
]:
    print(run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' 'http://127.0.0.1:5601{path}' | head -c 2500",
        check=False,
        timeout=60,
    ))
kb.close()