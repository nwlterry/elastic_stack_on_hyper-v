#!/usr/bin/env python3
import shlex
import time

from deploy_ordered_stack import NODES, connect, get_elastic_password, run

POLICY = "9be39452-a297-4b8b-9fae-b12ab3cb9315"


def main():
    es = connect(NODES["es01"][0])
    pwd = get_elastic_password(es)
    es.close()
    print(f"elastic={pwd}", flush=True)

    kb = connect(NODES["kibana"][0])
    auth = shlex.quote(f"elastic:{pwd}")
    for i in range(30):
        code = run(
            kb,
            f"curl -s -o /dev/null -w '%{{http_code}}' -u {auth} -H 'kbn-xsrf:true' "
            f"http://127.0.0.1:5601/api/status",
            check=False,
            timeout=30,
        ).strip()
        if code == "200":
            break
        print(f"kibana wait {i}: {code}", flush=True)
        time.sleep(10)

    print(run(kb, f"curl -s -u {auth} -H 'kbn-xsrf:true' "
          f"'http://127.0.0.1:5601/api/fleet/epm/packages/fleet_server' | head -c 500", check=False, timeout=120))

    print(run(kb, f"curl -s -u {auth} -H 'kbn-xsrf:true' "
          f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50' | "
          f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
          f"[print(p.get('package',{{}}).get('name'), p.get('id')) for p in d.get('items',[]) "
          f"if p.get('policy_id')=='{POLICY}']\"", check=False, timeout=120))

    script = f'''
import json, base64, urllib.request, time
pwd = {pwd!r}
policy_id = {POLICY!r}
kb = "http://127.0.0.1:5601"
auth = "Basic " + base64.b64encode(("elastic:" + pwd).encode()).decode()

def api(method, path, body=None):
    for attempt in range(8):
        try:
            req = urllib.request.Request(
                kb + path,
                data=json.dumps(body).encode() if body is not None else None,
                method=method,
                headers={{"kbn-xsrf": "true", "Content-Type": "application/json", "Authorization": auth}},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print("retry", attempt, e, flush=True)
            time.sleep(10)
    raise SystemExit("api failed")

ver = api("GET", "/api/fleet/epm/packages/fleet_server")["item"]["version"]
print("version", ver, flush=True)
items = api("GET", "/api/fleet/package_policies?perPage=200").get("items", [])
for p in items:
    if p.get("policy_id") == policy_id and p.get("package", {{}}).get("name") == "fleet_server":
        print("EXISTS", p["id"], flush=True)
        raise SystemExit(0)
body = {{
    "name": "fleet_server-1",
    "description": "Fleet Server",
    "namespace": "default",
    "policy_id": policy_id,
    "enabled": True,
    "package": {{"name": "fleet_server", "version": ver}},
    "inputs": {{"fleet-server": {{"enabled": True, "vars": {{"host": "0.0.0.0", "port": 8220}}}}}},
}}
r = api("POST", "/api/fleet/package_policies", body)
print("CREATED", r.get("item", {{}}).get("id"), flush=True)
'''
    print(run(kb, f"python3 -c {shlex.quote(script)}", timeout=900, check=False))
    print(run(kb, "journalctl -u kibana -n 10 --no-pager | grep -iE 'fleet|deploy' || true", check=False))
    kb.close()

    fl = connect(NODES["fleet"][0])
    print(run(fl, "elastic-agent status 2>&1 | head -20", check=False))
    fl.close()


if __name__ == "__main__":
    main()