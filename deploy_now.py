#!/usr/bin/env python3
"""One-shot remote deploy for ELK 8.18.4. Password via SSH_PASS env var."""
import os
import re
import sys
import time
from pathlib import Path

import paramiko
from scp import SCPClient

PASSWORD = os.environ.get("SSH_PASS")
if not PASSWORD:
    sys.exit("SSH_PASS environment variable required")

SCRIPT_DIR = Path(__file__).parent / "scripts"
REMOTE_DIR = "/opt/elastic-setup"
VERSION = "8.18.4"
CLUSTER = "ism-elk-cluster"

ES_NODES = [
    ("10.44.40.31", "ismelkesnode01"),
    ("10.44.40.32", "ismelkesnode02"),
    ("10.44.40.33", "ismelkesnode03"),
]
KIBANA = ("10.44.40.41", "ismelkkbnnode01")


def connect(ip: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ip, username="root", password=PASSWORD, timeout=20)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 600) -> str:
    print(f"  $ {cmd[:120]}{'...' if len(cmd) > 120 else ''}")
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    code = stdout.channel.recv_exit_status()
    if out.strip():
        print(out.rstrip())
    if code != 0:
        raise RuntimeError(f"Command failed ({code}) on {cmd}\n{err or out}")
    return out


def copy_scripts(client: paramiko.SSHClient):
    run(client, f"mkdir -p {REMOTE_DIR}")
    with SCPClient(client.get_transport()) as scp:
        for f in SCRIPT_DIR.glob("*.sh"):
            scp.put(str(f), f"{REMOTE_DIR}/{f.name}")


def prep_es_node(ip: str, hostname: str):
    print(f"\n=== Preparing {hostname} ({ip}) ===")
    c = connect(ip)
    ver = run(c, f"rpm -q elasticsearch-{VERSION} 2>/dev/null && echo INSTALLED || echo MISSING").strip()
    if "INSTALLED" in ver:
        print(f"  {hostname} already has elasticsearch-{VERSION}, skipping package install")
        copy_scripts(c)
        run(c, f"chmod +x {REMOTE_DIR}/*.sh")
        c.close()
        return
    copy_scripts(c)
    run(c, f"chmod +x {REMOTE_DIR}/*.sh")
    run(c, "systemctl stop elasticsearch || true")
    run(c, "mkdir -p /data/elasticsearch.backup && "
           "if [ -d /data/elasticsearch ] && mountpoint -q /data/elasticsearch 2>/dev/null; then true; "
           "elif [ -d /data/elasticsearch ]; then "
           "shopt -s dotglob; mv /data/elasticsearch/* /data/elasticsearch.backup/ 2>/dev/null || true; fi")
    run(c, f"bash {REMOTE_DIR}/prepare-data-disk.sh")
    run(c, "dnf remove -y elasticsearch || true")
    run(c, f"bash {REMOTE_DIR}/install-elasticsearch.sh --version {VERSION} "
           f"--node {hostname} --cluster {CLUSTER}")
    c.close()


def fix_node01_security():
    ip = ES_NODES[0][0]
    c = connect(ip)
    run(c, "systemctl stop elasticsearch || true")
    run(c, "rm -rf /etc/elasticsearch/certs /etc/elasticsearch/elasticsearch.keystore")
    run(c, "/usr/share/elasticsearch/bin/elasticsearch-keystore create")
    c.close()


def bootstrap_node01() -> dict:
    print("\n=== Bootstrapping node01 ===")
    fix_node01_security()
    ip = ES_NODES[0][0]
    c = connect(ip)
    out = run(c, "NONINTERACTIVE=1 bash /opt/elastic-setup/bootstrap-cluster.sh")
    password = ""
    for line in out.splitlines():
        m = re.search(r"New password:\s*(\S+)", line)
        if m:
            password = m.group(1)
    kibana_token = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana").strip()
    c.close()
    return {"password": password, "kibana_token": kibana_token, "raw": out}


def new_node_token(ip: str) -> str:
    c = connect(ip)
    token = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
    c.close()
    return token


def enroll_node(ip: str, hostname: str, token: str):
    print(f"\n=== Enrolling {hostname} ({ip}) ===")
    c = connect(ip)
    run(c, f"NODE_ENROLLMENT_TOKEN='{token}' NONINTERACTIVE=1 bash {REMOTE_DIR}/bootstrap-cluster.sh")
    c.close()


def deploy_kibana(token: str):
    ip, hostname = KIBANA
    print(f"\n=== Kibana {hostname} ({ip}) ===")
    c = connect(ip)
    copy_scripts(c)
    run(c, f"chmod +x {REMOTE_DIR}/*.sh")
    run(c, "systemctl stop kibana || true")
    run(c, "dnf remove -y kibana || true")
    run(c, f"bash {REMOTE_DIR}/install-kibana.sh --version {VERSION} --es-host 10.44.40.31 "
           f"--enrollment-token '{token}'")
    c.close()


def verify():
    print("\n=== Verification ===")
    c = connect(ES_NODES[0][0])
    run(c, "rpm -q elasticsearch kibana 2>/dev/null; systemctl is-active elasticsearch")
    run(c, "df -h /data/elasticsearch; lsblk -f /dev/sdb")
    run(c, "curl -sk https://localhost:9200 2>&1 | head -1")
    c.close()
    c = connect(KIBANA[0])
    run(c, "rpm -q kibana; systemctl is-active kibana")
    c.close()


def main():
    for ip, host in ES_NODES:
        prep_es_node(ip, host)

    tokens = bootstrap_node01()
    print(f"\nCaptured bootstrap output:\n{tokens.get('raw', '')}")
    kibana_token = tokens["kibana_token"]
    elastic_pwd = tokens["password"]

    for ip, host in ES_NODES[1:]:
        node_token = new_node_token(ES_NODES[0][0])
        enroll_node(ip, host, node_token)

    deploy_kibana(kibana_token)
    verify()

    print("\n" + "=" * 60)
    print("DEPLOYMENT COMPLETE")
    print(f"  Elasticsearch: {VERSION}")
    print(f"  Cluster:       {CLUSTER}")
    print(f"  elastic password: {elastic_pwd or '(see node01 reset-password output above)'}")
    print(f"  Kibana URL:    https://10.44.40.41:5601")
    print("=" * 60)


if __name__ == "__main__":
    main()