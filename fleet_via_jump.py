#!/usr/bin/env python3
"""Deploy Fleet Server via ES01 jump host when direct SSH to fleet is blocked."""
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
DOMAIN = "ocplab.net"

cfg = (ROOT / "config.psd1").read_text()
PASSWORD = re.search(r"RootPassword\s*=\s*'([^']+)'", cfg).group(1)

ES01 = "10.44.40.31"
FLEET = "10.44.40.42"
FLEET_FQDN = f"ismelkflnode01.{DOMAIN}"
ES_FQDN = f"ismelkesnode01.{DOMAIN}"


def connect(ip: str) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PASSWORD, timeout=30)
    return c


def run(c, cmd, check=True, timeout=900) -> str:
    print(f"  $ {cmd[:120]}", flush=True)
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    text = out + err
    if text.strip():
        print(text[-4000:], flush=True)
    if check and code != 0:
        raise RuntimeError(f"FAIL({code}): {err or out}")
    return text


def ssh_prefix() -> str:
    pwd = PASSWORD.replace("'", "'\"'\"'")
    return (
        f"sshpass -p '{pwd}' ssh -o StrictHostKeyChecking=no "
        f"-o ConnectTimeout=15 root@{0}"
    )


def run_jump(jump, target_ip, cmd, check=True, timeout=900) -> str:
    escaped = cmd.replace("'", "'\"'\"'")
    ssh_cmd = ssh_prefix().format(target_ip)
    return run(jump, f"{ssh_cmd} '{escaped}'", check=check, timeout=timeout)


def copy_to_fleet(jump):
    """Tar scripts+packages on jump, scp to fleet."""
    run(jump, f"mkdir -p {REMOTE}/rpms", timeout=60)
    with SCPClient(jump.get_transport()) as scp:
        for f in SCRIPTS.glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
        for f in (ROOT / "packages").iterdir():
            if f.is_file() and ("elastic-agent" in f.name or "GPG" in f.name):
                scp.put(str(f), f"{REMOTE}/rpms/{f.name}")
    run(jump, f"chmod +x {REMOTE}/*.sh", timeout=60)

    run(jump, f"tar czf /tmp/elastic-fleet.tgz -C {REMOTE} .", timeout=120)
    run_jump(jump, FLEET, f"mkdir -p {REMOTE}/rpms", timeout=60)
    pwd = PASSWORD.replace("'", "'\"'\"'")
    run(
        jump,
        f"sshpass -p '{pwd}' scp -o StrictHostKeyChecking=no /tmp/elastic-fleet.tgz "
        f"root@{FLEET}:{REMOTE}/fleet.tgz",
        timeout=300,
    )
    run_jump(jump, FLEET, f"tar xzf {REMOTE}/fleet.tgz -C {REMOTE} && chmod +x {REMOTE}/*.sh", timeout=120)


def get_elastic_password(es) -> str:
    out = run(es, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
    m = re.search(r"New (?:password|value):\s*(\S+)", out)
    if not m:
        raise RuntimeError("no elastic password")
    return m.group(1)


def setup_fleet(kibana_ip, elastic_pwd) -> dict:
    c = connect(kibana_ip)
    run(c, f"mkdir -p {REMOTE}", timeout=60)
    with SCPClient(c.get_transport()) as scp:
        scp.put(str(SCRIPTS / "setup-fleet-kibana.sh"), f"{REMOTE}/setup-fleet-kibana.sh")
    run(c, f"chmod +x {REMOTE}/setup-fleet-kibana.sh", timeout=60)
    out = run(c, f"ELASTIC_PASS='{elastic_pwd}' bash {REMOTE}/setup-fleet-kibana.sh", timeout=300)
    c.close()
    result = {}
    for line in out.splitlines():
        if "=" in line and line.startswith(("FLEET_", "ES_", "KIBANA_")):
            k, v = line.split("=", 1)
            result[k] = v
    return result


def main():
    print("=== Fleet deploy via jump host ===", flush=True)

    es = connect(ES01)
    run(es, "command -v sshpass >/dev/null || dnf install -y sshpass", check=False, timeout=180)
    pwd = get_elastic_password(es)
    print(f"elastic: {pwd}", flush=True)

    token_out = run(
        es,
        f"/usr/share/elasticsearch/bin/elasticsearch-service-tokens create elastic/fleet-server fleet-srv-{int(time.time())}",
        timeout=120,
    )
    m = re.search(r"(AAEAA[\w+/=-]+)", token_out)
    svc_token = m.group(1)
    ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt", timeout=60)

    fleet_info = setup_fleet("10.44.40.41", pwd)
    print(fleet_info, flush=True)
    policy_id = fleet_info["FLEET_POLICY_ID"]

    # Restart sshd on fleet via jump (may clear stuck sessions)
    run_jump(es, FLEET, "systemctl restart sshd || true", check=False, timeout=60)
    time.sleep(5)

    copy_to_fleet(es)

    run_jump(es, FLEET, f"bash {REMOTE}/update-cluster-hosts.sh", timeout=60)
    run_jump(es, FLEET, f"bash {REMOTE}/configure-firewall.sh fleet", check=False, timeout=60)
    run_jump(es, FLEET, "mkdir -p /etc/elasticsearch/certs", timeout=60)
    run_jump(
        es,
        FLEET,
        f"cat > /etc/elasticsearch/certs/http_ca.crt <<'EOF'\n{ca}\nEOF",
        timeout=60,
    )

    # Hard reset agent state on fleet
    run_jump(
        es,
        FLEET,
        "systemctl stop elastic-agent 2>/dev/null; elastic-agent uninstall --force 2>/dev/null; "
        "rpm -e elastic-agent-8.18.4 2>/dev/null; rm -rf /opt/Elastic /var/lib/elastic-agent /etc/elastic-agent; true",
        check=False,
        timeout=300,
    )

    run_jump(
        es,
        FLEET,
        f"bash {REMOTE}/install-fleet-server.sh --version {VERSION} --es-host {ES_FQDN} "
        f"--service-token '{svc_token}' --policy-id '{policy_id}'",
        timeout=2400,
    )

    for i in range(30):
        out = run_jump(
            es,
            FLEET,
            "ss -tlnp | grep 8220 || elastic-agent status 2>&1 | head -20",
            check=False,
            timeout=60,
        )
        if "8220" in out:
            print("Fleet Server UP on 8220", flush=True)
            break
        time.sleep(10)

    # Deploy agents via jump
    es_tok = fleet_info["ES_ENROLLMENT_TOKEN"]
    kb_tok = fleet_info["KIBANA_ENROLLMENT_TOKEN"]
    targets = [
        ("10.44.40.31", es_tok),
        ("10.44.40.32", es_tok),
        ("10.44.40.33", es_tok),
        ("10.44.40.41", kb_tok),
    ]
    for ip, tok in targets:
        sp = PASSWORD.replace("'", "'\"'\"'")
        run(
            es,
            f"sshpass -p '{sp}' scp -o StrictHostKeyChecking=no "
            f"{REMOTE}/install-elastic-agent.sh {REMOTE}/elastic-rpm-install.sh "
            f"root@{ip}:{REMOTE}/ && "
            f"sshpass -p '{sp}' scp -o StrictHostKeyChecking=no "
            f"{REMOTE}/rpms/elastic-agent-{VERSION}-x86_64.rpm root@{ip}:{REMOTE}/rpms/",
            timeout=300,
        )
        run_jump(es, ip, f"chmod +x {REMOTE}/*.sh", timeout=60)
        run_jump(
            es,
            ip,
            f"bash {REMOTE}/install-elastic-agent.sh --version {VERSION} --enrollment-token '{tok}'",
            timeout=1200,
        )
        print(f"agent on {ip} ok", flush=True)

    es.close()
    print(f"\nDONE\n  Kibana: http://ismelkkbnnode01.{DOMAIN}:5601\n  Fleet: https://{FLEET_FQDN}:8220\n  elastic: {pwd}", flush=True)


if __name__ == "__main__":
    main()