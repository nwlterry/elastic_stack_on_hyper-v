#!/usr/bin/env python3
import json
import shlex
from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run
from fix_dashboard_search import kibana_curl

c = connect(NODES["es01"][0])
auth = curl_elastic_auth(get_elastic_password(c))
print("=== SNAPSHOT ===")
print(
    run(
        c,
        f"curl -sk -u {auth} "
        f"'https://localhost:9200/_snapshot/fs_nfs_snapshots/pre-upgrade-system-20260724-0335"
        f"?pretty&filter_path=snapshots.snapshot,snapshots.state,snapshots.shards,"
        f"snapshots.include_global_state,snapshots.feature_states.feature_name,"
        f"snapshots.failures,snapshots.duration_in_millis,snapshots.metadata'",
        check=False,
    )
)
print("=== CLUSTER SETTINGS (monitoring) ===")
print(
    run(
        c,
        f"curl -sk -u {auth} "
        f"'https://localhost:9200/_cluster/settings?flat_settings=true&include_defaults=false&pretty'",
        check=False,
    )[:800]
)
print("=== NODE_STATS ROLES (15m) ===")
body = {
    "size": 0,
    "query": {"range": {"@timestamp": {"gte": "now-15m"}}},
    "aggs": {
        "nodes": {
            "terms": {"field": "elasticsearch.node.name", "size": 10},
            "aggs": {
                "with_roles": {"filter": {"exists": {"field": "elasticsearch.node.roles"}}}
            },
        }
    },
}
print(
    run(
        c,
        f"curl -sk -u {auth} -H 'Content-Type: application/json' "
        f"-d {shlex.quote(json.dumps(body))} "
        f"'https://localhost:9200/.ds-metrics-elasticsearch.stack_monitoring.node_stats-*/_search?pretty'",
        check=False,
    )[:2000]
)
c.close()

kb = connect(NODES["kibana"][0])
es = connect(NODES["es01"][0])
auth2 = curl_elastic_auth(get_elastic_password(es))
es.close()
print("=== FLEET es04 ===")
for a in (kibana_curl(kb, auth2, "GET", "/api/fleet/agents?perPage=100").get("items") or []):
    host = ((a.get("local_metadata") or {}).get("host") or {}).get("hostname") or ""
    if "esnode04" in host.lower() or "ismelkesnode04" in host:
        print(a.get("id"), host, a.get("status"), a.get("policy_id"))
pkgs = kibana_curl(kb, auth2, "GET", "/api/fleet/package_policies?perPage=200")
for pp in pkgs.get("items") or []:
    if pp.get("policy_id") == "1408bc34-132f-4b22-ba85-776e09f4e3cc":
        print("pkg", pp.get("name"), (pp.get("package") or {}).get("name"))
kb.close()
