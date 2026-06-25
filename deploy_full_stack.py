#!/usr/bin/env python3
"""
Full ELK stack deploy after RHEL flash install:
  Elasticsearch cluster → Kibana → Fleet Server → Elastic Agents + integrations
Password via SSH_PASS env var.
"""
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

PASSWORD = os.environ.get("SSH_PASS")
if not PASSWORD:
    sys.exit("Set SSH_PASS to root password")

NODES = {
    "es01": ("10.44.40.31", f"ismelkesnode01.{DOMAIN}"),
    "es02": ("10.44.40.32", f"ismelkesnode02.{DOMAIN}"),
    "es03": ("10.44.40.33", f"ismelkesnode03.{DOMAIN}"),
    "kibana": ("10.44.40.41", f"ismelkkbnnode01.{DOMAIN}"),
    "fleet": ("10.44.40.42", f"ismelkflnode01.{DOMAIN}"),
}

ES_NODES = [NODES["es01"], NODES["es02"], NODES["es03"]]


def connect(ip: str) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for attempt in range(60):
        try:
            c.connect(ip, username="root", password=PASSWORD, timeout=15)
            return c
        except Exception:
            time.sleep(20)
    raise RuntimeError(f"SSH failed: {ip}")


def run(c, cmd, check=True, timeout=900) -> str:
    print(f"  [{cmd[:90]}...]" if len(cmd) > 90 else f"  $ {cmd}")
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    if out.strip():
        print(out[-3000:])
    if check and code != 0:
        raise RuntimeError(f"FAIL({code}): {err or out}")
    return out


def copy_scripts(c):
    run(c, f"mkdir -p {REMOTE}/rpms", check=False)
    with SCPClient(c.get_transport()) as scp:
        for f in SCRIPTS.glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
        pkg_dir = ROOT / "packages"
        if pkg_dir.is_dir():
            for f in pkg_dir.iterdir():
                if f.is_file():
                    scp.put(str(f), f"{REMOTE}/rpms/{f.name}")
    run(c, f"chmod +x {REMOTE}/*.sh")


def attach_data_disks():
    """Run Attach-DataDisks.ps1 on Hyper-V host after OS is up."""
    import subprocess
    ps1 = ROOT / "Attach-DataDisks.ps1"
    print("=== Attaching Hyper-V data disks ===")
    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"Attach-DataDisks.ps1 failed (exit {result.returncode})")
    time.sleep(10)


def wait_flash_install():
    print("=== Waiting for RHEL flash install on all nodes ===")
    for name, (ip, fqdn) in NODES.items():
        print(f"  {name} ({ip})...")
        c = connect(ip)
        for _ in range(90):
            out = run(c, "test -f /root/.flash-install-complete && echo OK || echo WAIT", check=False)
            if "OK" in out:
                print(f"    {fqdn} ready")
                break
            time.sleep(20)
        else:
            c.close()
            raise RuntimeError(f"Flash install timeout: {fqdn}")
        c.close()


def prep_es_node(ip: str, fqdn: str):
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/prepare-data-disk.sh")
    run(c, f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} --node {fqdn} --cluster {CLUSTER}")
    c.close()


def wait_es_api(c) -> None:
    for _ in range(60):
        out = run(
            c,
            "curl -sk --connect-timeout 2 https://localhost:9200 2>&1 | head -c 200",
            check=False,
        )
        if "security_exception" in out or "tagline" in out:
            time.sleep(20)
            return
        time.sleep(5)
    raise RuntimeError("Elasticsearch API not ready")


def reset_elastic_password(c) -> str:
    for attempt in range(20):
        out = run(
            c,
            "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1",
            check=False,
            timeout=120,
        )
        m = re.search(r"New (?:password|value):\s*(\S+)", out)
        if m:
            return m.group(1)
        print(f"  reset-password attempt {attempt + 1} waiting...")
        time.sleep(15)
    raise RuntimeError("elasticsearch-reset-password failed after retries")


def bootstrap_cluster() -> str:
    print("=== Bootstrap Elasticsearch cluster ===")
    ip, fqdn = NODES["es01"]
    c = connect(ip)
    copy_scripts(c)
    run(c, "chown -R root:elasticsearch /etc/elasticsearch; chmod 2770 /etc/elasticsearch")
    run(c, f"NODE_IP=10.44.40.31 bash {REMOTE}/fix-es-bootstrap.sh", timeout=300)
    wait_es_api(c)
    elastic_pwd = reset_elastic_password(c)
    print(f"  elastic password set")
    node2_t = run(
        c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node"
    ).strip()
    node3_t = run(
        c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node"
    ).strip()
    c.close()

    for ip, fqdn, token in [
        (NODES["es02"][0], NODES["es02"][1], node2_t),
        (NODES["es03"][0], NODES["es03"][1], node3_t),
    ]:
        c = connect(ip)
        copy_scripts(c)
        run(c, "chown -R root:elasticsearch /etc/elasticsearch; chmod 2770 /etc/elasticsearch")
        run(
            c,
            f"NODE_ENROLLMENT_TOKEN='{token}' CLUSTER_NAME={CLUSTER} "
            f"bash {REMOTE}/bootstrap-cluster.sh",
            timeout=600,
        )
        run(c, "chown elasticsearch:elasticsearch /etc/elasticsearch/elasticsearch.keystore 2>/dev/null || true")
        run(c, "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch")
        c.close()

    time.sleep(15)
    c = connect(NODES["es01"][0])
    print(run(c, f"curl -sk -u elastic:{elastic_pwd} https://localhost:9200/_cluster/health?pretty"))
    c.close()
    return elastic_pwd


def deploy_kibana(elastic_pwd: str) -> str:
    print("=== Kibana ===")
    ip, _ = NODES["es01"]
    c = connect(ip)
    kibana_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana").strip()
    c.close()

    ip, fqdn = NODES["kibana"]
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/install-kibana.sh --version {VERSION} --es-host 10.44.40.31 --enrollment-token '{kibana_t}'")
    c.close()

    # Wait for Kibana
    c = connect(ip)
    for _ in range(40):
        if "200" in run(c, "curl -sk -o /dev/null -w '%{http_code}' https://localhost:5601", check=False):
            break
        time.sleep(10)
    c.close()
    return elastic_pwd


def setup_fleet(elastic_pwd: str) -> dict:
    print("=== Fleet policies (Kibana API) ===")
    ip, _ = NODES["kibana"]
    c = connect(ip)
    copy_scripts(c)
    out = run(
        c,
        f"ELASTIC_PASS='{elastic_pwd}' bash {REMOTE}/setup-fleet-kibana.sh",
    )
    c.close()
    result = {}
    for line in out.splitlines():
        if "=" in line and line.startswith(("FLEET_", "ES_", "KIBANA_")):
            k, v = line.split("=", 1)
            result[k] = v
    return result


def deploy_fleet_server(elastic_pwd: str, fleet_policy_id: str):
    print("=== Fleet Server ===")
    es_ip = NODES["es01"][0]
    c = connect(es_ip)
    token_name = f"fleet-srv-{int(time.time())}"
    token_out = run(
        c,
        f"/usr/share/elasticsearch/bin/elasticsearch-service-tokens create elastic/fleet-server {token_name}",
    )
    m = re.search(r"(AAEAA[\w+/=-]+)", token_out)
    svc_token = m.group(1) if m else ""
    if not svc_token:
        raise RuntimeError(f"Could not parse service token: {token_out[:200]}")
    ca = run(c, "cat /etc/elasticsearch/certs/http_ca.crt")
    c.close()

    ip, fqdn = NODES["fleet"]
    c = connect(ip)
    copy_scripts(c)
    run(c, "mkdir -p /etc/elasticsearch/certs")
    run(c, f"cat > /etc/elasticsearch/certs/http_ca.crt <<'EOF'\n{ca}\nEOF")
    run(
        c,
        f"bash {REMOTE}/install-fleet-server.sh --version {VERSION} --es-host {es_ip} "
        f"--service-token '{svc_token}' --policy-id '{fleet_policy_id}'",
    )
    c.close()


def deploy_agents(fleet_info: dict):
    print("=== Elastic Agents on ES + Kibana nodes ===")
    es_tok = fleet_info.get("ES_ENROLLMENT_TOKEN", "")
    kb_tok = fleet_info.get("KIBANA_ENROLLMENT_TOKEN", "")

    for ip, fqdn in ES_NODES:
        c = connect(ip)
        copy_scripts(c)
        run(c, f"bash {REMOTE}/install-elastic-agent.sh --version {VERSION} --enrollment-token '{es_tok}'")
        c.close()
        print(f"  Agent on {fqdn}")

    ip, fqdn = NODES["kibana"]
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/install-elastic-agent.sh --version {VERSION} --enrollment-token '{kb_tok}'")
    c.close()
    print(f"  Agent on {fqdn}")


def update_hosts():
    import subprocess
    print("=== Local + VM host records ===")
    subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "Add-LocalHosts.ps1")],
        check=True,
    )
    for name, (ip, fqdn) in NODES.items():
        c = connect(ip)
        copy_scripts(c)
        run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
        c.close()
        print(f"  hosts updated on {fqdn}")


def main():
    wait_flash_install()
    update_hosts()
    attach_data_disks()

    for ip, fqdn in ES_NODES:
        print(f"=== Prep {fqdn} ===")
        prep_es_node(ip, fqdn)

    elastic_pwd = bootstrap_cluster()
    deploy_kibana(elastic_pwd)

    fleet_info = setup_fleet(elastic_pwd)
    fleet_policy = fleet_info.get("FLEET_POLICY_ID", "")
    if fleet_policy:
        deploy_fleet_server(elastic_pwd, fleet_policy)
        time.sleep(30)
        deploy_agents(fleet_info)

    print("\n" + "=" * 60)
    print("FULL STACK DEPLOYED")
    print(f"  Elasticsearch: https://10.44.40.31:9200  ({VERSION})")
    print(f"  Kibana:        https://10.44.40.41:5601")
    print(f"  Fleet Server:  https://10.44.40.42:8220")
    print(f"  elastic user:  {elastic_pwd}")
    print("=" * 60)


if __name__ == "__main__":
    main()