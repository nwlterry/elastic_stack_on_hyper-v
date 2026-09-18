#!/usr/bin/env python3
import shlex
from deploy_ordered_stack import NODES, connect, get_elastic_password, run

es = connect(NODES["es01"][0])
pwd = get_elastic_password(es)
es.close()

kb = connect(NODES["kibana"][0])
for cmd in [
    f"curl -s -u elastic:{shlex.quote(pwd)} -H 'kbn-xsrf:true' "
    "'http://127.0.0.1:5601/api/fleet/epm/packages/fleet_server' | head -c 800",
    f"curl -s -u elastic:{shlex.quote(pwd)} -H 'kbn-xsrf:true' "
    "'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50' | python3 -c \"import sys,json; "
    "d=json.load(sys.stdin); [print(p.get('package',{}).get('name'), p.get('name'), p.get('policy_id')) "
    "for p in d.get('items',[]) if '9be39452' in p.get('policy_id','')]\"",
    "journalctl -u kibana -n 15 --no-pager | grep -iE 'fleet|deploy_agent' || true",
]:
    print(run(kb, cmd, check=False))
kb.close()