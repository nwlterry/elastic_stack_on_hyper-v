#!/usr/bin/env python3
"""Install Fleet Server on Kibana node (more swap, kibana stopped during enroll)."""
import os
import re
import sys
import time
from pathlib import Path

import paramiko
from scp import SCPClient

ROOT = Path(__file__).parent
REMOTE = "/opt/elastic-setup"
VERSION = "8.18.4"
POLICY = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
ES_FQDN = "ismelkesnode01.ocplab.net"
KB_IP = "10.44.40.41"
KB_FQDN = "ismelkkbnnode01.ocplab.net"

PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()).group(1)


def run(c, cmd, timeout=900, check=True):
    print(f"  $ {cmd[:95]}", flush=True)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-2500:], flush=True)
    if check and code != 0:
        raise RuntimeError(f"fail {code}")
    return text


def main():
    es = paramiko.SSHClient()
    es.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    es.connect("10.44.40.31", username="root", password=PWD, timeout=30)
    pwd_out = run(es, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
    elastic = re.search(r"New (?:password|value):\s*(\S+)", pwd_out).group(1)
    tok_out = run(
        es,
        f"/usr/share/elasticsearch/bin/elasticsearch-service-tokens create elastic/fleet-server fleet-srv-{int(time.time())}",
        timeout=120,
    )
    svc = re.search(r"(AAEAA[\w+/=-]+)", tok_out).group(1)
    ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt", timeout=60)
    auth = run(
        es,
        f"curl -sk -H 'Authorization: Bearer {svc}' https://localhost:9200/_security/_authenticate?pretty",
        check=False,
    )
    if "fleet-server" not in auth:
        print("WARNING: service token auth failed", flush=True)
    es.close()
    print(f"elastic={elastic}", flush=True)

    kb = paramiko.SSHClient()
    kb.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kb.connect(KB_IP, username="root", password=PWD, timeout=30)
    run(kb, f"mkdir -p {REMOTE}/rpms /etc/elasticsearch/certs", timeout=60)
    with SCPClient(kb.get_transport()) as scp:
        for f in (ROOT / "scripts").glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
        for f in (ROOT / "packages").iterdir():
            if f.is_file() and ("elastic-agent" in f.name or "GPG" in f.name):
                scp.put(str(f), f"{REMOTE}/rpms/{f.name}")
    run(kb, f"chmod +x {REMOTE}/*.sh", timeout=60)
    run(kb, f"bash {REMOTE}/prepare-fleet-memory.sh", check=False, timeout=300)
    run(kb, f"bash {REMOTE}/update-cluster-hosts.sh", timeout=60)
    run(kb, f"bash {REMOTE}/configure-firewall.sh fleet", check=False, timeout=60)
    run(kb, f"cat > /etc/elasticsearch/certs/http_ca.crt <<'EOF'\n{ca}\nEOF", timeout=60)
    run(kb, "systemctl stop kibana 2>/dev/null || true", check=False, timeout=60)
    run(
        kb,
        "pkill -9 -f elastic-agent 2>/dev/null; elastic-agent uninstall --force 2>/dev/null; "
        "rpm -e elastic-agent-8.18.4 2>/dev/null; rm -rf /var/lib/elastic-agent /etc/elastic-agent; true",
        check=False,
        timeout=300,
    )
    install = (
        f"nohup bash {REMOTE}/install-fleet-server.sh --version {VERSION} --es-host {ES_FQDN} "
        f"--service-token '{svc}' --policy-id '{POLICY}' > /var/log/fleet-install.log 2>&1 &"
    )
    run(kb, install, timeout=30)
    kb.close()

    for i in range(50):
        time.sleep(30)
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(KB_IP, username="root", password=PWD, timeout=30)
        _, o, e = c.exec_command(
            "ss -tlnp | grep 8220 || echo NO_8220; tail -8 /var/log/fleet-install.log; free -h | head -2",
            timeout=30,
        )
        o.channel.recv_exit_status()
        text = (o.read() + e.read()).decode()
        c.close()
        print(f"poll {i}: {text[-600:]}", flush=True)
        if ":8220" in text and "NO_8220" not in text.split("8220")[0][-20:]:
            print("FLEET UP on Kibana node", flush=True)
            break
        if "Fleet Server running" in text:
            break
        if "Error:" in text and "Waiting For Enroll" not in text:
            if i > 5:
                break

    # restart kibana
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(KB_IP, username="root", password=PWD, timeout=30)
    run(c, "systemctl start kibana", check=False, timeout=60)
    c.close()
    print(f"Fleet target: https://{KB_FQDN}:8220  elastic={elastic}", flush=True)


if __name__ == "__main__":
    main()