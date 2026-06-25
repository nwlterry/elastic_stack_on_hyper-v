#!/usr/bin/env python3
"""Enable Kibana Fleet air-gap mode and add fleet_server integration to unblock enrollment."""
import shlex
import time

from deploy_ordered_stack import NODES, REMOTE, connect, copy_scripts, get_elastic_password, run

POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
FLEET_HOST = NODES["fleet"][1]


def main():
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt")
    es.close()
    print(f"elastic={elastic_pwd}", flush=True)

    kb = connect(NODES["kibana"][0])
    copy_scripts(kb, roles=("kibana",))
    run(kb, f"mkdir -p /etc/kibana/certs /etc/elasticsearch/certs")
    run(kb, f"cat > /etc/elasticsearch/certs/http_ca.crt <<'EOF'\n{ca}\nEOF")
    run(
        kb,
        f"FLEET_HOST={FLEET_HOST} bash {REMOTE}/configure-fleet-airgap.sh",
        timeout=600,
    )

    # Ensure fleet_server package policy exists (bundled packages, no epr).
    pkg_script = f'''
import json, base64, urllib.request, ssl

pwd = {elastic_pwd!r}
policy_id = {POLICY_ID!r}
kb = "http://127.0.0.1:5601"
auth = "Basic " + base64.b64encode(("elastic:" + pwd).encode()).decode()

def api(method, path, body=None, timeout=180):
    req = urllib.request.Request(
        kb + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={{"kbn-xsrf": "true", "Content-Type": "application/json", "Authorization": auth}},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

ver = api("GET", "/api/fleet/epm/packages/fleet_server")["item"]["version"]
print("fleet_server version", ver)

items = api("GET", "/api/fleet/package_policies?perPage=200").get("items", [])
existing = None
for p in items:
    if p.get("policy_id") == policy_id and p.get("package", {{}}).get("name") == "fleet_server":
        existing = p
        break

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

if existing:
    r = api("PUT", f"/api/fleet/package_policies/{{existing['id']}}", body)
    print("UPDATED", existing["id"])
else:
    r = api("POST", "/api/fleet/package_policies", body)
    print("CREATED", r.get("item", {{}}).get("id"))

agents = api("GET", "/api/fleet/agents?perPage=20").get("items", [])
for a in agents:
    if a.get("policy_id") == policy_id:
        print("AGENT", a["id"], a.get("status"), a.get("last_checkin_status"))
        break
else:
    print("NO_AGENT_ON_POLICY_YET")
'''
    out = run(kb, f"python3 -c {shlex.quote(pkg_script)}", timeout=600, check=False)
    print(out, flush=True)

    print(run(kb, "journalctl -u kibana -n 8 --no-pager | grep -iE 'fleet|deploy_agent|airgap' || true", check=False))
    kb.close()

    fleet_ip = NODES["fleet"][0]
    print("Waiting for fleet enrollment to complete...", flush=True)
    for i in range(40):
        time.sleep(15)
        c = connect(fleet_ip)
        status = run(
            c,
            "elastic-agent status 2>&1 | head -20; tail -2 /var/log/fleet-install.log",
            check=False,
            timeout=30,
        )
        c.close()
        if "Waiting on fleet-server input" not in status and "└─ status: (HEALTHY)" in status:
            print(f"Fleet enrollment complete (poll {i})", flush=True)
            print(status, flush=True)
            return 0
        if i % 4 == 0:
            print(f"poll {i}: {status[-400:]}", flush=True)
    print("Fleet still waiting on policy deploy — check Kibana journal", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())