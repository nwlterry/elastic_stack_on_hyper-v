#!/usr/bin/env python3
"""Kill stuck Kibana-node enroll, create verified service token, redeploy Fleet."""
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

PWD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()
).group(1)


def run(c, cmd, timeout=900, check=True):
    print(f"  $ {cmd[:100]}", flush=True)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-2500:], flush=True)
    if check and code != 0:
        raise RuntimeError(f"fail({code})")
    return text


def main():
    es = paramiko.SSHClient()
    es.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    es.connect("10.44.40.31", username="root", password=PWD, timeout=30)

    pwd_out = run(es, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
    elastic = re.search(r"New (?:password|value):\s*(\S+)", pwd_out).group(1)

    token_name = f"fleet-api-{int(time.time())}"
    # CLI-created tokens fail auth on this cluster; use the Security API instead.
    tok_out = run(
        es,
        f"curl -sk -u elastic:{elastic} -X POST "
        f"'https://localhost:9200/_security/service/elastic/fleet-server/credential/token/{token_name}?pretty'",
    )
    m = re.search(r'"value"\s*:\s*"(AAEAA[^"]+)"', tok_out)
    if not m:
        raise RuntimeError("could not parse API service token")
    svc = m.group(1).strip()

    auth = run(es, f"curl -sk -H 'Authorization: Bearer {svc}' https://localhost:9200/_security/_authenticate?pretty", check=False)
    if '"username" : "elastic/fleet-server"' not in auth or '"authentication_type" : "token"' not in auth:
        print("TOKEN AUTH FAILED", flush=True)
        print(auth, flush=True)
        es.close()
        return 1
    print("TOKEN AUTH OK", flush=True)

    ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt")
    es.close()
    print(f"elastic={elastic}", flush=True)

    kb = paramiko.SSHClient()
    kb.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kb.connect(KB_IP, username="root", password=PWD, timeout=30)

    run(kb, "systemctl stop kibana 2>/dev/null || true", check=False)
    run(
        kb,
        "pkill -9 -f elastic-agent 2>/dev/null; pkill -9 -f install-fleet 2>/dev/null; "
        "elastic-agent uninstall --force 2>/dev/null; rpm -e elastic-agent-8.18.4 2>/dev/null; "
        "rm -rf /var/lib/elastic-agent /etc/elastic-agent; true",
        check=False,
        timeout=300,
    )
    time.sleep(5)

    run(kb, f"mkdir -p {REMOTE}/rpms /etc/elasticsearch/certs", check=False)
    with SCPClient(kb.get_transport()) as scp:
        for f in (ROOT / "scripts").glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
        pkg = ROOT / "packages"
        if pkg.is_dir():
            for f in pkg.iterdir():
                if f.is_file() and ("elastic-agent" in f.name or "GPG" in f.name):
                    scp.put(str(f), f"{REMOTE}/rpms/{f.name}")
    run(kb, f"chmod +x {REMOTE}/*.sh")
    run(kb, f"bash {REMOTE}/prepare-fleet-memory.sh", check=False)
    run(kb, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(kb, f"bash {REMOTE}/configure-firewall.sh fleet", check=False)
    run(kb, f"cat > /etc/elasticsearch/certs/http_ca.crt <<'EOF'\n{ca}\nEOF")

    run(
        kb,
        f"nohup bash {REMOTE}/install-fleet-server.sh --version {VERSION} --es-host {ES_FQDN} "
        f"--service-token '{svc}' --policy-id '{POLICY}' > /var/log/fleet-install.log 2>&1 &",
        timeout=30,
    )
    kb.close()

    print("Waiting for 8220 on Kibana node...", flush=True)
    for i in range(80):
        time.sleep(30)
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(KB_IP, username="root", password=PWD, timeout=30)
        text = run(
            c,
            "ss -tlnp | grep 8220 || echo NO_8220; systemctl is-active elastic-agent 2>&1; "
            "tail -3 /var/log/fleet-install.log; free -h | head -2",
            check=False,
            timeout=60,
        )
        c.close()
        print(f"poll {i}: {text[-400:]}", flush=True)
        if ":8220" in text and "NO_8220" not in text.split("8220")[0][-15:]:
            print(f"FLEET UP https://{KB_FQDN}:8220", flush=True)
            break
        if "Failed to Enroll" in text or ("Error:" in text and i > 10):
            print("Enroll failed — check /var/log/fleet-install.log", flush=True)
            return 1
    else:
        print("Timeout waiting for 8220", flush=True)
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(KB_IP, username="root", password=PWD, timeout=30)
        run(c, "systemctl start kibana", check=False)
        c.close()
        return 1

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(KB_IP, username="root", password=PWD, timeout=30)
    run(c, "systemctl start kibana", check=False)
    c.close()
    print(f"Done. Fleet=https://{KB_FQDN}:8220 Kibana=http://{KB_FQDN}:5601 elastic={elastic}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())