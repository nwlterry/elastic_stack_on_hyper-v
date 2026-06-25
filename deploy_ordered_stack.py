#!/usr/bin/env python3
"""
Ordered ELK deploy: ES cluster → Kibana → Fleet Server → Agents + integrations.

Fleet Server VM is limited to 8 GB (config.psd1). Kibana must be up before Fleet
policies/service-token enrollment (Fleet UI/API lives in Kibana).
"""
import os
import re
import shlex
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
FLEET_VM = "ISMELKFLNODE01"
KIBANA_VM = "ISMELKKBNNODE01"
FLEET_MEMORY_GB = 8

AGENT_CLEANUP = (
    "pkill -9 -f install-fleet-server 2>/dev/null; "
    "pkill -9 -f 'fleet-server' 2>/dev/null; "
    "pkill -9 -f '/opt/Elastic/Agent' 2>/dev/null; "
    "pkill -9 -f '/opt/elastic-setup/archives/elastic-agent' 2>/dev/null; "
    "pkill -9 -f elastic-agent 2>/dev/null; "
    "systemctl stop elastic-agent 2>/dev/null; "
    "systemctl disable elastic-agent 2>/dev/null; "
    "command -v elastic-agent >/dev/null && elastic-agent uninstall --force 2>/dev/null; "
    "/opt/Elastic/Agent/elastic-agent uninstall --force 2>/dev/null; "
    "rpm -e elastic-agent-8.18.4 2>/dev/null; "
    "rm -f /etc/systemd/system/elastic-agent.service; "
    "rm -rf /etc/systemd/system/elastic-agent.service.d; "
    "systemctl daemon-reload 2>/dev/null; "
    "rm -rf /opt/Elastic /var/lib/elastic-agent /etc/elastic-agent; true"
)

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


def curl_elastic_auth(elastic_pwd: str) -> str:
    return shlex.quote(f"elastic:{elastic_pwd}")


def ps(cmd: str) -> str:
    r = subprocess.run(
        ["powershell", "-Command", cmd],
        capture_output=True,
        text=True,
    )
    return (r.stdout + r.stderr).strip()


def connect(ip: str, attempts: int = 40) -> paramiko.SSHClient:
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


def copy_scripts(c, roles: tuple[str, ...] = ("elasticsearch", "kibana", "elastic-agent")):
    run(c, f"mkdir -p {REMOTE}/rpms {REMOTE}/archives", check=False)
    pkg_map = {
        "elasticsearch": ("elasticsearch", "GPG"),
        "kibana": ("kibana", "GPG"),
        "elastic-agent": ("elastic-agent", "GPG"),
    }
    with SCPClient(c.get_transport()) as scp:
        for f in SCRIPTS.glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
        for f in SCRIPTS.glob("*.py"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
        pkg = ROOT / "packages"
        if pkg.is_dir():
            for f in pkg.iterdir():
                if not f.is_file():
                    continue
                name = f.name
                if "elastic-agent" in roles and "elastic-agent" in name and (
                    name.endswith(".tar.gz") or name.endswith(".zip")
                ):
                    scp.put(str(f), f"{REMOTE}/archives/{name}")
                    continue
                if any(k in name for role in roles for k in (pkg_map.get(role, ()))):
                    scp.put(str(f), f"{REMOTE}/rpms/{name}")
    run(c, f"chmod +x {REMOTE}/*.sh {REMOTE}/*.py 2>/dev/null; true", check=False)


def vm_is_running(name: str) -> bool:
    return ps(f"(Get-VM {name} -EA SilentlyContinue).State") == "Running"


def connect_if_running(ip: str, vm_name: str | None = None, attempts: int = 8) -> paramiko.SSHClient | None:
    if vm_name and not vm_is_running(vm_name):
        return None
    try:
        return connect(ip, attempts=attempts)
    except RuntimeError:
        return None


def update_local_hosts():
    r = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "Add-LocalHosts.ps1")],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(f"  WARN: local hosts update failed: {(r.stdout + r.stderr)[-500:]}", flush=True)


def ensure_vm_running(name: str, wait_sec: int = 40):
    state = ps(f"(Get-VM {name} -EA SilentlyContinue).State")
    if state != "Running":
        print(f"  Starting {name}...", flush=True)
        ps(f"Start-VM -Name {name}")
        time.sleep(wait_sec)


def set_fleet_vm_memory():
    print(f"=== Fleet VM memory: {FLEET_MEMORY_GB} GB ===", flush=True)
    ps(f"Stop-VM -Name {FLEET_VM} -Force -ErrorAction SilentlyContinue")
    time.sleep(5)
    ps(f"Set-VM -Name {FLEET_VM} -MemoryStartupBytes {FLEET_MEMORY_GB}GB")
    print(ps(f"Get-VM {FLEET_VM} | % {{ $_.Name+' '+$_.State+' '+[math]::Round($_.MemoryStartup/1GB)+'GB' }}"), flush=True)


def cleanup_fleet_install():
    """Stop any stuck fleet enrollment from prior runs."""
    print("=== Clean stale Fleet install ===", flush=True)
    ps(f"Stop-VM -Name {FLEET_VM} -Force -ErrorAction SilentlyContinue")
    time.sleep(5)
    try:
        c = connect(NODES["fleet"][0], attempts=8)
        run(c, AGENT_CLEANUP, check=False, timeout=300)
        c.close()
    except Exception as exc:
        print(f"  fleet cleanup skip: {exc}", flush=True)


def wait_es_api(c):
    for _ in range(60):
        out = run(c, "curl -sk --connect-timeout 2 https://localhost:9200 2>&1 | head -c 200", check=False)
        if "security_exception" in out or "tagline" in out:
            time.sleep(8)
            return
        time.sleep(5)
    raise RuntimeError("ES API not ready")


def get_elastic_password(c) -> str:
    for _ in range(20):
        out = run(c, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", check=False, timeout=120)
        m = re.search(r"New (?:password|value):\s*(\S+)", out)
        if m:
            return m.group(1)
        time.sleep(12)
    raise RuntimeError("Could not obtain elastic password")


def ensure_es_node(ip: str, fqdn: str):
    c = connect(ip)
    copy_scripts(c, roles=("elasticsearch",))
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh elasticsearch", check=False)
    if "YES" not in run(c, f"rpm -q elasticsearch-{VERSION} 2>/dev/null && echo YES || echo NO", check=False):
        if "MOUNTED" not in run(c, "mountpoint -q /data/elasticsearch && echo MOUNTED || echo EMPTY", check=False):
            run(c, f"bash {REMOTE}/prepare-data-disk.sh")
        run(c, f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} --node {fqdn} --cluster {CLUSTER}")
    else:
        if "MOUNTED" not in run(c, "mountpoint -q /data/elasticsearch && echo MOUNTED || echo EMPTY", check=False):
            run(c, f"bash {REMOTE}/prepare-data-disk.sh")
        run(c, "systemctl enable elasticsearch; systemctl start elasticsearch", check=False)
    run(c, "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch 2>/dev/null || true")
    c.close()


def bootstrap_es_cluster() -> str:
    print("=== Phase 1: Elasticsearch cluster (3 nodes) ===", flush=True)
    ensure_vm_running("ISMELKESNODE01", 15)
    ensure_vm_running("ISMELKESNODE02", 15)
    ensure_vm_running("ISMELKESNODE03", 15)

    for ip, fqdn in ES_NODES:
        ensure_es_node(ip, fqdn)

    ip, _ = NODES["es01"]
    c = connect(ip)
    copy_scripts(c, roles=("elasticsearch",))
    if "active" not in run(c, "systemctl is-active elasticsearch 2>&1", check=False):
        run(c, f"NODE_IP=10.44.40.31 bash {REMOTE}/fix-es-bootstrap.sh", timeout=300)
        wait_es_api(c)
    elastic_pwd = get_elastic_password(c)

    health = run(
        c,
        f"curl -sk -u {curl_elastic_auth(elastic_pwd)} 'https://localhost:9200/_cluster/health?pretty'",
        check=False,
    )
    nodes = 1
    m = re.search(r'"number_of_nodes"\s*:\s*(\d+)', health)
    if m:
        nodes = int(m.group(1))

    if nodes < 3:
        t2 = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
        t3 = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
        c.close()
        for (nip, nfqdn), tok in zip([NODES["es02"], NODES["es03"]], [t2, t3]):
            c = connect(nip)
            copy_scripts(c, roles=("elasticsearch",))
            run(c, "systemctl stop elasticsearch 2>/dev/null || true")
            run(c, "chown -R root:elasticsearch /etc/elasticsearch; chmod 2770 /etc/elasticsearch")
            enroll_out = run(
                c,
                f"/usr/share/elasticsearch/bin/elasticsearch-reconfigure-node --enrollment-token '{tok}' <<< 'y'",
                check=False,
            )
            if "ERROR" in enroll_out or "Aborting" in enroll_out:
                run(c, f"dnf remove -y elasticsearch-{VERSION} 2>/dev/null || true", check=False)
                run(
                    c,
                    f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} --node {nfqdn} --cluster {CLUSTER}",
                )
                run(c, "systemctl stop elasticsearch 2>/dev/null || true")
                run(
                    c,
                    f"/usr/share/elasticsearch/bin/elasticsearch-reconfigure-node --enrollment-token '{tok}' <<< 'y'",
                )
            run(c, "chown elasticsearch:elasticsearch /etc/elasticsearch/elasticsearch.keystore 2>/dev/null || true")
            run(c, "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch")
            run(c, "systemctl enable elasticsearch; systemctl start elasticsearch")
            wait_es_api(c)
            c.close()
            print(f"  enrolled {nfqdn}", flush=True)
        c = connect(ip)
    else:
        print(f"  cluster already has {nodes} nodes", flush=True)

    for _ in range(40):
        h = run(
            c,
            f"curl -sk -u {curl_elastic_auth(elastic_pwd)} 'https://localhost:9200/_cluster/health?pretty'",
            check=False,
        )
        if '"status" : "green"' in h and '"number_of_nodes" : 3' in h:
            print(h, flush=True)
            c.close()
            return elastic_pwd
        time.sleep(10)
    c.close()
    raise RuntimeError("ES cluster not green with 3 nodes")


def wait_kibana_stable(
    ip: str,
    elastic_pwd: str | None = None,
    consecutive: int = 3,
    max_attempts: int = 60,
) -> bool:
    """Wait until Kibana HTTP responds and /api/status reports available (stable)."""
    c = connect(ip)
    ok_streak = 0
    auth = f"-u {curl_elastic_auth(elastic_pwd)}" if elastic_pwd else ""
    for attempt in range(max_attempts):
        code = run(
            c,
            "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5601 2>/dev/null || echo 000",
            check=False,
        ).strip()
        if not code or code[-3:] not in ("200", "302", "401", "403"):
            ok_streak = 0
            if attempt % 6 == 0:
                print(f"  Kibana HTTP not ready (code={code[-3:] if code else '000'})", flush=True)
            time.sleep(10)
            continue

        status = run(
            c,
            f"curl -s {auth} 'http://127.0.0.1:5601/api/status' 2>/dev/null | "
            "python3 -c \"import sys,json; d=json.load(sys.stdin); "
            "print(d.get('status',{}).get('overall',{}).get('level',''))\"",
            check=False,
        ).strip()
        if status in ("available", "green"):
            ok_streak += 1
            if ok_streak >= consecutive:
                print(f"  Kibana stable ({consecutive} consecutive /api/status checks)", flush=True)
                c.close()
                return True
        else:
            ok_streak = 0
            if attempt % 6 == 0:
                print("  Kibana up but /api/status not yet available", flush=True)
        time.sleep(10)
    c.close()
    return False


def wait_kibana_ready(ip: str, elastic_pwd: str | None = None) -> bool:
    return wait_kibana_stable(ip, elastic_pwd=elastic_pwd, consecutive=1, max_attempts=48)


def deploy_kibana(elastic_pwd: str):
    print("=== Phase 2: Kibana (after ES cluster ready) ===", flush=True)
    ensure_vm_running(KIBANA_VM, 45)

    c = connect(NODES["es01"][0])
    kibana_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana").strip()
    c.close()

    ip, fqdn = NODES["kibana"]
    c = connect(ip)
    copy_scripts(c, roles=("kibana",))
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh kibana", check=False)
    if "YES" not in run(c, f"rpm -q kibana-{VERSION} 2>/dev/null && echo YES || echo NO", check=False):
        run(
            c,
            f"bash {REMOTE}/install-kibana.sh --version {VERSION} --es-host 10.44.40.31 "
            f"--enrollment-token '{kibana_t}'",
        )
    run(c, f"bash {REMOTE}/configure-kibana-security.sh", timeout=120)
    run(
        c,
        f"ELASTIC_PASS={shlex.quote(elastic_pwd)} ES_HOST=ismelkesnode01.{DOMAIN} "
        f"bash {REMOTE}/enable-stack-monitoring.sh",
        timeout=120,
    )
    run(c, f"bash {REMOTE}/fix-kibana-access.sh", timeout=300)
    c.close()

    if not wait_kibana_stable(ip, elastic_pwd=elastic_pwd):
        raise RuntimeError("Kibana not stable on port 5601 (/api/status)")
    print(f"  Kibana stable: http://{ip}:5601 and http://{fqdn}:5601", flush=True)


def _parse_fleet_output(out: str) -> dict:
    result = {}
    for line in out.splitlines():
        if "=" in line and line.startswith(("FLEET_", "ES_", "KIBANA_", "INTEGRATION_")):
            k, v = line.split("=", 1)
            result[k] = v
    return result


def _run_fleet_setup(elastic_pwd: str, phase: str) -> dict:
    ip = NODES["kibana"][0]
    if not wait_kibana_stable(ip, elastic_pwd=elastic_pwd):
        raise RuntimeError("Kibana must be stable before Fleet API setup")

    c = connect(ip)
    copy_scripts(c, roles=("kibana",))
    fleet_host = NODES["fleet"][1]
    out = run(
        c,
        f"ELASTIC_PASS={shlex.quote(elastic_pwd)} FLEET_HOST={fleet_host} "
        f"SETUP_PHASE={phase} bash {REMOTE}/setup-fleet-kibana.sh",
        timeout=600,
    )
    c.close()
    return _parse_fleet_output(out)


def setup_fleet_server_policy(elastic_pwd: str) -> dict:
    print("=== Phase 3a: Fleet Server policy only (Kibana stable) ===", flush=True)
    result = _run_fleet_setup(elastic_pwd, "fleet-server")
    if not result.get("FLEET_POLICY_ID"):
        raise RuntimeError("Fleet Server policy setup failed")
    print(f"  fleet policy: {result['FLEET_POLICY_ID']}", flush=True)
    return result


def setup_agent_policies(elastic_pwd: str) -> dict:
    print("=== Phase 4: Agent policies + integrations (Fleet Server up) ===", flush=True)
    fleet_ip = NODES["fleet"][0]
    if not wait_fleet_server_ready(fleet_ip):
        raise RuntimeError("Fleet Server must be enrolled before agent policy setup")

    result = _run_fleet_setup(elastic_pwd, "agents")
    if not result.get("ES_ENROLLMENT_TOKEN") or not result.get("KIBANA_ENROLLMENT_TOKEN"):
        raise RuntimeError("Agent policy / enrollment token setup failed")
    print(f"  agent policies: ES={result.get('ES_POLICY_ID')} Kibana={result.get('KIBANA_POLICY_ID')}", flush=True)
    return result


def setup_fleet_policies(elastic_pwd: str) -> dict:
    """Legacy: all policies in one step."""
    print("=== Fleet policies (all phases) ===", flush=True)
    result = _run_fleet_setup(elastic_pwd, "all")
    if not result.get("FLEET_POLICY_ID"):
        raise RuntimeError("Fleet policy setup failed")
    print(f"  policies: {result}", flush=True)
    return result


def fleet_server_is_healthy(ip: str | None = None) -> bool:
    """Quick check: Fleet Server enrolled, port 8220 up, fleet component HEALTHY."""
    ip = ip or NODES["fleet"][0]
    try:
        c = connect(ip, attempts=3)
    except RuntimeError:
        return False
    try:
        text = run(
            c,
            "ss -tlnp | grep 8220 || echo NO_8220; "
            "systemctl is-active elastic-agent 2>&1; "
            "elastic-agent status 2>&1 | head -20",
            check=False,
            timeout=45,
        )
    finally:
        c.close()
    port_up = ":8220" in text and "NO_8220" not in text
    agent_active = bool(re.search(r"(?m)^active\s*$", text))
    return port_up and agent_active and _fleet_enrollment_complete(text)


def _fleet_enrollment_complete(status_text: str) -> bool:
    """True only when Fleet Server finished enrolling (not merely listening on 8220)."""
    if "Waiting on fleet-server input" in status_text:
        return False
    if "Waiting For Enroll" in status_text:
        return False
    if "status: (STARTING)" in status_text:
        return False
    if "status: (FAILED)" in status_text:
        return False
    # Top-level fleet component healthy (not just elastic-agent parent).
    fleet_block = status_text.split("┌─ fleet", 1)[-1].split("└─ elastic-agent", 1)[0] if "┌─ fleet" in status_text else ""
    if fleet_block and "(HEALTHY)" in fleet_block and "Waiting on fleet-server input" not in fleet_block:
        return True
    if "Connected to" in status_text or "(HEALTHY) Connected" in status_text:
        return True
    return False


def wait_fleet_server_ready(ip: str, max_polls: int = 180) -> bool:
    """Wait until Fleet Server enrollment completes and port 8220 is healthy."""
    print("  Waiting for Fleet Server enrollment to complete (up to 90 min)...", flush=True)
    for i in range(max_polls):
        time.sleep(30)
        try:
            c = connect(ip, attempts=6)
        except RuntimeError:
            if i % 4 == 0:
                print(f"  poll {i}: fleet VM SSH not ready", flush=True)
            continue
        text = run(
            c,
            "ps aux | grep -E 'install-fleet-server|elastic-agent enroll' | grep -v grep || echo ENROLL_DONE; "
            "ss -tlnp | grep 8220 || echo NO_8220; "
            "systemctl is-active elastic-agent 2>&1; "
            "elastic-agent status 2>&1 | head -20; "
            "tail -3 /var/log/fleet-install.log",
            check=False,
            timeout=60,
        )
        c.close()
        if i % 4 == 0:
            print(f"  poll {i}: {text[-500:]}", flush=True)
        port_up = ":8220" in text and "NO_8220" not in text
        agent_active = bool(re.search(r"(?m)^active\s*$", text))
        enroll_done = "ENROLL_DONE" in text
        if port_up and agent_active and enroll_done and _fleet_enrollment_complete(text):
            print("  Fleet Server enrollment complete on 8220", flush=True)
            return True
        if "Failed to Enroll" in text and "Waiting For Enroll" not in text and i > 8:
            break
    return False


def create_service_token(elastic_pwd: str) -> tuple[str, str]:
    c = connect(NODES["es01"][0])
    token_name = f"fleet-api-{int(time.time())}"
    tok_out = run(
        c,
        f"curl -sk -u {curl_elastic_auth(elastic_pwd)} -X POST "
        f"'https://localhost:9200/_security/service/elastic/fleet-server/credential/token/{token_name}?pretty'",
    )
    m = re.search(r'"value"\s*:\s*"(AAEAA[^"]+)"', tok_out)
    if not m:
        raise RuntimeError("API service token parse failed")
    svc = m.group(1)
    auth = run(
        c,
        f"curl -sk -H 'Authorization: Bearer {svc}' https://localhost:9200/_security/_authenticate?pretty",
        check=False,
    )
    if '"username" : "elastic/fleet-server"' not in auth:
        raise RuntimeError("service token auth failed")
    ca = run(c, "cat /etc/elasticsearch/certs/http_ca.crt")
    c.close()
    return svc, ca


def install_es_ca_on_node(c, ca: str):
    """Stage Elasticsearch http CA (survives elastic-agent cleanup)."""
    run(c, f"mkdir -p {REMOTE}/certs /etc/elasticsearch/certs /etc/elastic-agent/certs")
    for path in (
        f"{REMOTE}/certs/http_ca.crt",
        "/etc/elasticsearch/certs/http_ca.crt",
        "/etc/elastic-agent/certs/http_ca.crt",
    ):
        run(c, f"cat > {path} <<'EOF'\n{ca}\nEOF")
        run(c, f"chmod 644 {path}")


def deploy_fleet_server(fleet_policy_id: str, svc_token: str, ca: str) -> bool:
    print(f"=== Phase 3b: Fleet Server ({FLEET_MEMORY_GB} GB VM) ===", flush=True)
    set_fleet_vm_memory()
    ensure_vm_running(FLEET_VM, 40)

    ip, es_fqdn = NODES["fleet"][0], NODES["es01"][1]
    c = connect(ip)
    copy_scripts(c, roles=("elastic-agent",))
    run(c, AGENT_CLEANUP, check=False, timeout=300)
    run(c, f"bash {REMOTE}/prepare-fleet-memory.sh", check=False)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh fleet", check=False)
    install_es_ca_on_node(c, ca)
    run(
        c,
        f"nohup env FLEET_MEMORY_MAX=8G FLEET_MEMORY_HIGH=6G bash {REMOTE}/install-fleet-server.sh "
        f"--version {VERSION} --es-host {es_fqdn} "
        f"--ca-file {REMOTE}/certs/http_ca.crt "
        f"--service-token '{svc_token}' "
        f"--policy-id '{fleet_policy_id}' > /var/log/fleet-install.log 2>&1 &",
        timeout=30,
    )
    c.close()

    return wait_fleet_server_ready(ip)


def deploy_agents(fleet_info: dict, ca: str):
    print("=== Phase 5: Elastic Agents on all nodes ===", flush=True)
    es_tok = fleet_info.get("ES_ENROLLMENT_TOKEN", "")
    kb_tok = fleet_info.get("KIBANA_ENROLLMENT_TOKEN", "")
    fleet_url = f"https://{NODES['fleet'][1]}:8220"
    es_fqdn = NODES["es01"][1]
    ca_arg = f"--ca-file {REMOTE}/certs/http_ca.crt"

    for ip, fqdn in ES_NODES:
        c = connect(ip)
        copy_scripts(c, roles=("elastic-agent",))
        install_es_ca_on_node(c, ca)
        run(
            c,
            f"bash {REMOTE}/install-elastic-agent.sh --version {VERSION} "
            f"--fleet-url '{fleet_url}' --enrollment-token '{es_tok}' "
            f"--es-host {es_fqdn} {ca_arg}",
            check=False,
            timeout=600,
        )
        c.close()
        print(f"  agent on {fqdn}", flush=True)

    ip, fqdn = NODES["kibana"]
    c = connect(ip)
    copy_scripts(c, roles=("elastic-agent",))
    install_es_ca_on_node(c, ca)
    run(
        c,
        f"bash {REMOTE}/install-elastic-agent.sh --version {VERSION} "
        f"--fleet-url '{fleet_url}' --enrollment-token '{kb_tok}' "
        f"--es-host {es_fqdn} {ca_arg}",
        check=False,
        timeout=600,
    )
    c.close()
    print(f"  agent on {fqdn}", flush=True)


def verify_stack(elastic_pwd: str):
    print("=== Verification ===", flush=True)
    c = connect(NODES["es01"][0])
    auth = curl_elastic_auth(elastic_pwd)
    print(run(c, f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'", check=False))
    print(run(c, f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,node.role,master'", check=False))
    c.close()

    c = connect(NODES["kibana"][0])
    print(run(c, "curl -s -o /dev/null -w 'kibana_http:%{http_code}\n' http://10.44.40.41:5601", check=False))
    print(
        run(
            c,
            f"curl -s -u {curl_elastic_auth(elastic_pwd)} "
            "'http://localhost:5601/api/fleet/agents?perPage=10' 2>/dev/null | head -c 600",
            check=False,
        )
    )
    c.close()

    c = connect(NODES["fleet"][0])
    print(run(c, "ss -tlnp | grep 8220 || echo NO_8220", check=False))
    print(run(c, "systemctl show elastic-agent -p MemoryMax -p MemoryHigh --no-pager", check=False))
    c.close()


def main():
    update_local_hosts()
    cleanup_fleet_install()

    vm_by_ip = {
        NODES["es01"][0]: "ISMELKESNODE01",
        NODES["es02"][0]: "ISMELKESNODE02",
        NODES["es03"][0]: "ISMELKESNODE03",
        NODES["kibana"][0]: KIBANA_VM,
        NODES["fleet"][0]: FLEET_VM,
    }
    for ip, fqdn in NODES.values():
        c = connect_if_running(ip, vm_by_ip.get(ip), attempts=6)
        if not c:
            print(f"  hosts skip {fqdn} (VM offline)", flush=True)
            continue
        copy_scripts(c, roles=("elasticsearch",))
        run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
        c.close()

    elastic_pwd = bootstrap_es_cluster()
    deploy_kibana(elastic_pwd)
    fleet_policy = setup_fleet_server_policy(elastic_pwd)
    svc_token, ca = create_service_token(elastic_pwd)

    if not deploy_fleet_server(fleet_policy["FLEET_POLICY_ID"], svc_token, ca):
        print("\nFleet Server did not start — check /var/log/fleet-install.log on fleet node", flush=True)
        return 1

    agent_info = setup_agent_policies(elastic_pwd)
    deploy_agents(agent_info, ca)
    verify_stack(elastic_pwd)

    print("\n" + "=" * 60)
    print("ORDERED DEPLOY COMPLETE")
    print(f"  ES:     https://ismelkesnode01.{DOMAIN}:9200  (3 nodes, green)")
    print(f"  Kibana: http://10.44.40.41:5601")
    print(f"  Fleet:  https://ismelkflnode01.{DOMAIN}:8220  (VM {FLEET_MEMORY_GB}GB, MemoryMax=8G)")
    print(f"  elastic: {elastic_pwd}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())