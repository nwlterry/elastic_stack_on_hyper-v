#!/usr/bin/env python3
import shlex
from deploy_ordered_stack import NODES, connect, get_elastic_password, run

es = connect(NODES["es01"][0])
pwd = get_elastic_password(es)
es.close()
auth = shlex.quote(f"elastic:{pwd}")
kb = connect(NODES["kibana"][0])
print(run(kb, f"curl -s -u {auth} -H 'kbn-xsrf:true' 'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50'", timeout=120))
kb.close()