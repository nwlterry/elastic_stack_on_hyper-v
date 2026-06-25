#!/usr/bin/env python3
"""Fix ES02 cluster, Kibana access, Fleet Server — full redeploy."""
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
POLICY = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
DOMAIN = "ocplab.net"
_cfg = (ROOT / "config.psd1").read_text()
PASSWORD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", _cfg
).group(1)

NODES = {
    "es01": "10.44.40.31",
    "es02": "10.44.40.32",
    "es03": "10.44.40.33",
    "kibana": "10.44.40.41",
    "fleet": "10.44.40.42",
}


def ps(cmd: str) -> str:
    r = subprocess.run(
        ["powershell", "-Command", cmd],
        capture_output=True,
        text=True,
    )
    return (r.stdout + r.stderr).strip()


def connect(ip: str, attempts=40) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for _ in range(attempts):
        try:
            c.connect(ip, username="root", password=PASSWORD, timeout=25)
            return c
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"SSH failed: {ip}")


def run(c, cmd, check=True, timeout=900) -> str:
    print(f"  $ {cmd[:110]}..." if len(cmd) > 110 else f"  $ {cmd}", flush=True)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-2800:], flush=True)
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
                if f.is_file() and ("elastic-agent" in f.name or "GPG" in f.name):
                    scp.put(str(f), f"{REMOTE}/rpms/{f.name}")
    run(c, f"chmod +x {REMOTE}/*.sh")


def update_local_hosts():
    subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "Add-LocalHosts.ps1")],
        check=True,
    )


def hyperv_prep():
    print("=== Hyper-V: stop Fleet, bump 32GB, start ES02 ===", flush=True)
    ps("Stop-VM -Name ISMELKFLNODE01 -Force -ErrorAction SilentlyContinue")
    time.sleep(5)
    ps("Set-VM -Name ISMELKFLNODE01 -MemoryStartupBytes 32GB -ErrorAction SilentlyContinue")
    r = ps(
        "$s=(Get-VM ISMELKESNODE02 -EA SilentlyContinue).State; "
        "if($s -ne 'Running'){Start-VM ISMELKESNODE02; 'es02_started'}else{'es02_running'}"
    )
    print(r, flush=True)
    time.sleep(40)
    print(ps("Get-VM | ? Name -like 'ISMELK*' | % { $_.Name+' '+$_.State+' '+[math]::Round($_.MemoryStartup/1GB)+'GB' }"), flush=True)


def ensure_es02(elastic_pwd: str):
    print("=== Enroll ES02 ===", flush=True)
    ip01, ip02 = NODES["es01"], NODES["es02"]
    fqdn02 = f"ismelkesnode02.{DOMAIN}"

    c = connect(ip01)
    nodes = run(c, f"curl -sk -u elastic:{elastic_pwd} 'https://localhost:9200/_cat/nodes?v&h=name'", check=False)
    if fqdn02 in nodes:
        print(f"  {fqdn02} already in cluster", flush=True)
        c.close()
        return elastic_pwd

    tok = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
    c.close()

    c = connect(ip02)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh elasticsearch", check=False)
    if "YES" not in run(c, f"rpm -q elasticsearch-{VERSION} 2>/dev/null && echo YES || echo NO", check=False):
        run(
            c,
            f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} "
            f"--node {fqdn02} --cluster ism-elk-cluster",
        )
    if "MOUNTED" not in run(c, "mountpoint -q /data/elasticsearch && echo MOUNTED || echo EMPTY", check=False):
        run(c, f"bash {REMOTE}/prepare-data-disk.sh")
    run(c, "systemctl stop elasticsearch 2>/dev/null || true")
    run(c, "chown -R root:elasticsearch /etc/elasticsearch; chmod 2770 /etc/elasticsearch")
    out = run(
        c,
        f"/usr/share/elasticsearch/bin/elasticsearch-reconfigure-node --enrollment-token '{tok}' <<< 'y'",
        check=False,
    )
    if "ERROR" in out or "Aborting" in out:
        run(c, f"dnf remove -y elasticsearch-{VERSION} 2>/dev/null || true", check=False)
        run(
            c,
            f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} "
            f"--node {fqdn02} --cluster ism-elk-cluster",
        )
        run(c, "systemctl stop elasticsearch 2>/dev/null || true")
        run(
            c,
            f"/usr/share/elasticsearch/bin/elasticsearch-reconfigure-node --enrollment-token '{tok}' <<< 'y'",
        )
    run(c, "chown elasticsearch:elasticsearch /etc/elasticsearch/elasticsearch.keystore 2>/dev/null || true")
    run(c, "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch")
    run(c, "systemctl enable elasticsearch; systemctl start elasticsearch")
    c.close()

    for _ in range(40):
        c = connect(ip01)
        h = run(c, f"curl -sk -u elastic:{elastic_pwd} 'https://localhost:9200/_cluster/health?pretty'", check=False)
        c.close()
        if '"number_of_nodes" : 3' in h:
            print(h, flush=True)
            return elastic_pwd
        time.sleep(10)
    raise RuntimeError("ES02 did not join cluster")


def fix_kibana(elastic_pwd: str):
    print("=== Fix Kibana access ===", flush=True)
    c = connect(NODES["kibana"])
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/fix-kibana-access.sh", timeout=300)
    run(c, f"ELASTIC_PASS='{elastic_pwd}' FLEET_HOST=ismelkflnode01.{DOMAIN} bash {REMOTE}/setup-fleet-kibana.sh", timeout=300)
    c.close()
    print("  Kibana: http://10.44.40.41:5601 and http://ismelkkbnnode01.ocplab.net:5601", flush=True)


def deploy_fleet(elastic_pwd: str):
    print("=== Deploy Fleet Server (32GB VM, API token) ===", flush=True)
    ps("Stop-VM -Name ISMELKKBNNODE01 -Force -ErrorAction SilentlyContinue")
    time.sleep(3)
    ps("Start-VM -Name ISMELKFLNODE01")
    time.sleep(35)

    es = connect(NODES["es01"])
    token_name = f"fleet-api-{int(time.time())}"
    tok_out = run(
        es,
        f"curl -sk -u elastic:{elastic_pwd} -X POST "
        f"'https://localhost:9200/_security/service/elastic/fleet-server/credential/token/{token_name}?pretty'",
    )
    m = re.search(r'"value"\s*:\s*"(AAEAA[^"]+)"', tok_out)
    if not m:
        raise RuntimeError("API token parse failed")
    svc = m.group(1)
    auth = run(es, f"curl -sk -H 'Authorization: Bearer {svc}' https://localhost:9200/_security/_authenticate?pretty", check=False)
    if '"username" : "elastic/fleet-server"' not in auth:
        raise RuntimeError("service token auth failed")
    print("  TOKEN AUTH OK", flush=True)
    ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt")
    es.close()

    fl = connect(NODES["fleet"])
    run(
        fl,
        "pkill -9 -f elastic-agent 2>/dev/null; pkill -9 -f install-fleet 2>/dev/null; "
        "elastic-agent uninstall --force 2>/dev/null; rpm -e elastic-agent-8.18.4 2>/dev/null; "
        "rm -rf /var/lib/elastic-agent /etc/elastic-agent; true",
        check=False,
        timeout=300,
    )
    time.sleep(5)
    copy_scripts(fl)
    run(fl, f"bash {REMOTE}/prepare-fleet-memory.sh", check=False)
    run(fl, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(fl, f"bash {REMOTE}/configure-firewall.sh fleet", check=False)
    run(fl, "mkdir -p /etc/elasticsearch/certs")
    run(fl, f"cat > /etc/elasticsearch/certs/http_ca.crt <<'EOF'\n{ca}\nEOF")
    run(
        fl,
        f"nohup bash {REMOTE}/install-fleet-server.sh --version {VERSION} "
        f"--es-host ismelkesnode01.{DOMAIN} --service-token '{svc}' --policy-id '{POLICY}' "
        f"> /var/log/fleet-install.log 2>&1 &",
        timeout=30,
    )
    fl.close()

    print("  Waiting for port 8220 (up to 90 min)...", flush=True)
    for i in range(180):
        time.sleep(30)
        c = connect(NODES["fleet"])
        text = run(
            c,
            "ss -tlnp | grep 8220 || echo NO_8220; systemctl is-active elastic-agent 2>&1; "
            "tail -2 /var/log/fleet-install.log; free -h | head -2",
            check=False,
            timeout=60,
        )
        c.close()
        if i % 4 == 0:
            print(f"  poll {i}: {text[-350:]}", flush=True)
        if ":8220" in text and "NO_8220" not in text.split("8220")[0][-12:]:
            print("  FLEET UP on 8220", flush=True)
            ps("Start-VM -Name ISMELKKBNNODE01")
            time.sleep(25)
            c = connect(NODES["kibana"])
            run(c, f"bash {REMOTE}/fix-kibana-access.sh", check=False, timeout=300)
            c.close()
            return True
        if "Failed to Enroll" in text and "Waiting For Enroll" not in text and i > 10:
            break
    ps("Start-VM -Name ISMELKKBNNODE01")
    return False


def deploy_agents(elastic_pwd: str):
    print("=== Deploy Elastic Agents ===", flush=True)
    sys.path.insert(0, str(ROOT))
    from continue_stack import setup_fleet, deploy_agents as _deploy

    fleet_info = setup_fleet(elastic_pwd)
    if not fleet_info.get("ES_ENROLLMENT_TOKEN"):
        print("  skip agents — no enrollment tokens", flush=True)
        return
    for ip in (NODES["es01"], NODES["es02"], NODES["es03"], NODES["kibana"]):
        c = connect(ip)
        copy_scripts(c)
        tok = fleet_info["ES_ENROLLMENT_TOKEN"]
        if ip == NODES["kibana"]:
            tok = fleet_info["KIBANA_ENROLLMENT_TOKEN"]
        run(
            c,
            f"bash {REMOTE}/install-elastic-agent.sh --version {VERSION} --enrollment-token '{tok}'",
            check=False,
            timeout=600,
        )
        c.close()
    _deploy(fleet_info)


def main():
    update_local_hosts()
    hyperv_prep()

    c = connect(NODES["es01"])
    out = run(c, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
    elastic_pwd = re.search(r"New (?:password|value):\s*(\S+)", out).group(1)
    c.close()
    print(f"elastic={elastic_pwd}", flush=True)

    for ip in NODES.values():
        try:
            c = connect(ip, attempts=12)
            copy_scripts(c)
            run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
            c.close()
        except Exception as exc:
            print(f"  hosts skip {ip}: {exc}", flush=True)

    elastic_pwd = ensure_es02(elastic_pwd)
    fix_kibana(elastic_pwd)

    if not deploy_fleet(elastic_pwd):
        print("\nFleet deploy did not complete — check /var/log/fleet-install.log on fleet node", flush=True)
        return 1

    deploy_agents(elastic_pwd)

    c = connect(NODES["es01"])
    print(run(c, f"curl -sk -u elastic:{elastic_pwd} 'https://localhost:9200/_cluster/health?pretty'", check=False))
    c.close()

    print("\n" + "=" * 60)
    print("REDEPLOY COMPLETE")
    print(f"  ES:    https://ismelkesnode01.{DOMAIN}:9200  (3 nodes)")
    print(f"  Kibana: http://10.44.40.41:5601")
    print(f"  Fleet:  https://ismelkflnode01.{DOMAIN}:8220")
    print(f"  elastic: {elastic_pwd}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())