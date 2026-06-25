#!/usr/bin/env python3
"""Apply Kibana encryption key + stack monitoring, then resume Fleet deploy."""
import os
import re
import subprocess
import sys
from pathlib import Path

import paramiko
from scp import SCPClient

ROOT = Path(__file__).parent
REMOTE = "/opt/elastic-setup"
DOMAIN = "ocplab.net"
PASSWORD = os.environ.get("SSH_PASS") or re.search(
    r"RootPassword\s*=\s*'([^']+)'", open(ROOT / "config.psd1").read()
).group(1)

sys.path.insert(0, str(ROOT))
from deploy_ordered_stack import (  # noqa: E402
    NODES,
    SCRIPTS,
    connect,
    copy_scripts,
    create_service_token,
    deploy_agents,
    deploy_fleet_server,
    get_elastic_password,
    run,
    setup_fleet_policies,
    verify_stack,
    wait_kibana_ready,
)


def main():
    es_ip = NODES["es01"][0]
    kb_ip = NODES["kibana"][0]

    c = connect(es_ip)
    elastic_pwd = get_elastic_password(c)
    c.close()

    c = connect(kb_ip)
    copy_scripts(c, roles=("kibana",))
    run(c, f"bash {REMOTE}/configure-kibana-security.sh", timeout=120)
    run(
        c,
        f"ELASTIC_PASS='{elastic_pwd}' ES_HOST=ismelkesnode01.{DOMAIN} "
        f"bash {REMOTE}/enable-stack-monitoring.sh",
        timeout=120,
    )
    run(c, f"bash {REMOTE}/fix-kibana-access.sh", timeout=300)
    run(c, "grep encryptedSavedObjects /etc/kibana/kibana.yml | head -3", check=False)
    run(c, "grep monitoring /etc/kibana/kibana.yml | head -6", check=False)
    c.close()

    if not wait_kibana_ready(kb_ip):
        print("Kibana not ready after improvements")
        return 1

    fleet_info = setup_fleet_policies(elastic_pwd)
    svc_token, ca = create_service_token(elastic_pwd)

    if not deploy_fleet_server(fleet_info["FLEET_POLICY_ID"], svc_token, ca):
        print("Fleet Server did not start — check /var/log/fleet-install.log")
        return 1

    deploy_agents(fleet_info)
    verify_stack(elastic_pwd)
    print(f"\nDONE  elastic={elastic_pwd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())