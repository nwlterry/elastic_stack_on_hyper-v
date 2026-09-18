#!/usr/bin/env python3
import os
import re
import paramiko

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open("config.psd1").read()
).group(1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.31", username="root", password=PWD, timeout=30)
out = c.exec_command("/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)[1].read().decode()
pwd = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
c.close()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.41", username="root", password=PWD, timeout=30)
script = r'''
import json, os, urllib.request, base64, ssl
pwd = os.environ["PWD"]
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
def api(method, path, body=None):
    req = urllib.request.Request("http://10.44.40.41:5601"+path,
        data=json.dumps(body).encode() if body else None, method=method,
        headers={"kbn-xsrf":"true","Content-Type":"application/json",
                 "Authorization":"Basic "+base64.b64encode(("elastic:"+pwd).encode()).decode()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

policies = api("GET", "/api/fleet/agent_policies")["items"]
es = [p for p in policies if p["name"]=="Elastic-Agents-ES"][0]
ver = api("GET", "/api/fleet/epm/packages/system")["items"][0]["version"]
print("system version", ver)
body = {
    "name": "es-system-test",
    "policy_id": es["id"],
    "enabled": True,
    "package": {"name": "system", "version": ver},
    "inputs": {
        "system-metrics": {"enabled": True, "streams": {"system.cpu": {"enabled": True}}},
        "system-logs": {"enabled": True, "streams": {"system.syslog": {"enabled": True, "vars": {"paths": ["/var/log/messages"]}}}},
    },
}
try:
    r = api("POST", "/api/fleet/package_policies", body)
    print("OK", r.get("item",{}).get("id"))
except Exception as e:
    if hasattr(e, "read"):
        print("ERR", e.read().decode()[:2000])
    else:
        print("ERR", e)
'''
_, o, e = c.exec_command(f"PWD='{pwd}' python3 -c {repr(script)}", timeout=60)
print((o.read()+e.read()).decode())
c.close()