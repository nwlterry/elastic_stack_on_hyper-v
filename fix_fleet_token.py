#!/usr/bin/env python3
"""Create fresh service token, verify auth, install fleet server."""
import re, time, paramiko
from pathlib import Path
from scp import SCPClient

ROOT = Path(__file__).parent
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()).group(1)
REMOTE = "/opt/elastic-setup"
VERSION = "8.18.4"
POLICY = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
ES_FQDN = "ismelkesnode01.ocplab.net"

def run(c, cmd, timeout=120, check=True):
    print(f"$ {cmd[:90]}", flush=True)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-2000:], flush=True)
    if check and code != 0:
        raise RuntimeError(f"exit {code}")
    return text

# cleanup old tokens on ES
es = paramiko.SSHClient()
es.set_missing_host_key_policy(paramiko.AutoAddPolicy())
es.connect("10.44.40.31", username="root", password=PWD, timeout=30)

# delete old fleet-srv tokens (keep one)
list_out = run(es, "/usr/share/elasticsearch/bin/elasticsearch-service-tokens list elastic/fleet-server 2>&1", check=False)
for line in list_out.splitlines():
    if "fleet-srv" in line:
        name = line.strip().split("/")[-1]
        run(es, f"/usr/share/elasticsearch/bin/elasticsearch-service-tokens delete elastic/fleet-server/{name} 2>&1", check=False)

token_name = f"fleet-srv-{int(time.time())}"
tok_out = run(es, f"/usr/share/elasticsearch/bin/elasticsearch-service-tokens create elastic/fleet-server {token_name}")
# parse full token line: SERVICE_TOKEN elastic/fleet-server/name = VALUE
m = re.search(r"=\s*(AAEAA\S+)", tok_out)
if not m:
    m = re.search(r"(AAEAA[\w+/=-]+)", tok_out)
svc_token = m.group(1).strip()
print(f"TOKEN={svc_token[:40]}...", flush=True)

auth = run(es, f"curl -sk -H 'Authorization: Bearer {svc_token}' https://localhost:9200/_security/_authenticate?pretty", check=False)
if "fleet-server" not in auth and "username" not in auth:
    print("TOKEN AUTH FAILED - aborting", flush=True)
    es.close()
    raise SystemExit(1)
print("TOKEN AUTH OK", flush=True)

ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt")
pwd_out = run(es, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1")
elastic = re.search(r"New (?:password|value):\s*(\S+)", pwd_out).group(1)
es.close()

# fleet node
fl = paramiko.SSHClient()
fl.set_missing_host_key_policy(paramiko.AutoAddPolicy())
fl.connect("10.44.40.42", username="root", password=PWD, timeout=30)
run(fl, "pkill -9 -f elastic-agent 2>/dev/null || true; pkill -9 -f install-fleet 2>/dev/null || true", check=False)
time.sleep(3)

with SCPClient(fl.get_transport()) as scp:
    for f in (ROOT / "scripts").glob("*.sh"):
        scp.put(str(f), f"{REMOTE}/{f.name}")
run(fl, f"chmod +x {REMOTE}/*.sh", check=False)
run(fl, f"cat > /etc/elasticsearch/certs/http_ca.crt <<'EOF'\n{ca}\nEOF", check=False)

run(
    fl,
    "elastic-agent uninstall --force 2>/dev/null || true; rpm -e elastic-agent-8.18.4 2>/dev/null || true; "
    "rm -rf /var/lib/elastic-agent /etc/elastic-agent; true",
    check=False,
    timeout=300,
)

install = (
    f"nohup bash {REMOTE}/install-fleet-server.sh --version {VERSION} --es-host {ES_FQDN} "
    f"--service-token '{svc_token}' --policy-id '{POLICY}' > /var/log/fleet-install.log 2>&1 &"
)
run(fl, install, timeout=30)
fl.close()
print("install started with verified token", flush=True)

for i in range(40):
    time.sleep(30)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
    _, o, e = c.exec_command(
        "ss -tlnp | grep 8220 || echo NO_8220; tail -6 /var/log/fleet-install.log; "
        "curl -sk https://localhost:8220/api/status 2>/dev/null | head -c 100",
        timeout=30,
    )
    o.channel.recv_exit_status()
    text = (o.read() + e.read()).decode()
    c.close()
    print(f"poll {i}: {text[-800:]}", flush=True)
    if ":8220" in text and "NO_8220" not in text:
        print(f"DONE elastic={elastic}", flush=True)
        break

print(f"elastic password: {elastic}", flush=True)