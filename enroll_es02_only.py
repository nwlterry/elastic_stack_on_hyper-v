#!/usr/bin/env python3
import os
import re
import sys
import time
from pathlib import Path

import paramiko
from scp import SCPClient

ROOT = Path(__file__).parent
SCRIPTS = ROOT / "scripts"
REMOTE = "/opt/elastic-setup"
VERSION = "8.18.4"
CLUSTER = "ism-elk-cluster"
DOMAIN = "ocplab.net"
_cfg = (ROOT / "config.psd1").read_text()
PASSWORD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", _cfg
).group(1)

ES01 = ("10.44.40.31", f"ismelkesnode01.{DOMAIN}")
ES02 = ("10.44.40.32", f"ismelkesnode02.{DOMAIN}")


def connect(ip: str, attempts=40) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for _ in range(attempts):
        try:
            c.connect(ip, username="root", password=PASSWORD, timeout=20)
            return c
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"SSH failed: {ip}")


def run(c, cmd, check=True, timeout=900) -> str:
    print(f"$ {cmd[:120]}..." if len(cmd) > 120 else f"$ {cmd}", flush=True)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-3000:], flush=True)
    if check and code != 0:
        raise RuntimeError(f"FAIL({code}): {err or out}")
    return text


def copy_scripts(c):
    run(c, f"mkdir -p {REMOTE}/rpms", check=False)
    with SCPClient(c.get_transport()) as scp:
        for f in SCRIPTS.glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
    run(c, f"chmod +x {REMOTE}/*.sh")


def wait_es_api(c):
    for _ in range(60):
        out = run(c, "curl -sk --connect-timeout 2 https://localhost:9200 2>&1 | head -c 200", check=False)
        if "security_exception" in out or "tagline" in out:
            time.sleep(10)
            return
        time.sleep(5)
    raise RuntimeError("ES API not ready")


def main():
    nip, nfqdn = ES02
    ip01, _ = ES01

    print("=== Prepare ES02 ===", flush=True)
    c = connect(nip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh elasticsearch", check=False)
    installed = run(c, f"rpm -q elasticsearch-{VERSION} 2>/dev/null && echo YES || echo NO", check=False)
    mounted = run(c, "mountpoint -q /data/elasticsearch && echo MOUNTED || echo EMPTY", check=False)
    if "MOUNTED" not in mounted:
        run(c, f"bash {REMOTE}/prepare-data-disk.sh")
    if "YES" not in installed:
        run(
            c,
            f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} "
            f"--node {nfqdn} --cluster {CLUSTER}",
        )
    run(c, "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch 2>/dev/null || true")
    c.close()

    print("=== Get enrollment token from ES01 ===", flush=True)
    c = connect(ip01)
    out = run(c, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", check=False, timeout=120)
    m = re.search(r"New (?:password|value):\s*(\S+)", out)
    pwd = m.group(1) if m else None
    if not pwd:
        raise RuntimeError("no elastic password")
    print(f"elastic={pwd}", flush=True)

    health = run(c, f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cat/nodes?v&h=name'", check=False)
    if nfqdn in health:
        print(f"{nfqdn} already in cluster", flush=True)
        c.close()
        return

    tok = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
    c.close()

    print("=== Enroll ES02 ===", flush=True)
    c = connect(nip)
    copy_scripts(c)
    run(c, "systemctl stop elasticsearch 2>/dev/null || true")
    run(c, "chown -R root:elasticsearch /etc/elasticsearch; chmod 2770 /etc/elasticsearch")
    enroll_out = run(
        c,
        f"/usr/share/elasticsearch/bin/elasticsearch-reconfigure-node "
        f"--enrollment-token '{tok}' <<< 'y'",
        check=False,
    )
    if "ERROR" in enroll_out or "Aborting" in enroll_out:
        print("reconfigure failed, reinstalling", flush=True)
        run(c, f"dnf remove -y elasticsearch-{VERSION} 2>/dev/null || true", check=False)
        run(
            c,
            f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} "
            f"--node {nfqdn} --cluster {CLUSTER}",
            timeout=900,
        )
        run(c, "systemctl stop elasticsearch 2>/dev/null || true")
        run(
            c,
            f"/usr/share/elasticsearch/bin/elasticsearch-reconfigure-node "
            f"--enrollment-token '{tok}' <<< 'y'",
        )
    run(c, "chown elasticsearch:elasticsearch /etc/elasticsearch/elasticsearch.keystore 2>/dev/null || true")
    run(c, "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch")
    run(c, "systemctl enable elasticsearch")
    run(c, "systemctl start elasticsearch")
    wait_es_api(c)
    c.close()

    time.sleep(20)
    c = connect(ip01)
    print(run(c, f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cluster/health?pretty'"))
    print(run(c, f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cat/nodes?v'"))
    c.close()
    print("ES02 enrollment done", flush=True)


if __name__ == "__main__":
    main()