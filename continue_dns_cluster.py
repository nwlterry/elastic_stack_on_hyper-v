#!/usr/bin/env python3
"""Update VM hosts, enroll ES02, verify Kibana — skip Fleet (enroll may be in progress)."""
import os
import re
import subprocess
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
    for attempt in range(40):
        try:
            c.connect(ip, username="root", password=PASSWORD, timeout=30)
            return c
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"SSH failed: {ip}")


def run(c, cmd, check=True, timeout=900) -> str:
    print(f"  $ {cmd[:120]}..." if len(cmd) > 120 else f"  $ {cmd}")
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-2500:])
    if check and code != 0:
        raise RuntimeError(f"FAIL({code}): {err or out}")
    return text


def copy_scripts(c):
    run(c, f"mkdir -p {REMOTE}/rpms", check=False)
    with SCPClient(c.get_transport()) as scp:
        for f in SCRIPTS.glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
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
        try:
            c = connect(ip)
            copy_scripts(c)
            run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
            c.close()
            print(f"  {fqdn} ok")
        except Exception as exc:
            print(f"  {fqdn} SKIP ({exc})")


def start_es02():
    print("=== Start ISMELKESNODE02 ===")
    r = subprocess.run(
        [
            "powershell",
            "-Command",
            "$s = (Get-VM -Name ISMELKESNODE02 -ErrorAction SilentlyContinue).State; "
            "if ($s -ne 'Running') { Start-VM -Name ISMELKESNODE02; 'started' } else { 'already_running' }",
        ],
        capture_output=True,
        text=True,
    )
    print(r.stdout.strip() or r.stderr.strip())
    if "started" in (r.stdout or ""):
        print("  waiting 45s for boot...")
        time.sleep(45)


def ensure_es_node(ip: str, fqdn: str):
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh elasticsearch", check=False)
    installed = run(
        c, f"rpm -q elasticsearch-{VERSION} 2>/dev/null && echo YES || echo NO", check=False
    )
    mounted = run(c, "mountpoint -q /data/elasticsearch && echo MOUNTED || echo EMPTY", check=False)
    if "MOUNTED" not in mounted:
        run(c, f"bash {REMOTE}/prepare-data-disk.sh")
    if "YES" not in installed:
        run(
            c,
            f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} "
            f"--node {fqdn} --cluster {CLUSTER}",
        )
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


def get_elastic_password(c) -> str:
    out = run(c, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", check=False, timeout=120)
    m = re.search(r"New (?:password|value):\s*(\S+)", out)
    if m:
        return m.group(1)
    raise RuntimeError("Could not obtain elastic password")


def enroll_es02(elastic_pwd: str):
    print("=== Enroll ES02 into cluster ===")
    ip01, _ = NODES["es01"]
    nip, nfqdn = NODES["es02"]

    c = connect(ip01)
    health = run(
        c,
        f"curl -sk -u elastic:{elastic_pwd} 'https://localhost:9200/_cat/nodes?v&h=name'",
        check=False,
    )
    if nfqdn in health:
        print(f"  {nfqdn} already in cluster")
        c.close()
        return

    tok = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
    c.close()

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

    time.sleep(15)
    c = connect(ip01)
    print(run(c, f"curl -sk -u elastic:{elastic_pwd} 'https://localhost:9200/_cluster/health?pretty'"))
    print(run(c, f"curl -sk -u elastic:{elastic_pwd} 'https://localhost:9200/_cat/nodes?v'"))
    c.close()


def deploy_kibana(elastic_pwd: str):
    print("=== Kibana ===")
    c = connect(NODES["es01"][0])
    kibana_t = run(
        c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana"
    ).strip()
    c.close()

    ip, fqdn = NODES["kibana"]
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh kibana", check=False)
    if "YES" not in run(c, f"rpm -q kibana-{VERSION} 2>/dev/null && echo YES || echo NO", check=False):
        run(
            c,
            f"bash {REMOTE}/install-kibana.sh --version {VERSION} --es-host 10.44.40.31 "
            f"--enrollment-token '{kibana_t}'",
        )
    else:
        run(c, "systemctl enable kibana; systemctl restart kibana")
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
            print(f"  Kibana ready at http://{fqdn}:5601 (HTTP {code[-3:]})")
            break
        time.sleep(10)
    else:
        print("  WARN: Kibana not responding on 5601 yet")
    c.close()


def setup_fleet_policies(elastic_pwd: str):
    print("=== Fleet policies (Kibana API) ===")
    c = connect(NODES["kibana"][0])
    copy_scripts(c)
    out = run(c, f"ELASTIC_PASS='{elastic_pwd}' bash {REMOTE}/setup-fleet-kibana.sh")
    c.close()
    for line in out.splitlines():
        if "=" in line and line.startswith(("FLEET_", "ES_", "KIBANA_")):
            print(f"  {line}")


def main():
    update_local_hosts()
    start_es02()
    update_vm_hosts()
    c = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(c)
    c.close()
    print(f"  elastic password: {elastic_pwd}")
    enroll_es02(elastic_pwd)
    deploy_kibana(elastic_pwd)
    setup_fleet_policies(elastic_pwd)
    print("\n" + "=" * 60)
    print("DNS + cluster + Kibana complete (Fleet left running)")
    print(f"  Elasticsearch: https://ismelkesnode01.{DOMAIN}:9200")
    print(f"  Kibana:        http://ismelkkbnnode01.{DOMAIN}:5601")
    print(f"  elastic:       {elastic_pwd}")
    print("=" * 60)


if __name__ == "__main__":
    main()