#!/usr/bin/env python3
"""Update hosts on all nodes, then complete ES cluster + Kibana + Fleet."""
import os
import re
import sys
import time
import subprocess
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
os.environ["SSH_PASS"] = PASSWORD

NODES = {
    "es01": ("10.44.40.31", f"ismelkesnode01.{DOMAIN}"),
    "es02": ("10.44.40.32", f"ismelkesnode02.{DOMAIN}"),
    "es03": ("10.44.40.33", f"ismelkesnode03.{DOMAIN}"),
    "kibana": ("10.44.40.41", f"ismelkkbnnode01.{DOMAIN}"),
    "fleet": ("10.44.40.42", f"ismelkflnode01.{DOMAIN}"),
}
ES_NODES = [NODES["es01"], NODES["es02"], NODES["es03"]]
ALL_IPS = [ip for ip, _ in NODES.values()]


def connect(ip: str) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for attempt in range(30):
        try:
            c.connect(ip, username="root", password=PASSWORD, timeout=30)
            return c
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"SSH failed: {ip}")


def run(c, cmd, check=True, timeout=900) -> str:
    print(f"  $ {cmd[:100]}..." if len(cmd) > 100 else f"  $ {cmd}")
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-3500:])
    if check and code != 0:
        raise RuntimeError(f"FAIL({code}): {err or out}")
    return text


def copy_scripts(c):
    run(c, f"mkdir -p {REMOTE}/rpms", check=False)
    with SCPClient(c.get_transport()) as scp:
        for f in SCRIPTS.glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
        pkg = ROOT / "packages"
        if pkg.is_dir():
            for f in pkg.iterdir():
                if f.is_file():
                    scp.put(str(f), f"{REMOTE}/rpms/{f.name}")
    run(c, f"chmod +x {REMOTE}/*.sh")


def update_local_hosts():
    ps1 = ROOT / "Add-LocalHosts.ps1"
    print("=== Windows hosts file ===")
    subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        check=True,
    )


def update_vm_hosts():
    print("=== /etc/hosts on all VMs ===")
    for ip, fqdn in NODES.values():
        c = connect(ip)
        copy_scripts(c)
        run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
        c.close()
        print(f"  {fqdn} ok")


def attach_data_disks():
    ps1 = ROOT / "Attach-DataDisks.ps1"
    print("=== Attach data disks ===")
    subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        check=True,
    )
    time.sleep(10)


def ensure_es_node(ip: str, fqdn: str):
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh elasticsearch", check=False)
    installed = run(c, f"rpm -q elasticsearch-{VERSION} 2>/dev/null && echo YES || echo NO", check=False)
    mounted = run(c, "mountpoint -q /data/elasticsearch && echo MOUNTED || echo EMPTY", check=False)
    if "MOUNTED" not in mounted:
        run(c, f"bash {REMOTE}/prepare-data-disk.sh")
    if "YES" not in installed:
        run(c, f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} --node {fqdn} --cluster {CLUSTER}")
    run(c, "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch 2>/dev/null || true")
    c.close()


def wait_es_api(c):
    for _ in range(60):
        out = run(c, "curl -sk --connect-timeout 2 https://localhost:9200 2>&1 | head -c 200", check=False)
        if "security_exception" in out or "tagline" in out:
            time.sleep(10)
            return
        time.sleep(5)
    raise RuntimeError("ES API not ready")


def get_or_reset_password(c) -> str:
    for attempt in range(20):
        out = run(c, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", check=False, timeout=120)
        m = re.search(r"New (?:password|value):\s*(\S+)", out)
        if m:
            return m.group(1)
        time.sleep(15)
    raise RuntimeError("Could not obtain elastic password")


def bootstrap_cluster() -> str:
    print("=== Bootstrap / enroll ES cluster ===")
    ip, _ = NODES["es01"]
    c = connect(ip)
    copy_scripts(c)
    active = run(c, "systemctl is-active elasticsearch 2>&1 || true", check=False)
    if "active" not in active:
        run(c, f"NODE_IP=10.44.40.31 bash {REMOTE}/fix-es-bootstrap.sh", timeout=300)
        wait_es_api(c)
    elastic_pwd = get_or_reset_password(c)

    health = run(c, f"curl -sk -u elastic:{elastic_pwd} 'https://localhost:9200/_cluster/health?pretty'", check=False)
    nodes = 1
    m = re.search(r'"number_of_nodes"\s*:\s*(\d+)', health)
    if m:
        nodes = int(m.group(1))

    if nodes < 3:
        t2 = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
        t3 = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
        c.close()
        for (nip, nfqdn), tok in zip([NODES["es02"], NODES["es03"]], [t2, t3]):
            ensure_es_node(nip, nfqdn)
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
                print(f"  reconfigure failed on {nfqdn}, reinstalling elasticsearch")
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
            print(f"  enrolled {nfqdn}")
        time.sleep(20)
        c = connect(ip)
        print(run(c, f"curl -sk -u elastic:{elastic_pwd} 'https://localhost:9200/_cluster/health?pretty'"))
        c.close()
    else:
        c.close()
        print(f"  cluster already has {nodes} nodes")
    return elastic_pwd


def deploy_kibana(elastic_pwd: str):
    print("=== Kibana ===")
    c = connect(NODES["es01"][0])
    kibana_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana").strip()
    c.close()

    ip, fqdn = NODES["kibana"]
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh kibana", check=False)
    if "YES" not in run(c, f"rpm -q kibana-{VERSION} 2>/dev/null && echo YES || echo NO", check=False):
        run(c, f"bash {REMOTE}/install-kibana.sh --version {VERSION} --es-host 10.44.40.31 --enrollment-token '{kibana_t}'")
    else:
        run(c, "systemctl enable kibana; systemctl restart kibana")
    run(c, f"bash {REMOTE}/configure-kibana-security.sh", check=False, timeout=120)
    run(
        c,
        f"ELASTIC_PASS='{elastic_pwd}' ES_HOST=ismelkesnode01.{DOMAIN} "
        f"bash {REMOTE}/enable-stack-monitoring.sh",
        check=False,
        timeout=120,
    )
    run(c, f"bash {REMOTE}/fix-kibana-access.sh", check=False, timeout=300)
    c.close()

    c = connect(ip)
    for _ in range(40):
        code = run(
            c,
            "curl -s -o /dev/null -w '%{http_code}' http://localhost:5601 2>/dev/null || "
            "curl -sk -o /dev/null -w '%{http_code}' https://localhost:5601 2>/dev/null",
            check=False,
        ).strip()
        if code and code[-3:] in ("200", "302", "401"):
            print(f"  Kibana ready at http://{ip}:5601")
            break
        time.sleep(10)
    c.close()


def setup_fleet(elastic_pwd: str) -> dict:
    print("=== Fleet policies ===")
    c = connect(NODES["kibana"][0])
    copy_scripts(c)
    out = run(c, f"ELASTIC_PASS='{elastic_pwd}' bash {REMOTE}/setup-fleet-kibana.sh")
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
    token_name = f"fleet-api-{int(time.time())}"
    token_out = run(
        c,
        f"curl -sk -u elastic:{elastic_pwd} -X POST "
        f"'https://localhost:9200/_security/service/elastic/fleet-server/credential/token/{token_name}?pretty'",
    )
    m = re.search(r'"value"\s*:\s*"(AAEAA[^"]+)"', token_out)
    if not m:
        raise RuntimeError(f"Could not parse service token: {token_out[:200]}")
    svc_token = m.group(1)
    ca = run(c, "cat /etc/elasticsearch/certs/http_ca.crt")
    c.close()

    ip, fqdn = NODES["fleet"]
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh fleet", check=False)
    run(c, f"mkdir -p {REMOTE}/certs /etc/elasticsearch/certs /etc/elastic-agent/certs")
    for path in (
        f"{REMOTE}/certs/http_ca.crt",
        "/etc/elasticsearch/certs/http_ca.crt",
        "/etc/elastic-agent/certs/http_ca.crt",
    ):
        run(c, f"cat > {path} <<'EOF'\n{ca}\nEOF")
        run(c, f"chmod 644 {path}")
    es_fqdn = NODES["es01"][1]
    run(
        c,
        f"bash {REMOTE}/install-fleet-server.sh --version {VERSION} --es-host {es_fqdn} "
        f"--ca-file {REMOTE}/certs/http_ca.crt "
        f"--service-token '{svc_token}' --policy-id '{fleet_policy_id}'",
        timeout=3600,
    )
    c.close()


def deploy_agents(fleet_info: dict, ca: str):
    print("=== Elastic Agents ===")
    es_tok = fleet_info.get("ES_ENROLLMENT_TOKEN", "")
    kb_tok = fleet_info.get("KIBANA_ENROLLMENT_TOKEN", "")
    fleet_url = f"https://{NODES['fleet'][1]}:8220"
    es_fqdn = NODES["es01"][1]
    ca_arg = f"--ca-file {REMOTE}/certs/http_ca.crt"
    for ip, fqdn in ES_NODES + [NODES["kibana"]]:
        tok = es_tok if "esnode" in fqdn or "kesnode" in fqdn else kb_tok
        c = connect(ip)
        copy_scripts(c)
        run(c, f"mkdir -p {REMOTE}/certs /etc/elasticsearch/certs /etc/elastic-agent/certs")
        for path in (
            f"{REMOTE}/certs/http_ca.crt",
            "/etc/elasticsearch/certs/http_ca.crt",
            "/etc/elastic-agent/certs/http_ca.crt",
        ):
            run(c, f"cat > {path} <<'EOF'\n{ca}\nEOF")
            run(c, f"chmod 644 {path}")
        run(
            c,
            f"bash {REMOTE}/install-elastic-agent.sh --version {VERSION} "
            f"--fleet-url '{fleet_url}' --enrollment-token '{tok}' "
            f"--es-host {es_fqdn} {ca_arg}",
        )
        c.close()
        print(f"  agent on {fqdn}")


def main():
    update_local_hosts()
    update_vm_hosts()
    attach_data_disks()
    for ip, fqdn in ES_NODES:
        ensure_es_node(ip, fqdn)
    elastic_pwd = bootstrap_cluster()
    deploy_kibana(elastic_pwd)
    fleet_info = setup_fleet(elastic_pwd)
    if fleet_info.get("FLEET_POLICY_ID"):
        c = connect(NODES["es01"][0])
        ca = run(c, "cat /etc/elasticsearch/certs/http_ca.crt")
        c.close()
        deploy_fleet_server(elastic_pwd, fleet_info["FLEET_POLICY_ID"])
        time.sleep(30)
        deploy_agents(fleet_info, ca)
    print("\n" + "=" * 60)
    print("STACK COMPLETE")
    print(f"  Elasticsearch: https://ismelkesnode01.{DOMAIN}:9200")
    print(f"  Kibana:        https://ismelkkbnnode01.{DOMAIN}:5601")
    print(f"  Fleet:         https://ismelkflnode01.{DOMAIN}:8220")
    print(f"  elastic:       {elastic_pwd}")
    print("=" * 60)


if __name__ == "__main__":
    main()