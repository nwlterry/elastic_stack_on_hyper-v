#!/usr/bin/env python3
"""Add fleet_server package to Fleet Server policy only."""
import shlex

from deploy_ordered_stack import NODES, connect, get_elastic_password, run

POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"

es = connect(NODES["es01"][0])
pwd = get_elastic_password(es)
es.close()

kb = connect(NODES["kibana"][0])
script = f'''
import json, base64, urllib.request, ssl
pwd = {pwd!r}
policy_id = {POLICY_ID!r}
kb = "http://127.0.0.1:5601"
auth = "Basic " + base64.b64encode(("elastic:" + pwd).encode()).decode()

def api(method, path, body=None):
    req = urllib.request.Request(
        kb + path,
        data=json.dumps(body).encode() if body else None,
        method=method,
        headers={{"kbn-xsrf": "true", "Content-Type": "application/json", "Authorization": auth}},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())

ver = api("GET", "/api/fleet/epm/packages/fleet_server")["item"]["version"]
print("fleet_server version", ver)
items = api("GET", "/api/fleet/package_policies?perPage=200").get("items", [])
for p in items:
    if p.get("policy_id") == policy_id and p.get("package", {{}}).get("name") == "fleet_server":
        print("ALREADY_EXISTS", p["id"])
        raise SystemExit(0)

body = {{
    "name": "fleet_server-1",
    "description": "Fleet Server",
    "namespace": "default",
    "policy_id": policy_id,
    "enabled": True,
    "package": {{"name": "fleet_server", "version": ver}},
    "inputs": {{
        "fleet-server": {{
            "enabled": True,
            "vars": {{"host": "0.0.0.0", "port": 8220}},
        }},
    }},
}}
r = api("POST", "/api/fleet/package_policies", body)
print("CREATED", r.get("item", {{}}).get("id"))
'''
run(kb, f"python3 -c {shlex.quote(script)}", timeout=600)
kb.close()