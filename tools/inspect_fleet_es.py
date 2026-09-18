#!/usr/bin/env python3
import shlex
from deploy_ordered_stack import NODES, connect, get_elastic_password, run

es = connect(NODES["es01"][0])
pwd = get_elastic_password(es)
auth = shlex.quote(f"elastic:{pwd}")
for cmd in [
    f"curl -sk -u {auth} 'https://localhost:9200/_cat/indices/.fleet*?v'",
    f"curl -sk -u {auth} 'https://localhost:9200/.fleet-policies-*/_search?size=3&pretty'",
    f"curl -sk -u {auth} 'https://localhost:9200/.fleet-agents-*/_search?q=policy_id:9be39452*&pretty' | head -80",
]:
    print(f"\n=== {cmd[:70]} ===\n{run(es, cmd, check=False)}")
es.close()