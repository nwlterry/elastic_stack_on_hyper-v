#!/usr/bin/env python3
"""Install Fleet Server in background on fleet node, then deploy agents."""
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
os.environ["SSH_PASS"] = re.search(
    r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()
).group(1)

sys.path.insert(0, str(ROOT))
from continue_stack import (  # noqa: E402
    NODES,
    VERSION,
    REMOTE,
    connect,
    copy_scripts,
    run,
    setup_fleet,
    deploy_agents,
    get_or_reset_password,
)

def fleet_install_bg(elastic_pwd: str, policy_id: str):
    es_ip = NODES["es01"][0]
    es_fqdn = NODES["es01"][1]
    fleet_ip = NODES["fleet"][0]

    c = connect(es_ip)
    token_name = f"fleet-srv-{int(time.time())}"
    token_out = run(
        c,
        f"/usr/share/elasticsearch/bin/elasticsearch-service-tokens create elastic/fleet-server {token_name}",
    )
    m = re.search(r"(AAEAA[\w+/=-]+)", token_out)
    svc_token = m.group(1)
    ca = run(c, "cat /etc/elasticsearch/certs/http_ca.crt")
    c.close()

    c = connect(fleet_ip)
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

    # Clean slate
    run(
        c,
        "pkill -9 -f elastic-agent 2>/dev/null; systemctl stop elastic-agent 2>/dev/null; "
        "elastic-agent uninstall --force 2>/dev/null; rpm -e elastic-agent-8.18.4 2>/dev/null; "
        "rm -rf /opt/Elastic /var/lib/elastic-agent /etc/elastic-agent; true",
        check=False,
        timeout=300,
    )

    install_cmd = (
        f"bash {REMOTE}/install-fleet-server.sh --version {VERSION} --es-host {es_fqdn} "
        f"--ca-file {REMOTE}/certs/http_ca.crt "
        f"--service-token '{svc_token}' --policy-id '{policy_id}' "
        f"> /var/log/fleet-install.log 2>&1"
    )
    run(c, f"nohup {install_cmd} &", timeout=30)
    c.close()
    print("Fleet install started in background", flush=True)

    for i in range(80):
        time.sleep(30)
        c = connect(fleet_ip)
        status = run(
            c,
            "ss -tlnp | grep 8220; tail -5 /var/log/fleet-install.log 2>/dev/null; "
            "systemctl is-active elastic-agent 2>&1; free -h | head -2",
            check=False,
            timeout=60,
        )
        c.close()
        print(f"\n--- poll {i} ---\n{status[-1500:]}", flush=True)
        if ":8220" in status or "LISTEN" in status and "8220" in status:
            print("Fleet Server UP", flush=True)
            return
        if "Fleet Server running" in status:
            print("Fleet Server UP", flush=True)
            return
        if "Error:" in status and "Fleet Server running" not in status and i > 3:
            tail = status
            if tail.count("Error:") > 0 and "install-fleet" in tail:
                raise RuntimeError(f"Fleet install failed:\n{tail[-2000:]}")
    raise RuntimeError("Fleet install timed out after 30 minutes")


def main():
    c = connect(NODES["es01"][0])
    pwd = get_or_reset_password(c)
    c.close()
    print(f"elastic: {pwd}", flush=True)

    c = connect(NODES["es01"][0])
    ca = run(c, "cat /etc/elasticsearch/certs/http_ca.crt")
    c.close()
    fleet_info = setup_fleet(pwd)
    fleet_install_bg(pwd, fleet_info["FLEET_POLICY_ID"])
    time.sleep(15)
    deploy_agents(fleet_info, ca)
    print(f"\nDONE\n  Kibana: http://ismelkkbnnode01.ocplab.net:5601\n  Fleet: https://ismelkflnode01.ocplab.net:8220\n  elastic: {pwd}", flush=True)


if __name__ == "__main__":
    main()