#!/usr/bin/env python3
"""Single-session fleet install: swap + install script on fleet node."""
import re, time, paramiko
from pathlib import Path
from scp import SCPClient

ROOT = Path(__file__).parent
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()).group(1)
REMOTE = "/opt/elastic-setup"
VERSION = "8.18.4"
POLICY = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
ES_FQDN = "ismelkesnode01.ocplab.net"

def run(c, cmd, timeout=3600, check=True):
    print(f"$ {cmd[:100]}", flush=True)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-3000:], flush=True)
    if check and code != 0:
        raise RuntimeError(f"exit {code}: {err or out}")
    return text

# ES: token + CA
es = paramiko.SSHClient()
es.set_missing_host_key_policy(paramiko.AutoAddPolicy())
es.connect("10.44.40.31", username="root", password=PWD, timeout=30)
out = run(es, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
elastic = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
print(f"elastic={elastic}", flush=True)
tok_out = run(es, f"/usr/share/elasticsearch/bin/elasticsearch-service-tokens create elastic/fleet-server fleet-srv-{int(time.time())}", timeout=120)
svc = re.search(r"(AAEAA[\w+/=-]+)", tok_out).group(1)
ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt", timeout=60)
es.close()

# Fleet
fl = paramiko.SSHClient()
fl.set_missing_host_key_policy(paramiko.AutoAddPolicy())
fl.connect("10.44.40.42", username="root", password=PWD, timeout=30)
run(fl, f"mkdir -p {REMOTE}/rpms", timeout=60)
with SCPClient(fl.get_transport()) as scp:
    for f in (ROOT / "scripts").glob("*.sh"):
        scp.put(str(f), f"{REMOTE}/{f.name}")
    for f in (ROOT / "packages").iterdir():
        if f.is_file() and ("elastic-agent" in f.name or "GPG" in f.name):
            scp.put(str(f), f"{REMOTE}/rpms/{f.name}")
run(fl, f"chmod +x {REMOTE}/*.sh", timeout=60)
run(fl, f"bash {REMOTE}/update-cluster-hosts.sh", timeout=60)
run(fl, f"bash {REMOTE}/configure-firewall.sh fleet", timeout=60)
run(fl, f"mkdir -p {REMOTE}/certs /etc/elasticsearch/certs /etc/elastic-agent/certs", timeout=60)
for path in (
    f"{REMOTE}/certs/http_ca.crt",
    "/etc/elasticsearch/certs/http_ca.crt",
    "/etc/elastic-agent/certs/http_ca.crt",
):
    run(fl, f"cat > {path} <<'EOF'\n{ca}\nEOF", timeout=60)
    run(fl, f"chmod 644 {path}", timeout=60)

run(
    fl,
    "pkill -9 -f elastic-agent 2>/dev/null || true; systemctl stop elastic-agent 2>/dev/null || true; "
    "elastic-agent uninstall --force 2>/dev/null || true; rpm -e elastic-agent-8.18.4 2>/dev/null || true; "
    "rm -rf /opt/Elastic /var/lib/elastic-agent /etc/elastic-agent; true",
    timeout=600,
    check=False,
)
# run install in background
install = (
    f"nohup bash {REMOTE}/install-fleet-server.sh --version {VERSION} --es-host {ES_FQDN} "
    f"--ca-file {REMOTE}/certs/http_ca.crt "
    f"--service-token '{svc}' --policy-id '{POLICY}' > /var/log/fleet-install.log 2>&1 &"
)
run(fl, install, timeout=30)
fl.close()
print("install launched", flush=True)

for i in range(50):
    time.sleep(30)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
    _, o, e = c.exec_command(
        "free -h | head -2; swapon --show; ss -tlnp | grep 8220 || echo NO_8220; "
        "tail -8 /var/log/fleet-install.log; systemctl is-active elastic-agent",
        timeout=30,
    )
    o.channel.recv_exit_status()
    text = (o.read() + e.read()).decode()
    c.close()
    print(f"\n=== poll {i} ===\n{text}", flush=True)
    if "LISTEN" in text and "8220" in text:
        print("FLEET READY", flush=True)
        break
    if "Fleet Server running" in text:
        print("FLEET READY", flush=True)
        break
    if "Error:" in text and "Waiting For Enroll" not in text.split("Error:")[-1][:200]:
        pass

print(f"elastic password: {elastic}", flush=True)