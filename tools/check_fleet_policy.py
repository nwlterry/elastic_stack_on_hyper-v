#!/usr/bin/env python3
import json
import re
import ssl
import base64
import urllib.request
from pathlib import Path

PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def api(path):
    req = urllib.request.Request(
        "http://10.44.40.41:5601" + path,
        headers={
            "kbn-xsrf": "true",
            "Authorization": "Basic " + base64.b64encode(f"elastic:{PWD}".encode()).decode(),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

policies = api("/api/fleet/agent_policies")
for p in policies.get("items", []):
    if "Fleet" in p.get("name", ""):
        print("POLICY", p["id"], p["name"], "has_fleet_server=", p.get("is_managed") or p.get("has_fleet_server"))
        pkgs = api(f"/api/fleet/package_policies?perPage=200")
        for pp in pkgs.get("items", []):
            if pp.get("policy_id") == p["id"]:
                print("  PKG", pp.get("package", {}).get("name"), pp.get("name"), pp.get("id"))