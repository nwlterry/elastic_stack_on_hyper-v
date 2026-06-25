#!/usr/bin/env python3
"""Complete Fleet + Agents after cluster and Kibana are up."""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
os.environ.setdefault(
    "SSH_PASS",
    re.search(r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()).group(1),
)

sys.path.insert(0, str(ROOT))
from continue_stack import (  # noqa: E402
    NODES,
    connect,
    copy_scripts,
    run,
    setup_fleet,
    deploy_fleet_server,
    deploy_agents,
    get_or_reset_password,
    VERSION,
    REMOTE,
)

def fleet_ready() -> bool:
    c = connect(NODES["fleet"][0])
    out = run(
        c,
        "ss -tlnp | grep 8220 || curl -sk -o /dev/null -w '%{http_code}' https://localhost:8220 2>/dev/null",
        check=False,
        timeout=30,
    )
    c.close()
    return "8220" in out or out.strip().endswith(("200", "401"))


def fleet_agent_active() -> bool:
    c = connect(NODES["fleet"][0])
    out = run(c, "systemctl is-active elastic-agent 2>&1", check=False, timeout=30)
    c.close()
    return out.strip() == "active"


def fleet_install_running() -> bool:
    c = connect(NODES["fleet"][0])
    out = run(
        c,
        "ps aux | grep -E '/usr/share/elastic-agent/bin/elastic-agent.*enroll' | "
        "grep -v grep >/dev/null && echo RUNNING || echo IDLE",
        check=False,
        timeout=30,
    )
    c.close()
    return "RUNNING" in out


def fleet_install_script_running() -> bool:
    c = connect(NODES["fleet"][0])
    out = run(
        c,
        "ps aux | grep -E '/opt/elastic-setup/install-fleet-server.sh' | "
        "grep -v grep >/dev/null && echo RUNNING || echo IDLE",
        check=False,
        timeout=30,
    )
    c.close()
    return "RUNNING" in out


def fleet_starting() -> bool:
    return (
        fleet_install_running()
        or fleet_install_script_running()
        or fleet_agent_active()
    )


def wait_for_fleet(timeout_sec: int = 2400) -> bool:
    import time

    for i in range(timeout_sec // 30):
        if fleet_ready():
            print("  Fleet Server listening on 8220")
            return True
        if not fleet_starting():
            return False
        print(f"  waiting for fleet server on 8220... ({i * 30}s)", flush=True)
        time.sleep(30)
    return fleet_ready()


def complete_after_fleet(pwd: str | None = None):
    import time

    c = connect(NODES["es01"][0])
    pwd = pwd or get_or_reset_password(c)
    c.close()
    print(f"elastic: {pwd}")

    c = connect(NODES["kibana"][0])
    copy_scripts(c)
    c.close()

    fleet_info = setup_fleet(pwd)
    if not fleet_info.get("FLEET_POLICY_ID"):
        print("Fleet policy setup failed")
        return 1

    deploy_agents(fleet_info)
    print("\nDONE")
    print(f"  Kibana: http://ismelkkbnnode01.ocplab.net:5601")
    print(f"  elastic: {pwd}")
    return 0


def main():
    import time

    if fleet_ready():
        print("=== Fleet Server already up ===")
        return complete_after_fleet()

    if fleet_starting():
        print("=== Fleet enrollment in progress — waiting only ===")
        print("    (skipping password reset and redeploy to avoid interrupting enroll)")
        if not wait_for_fleet(7200):
            print("Fleet still not ready after 120m. Monitor with: python run_with_pass.py fleet_ps.py")
            return 1
        if not fleet_ready():
            print("Enrollment ended without 8220. Check /var/log/fleet-install.log on fleet node.")
            return 1
        return complete_after_fleet()

    c = connect(NODES["es01"][0])
    pwd = get_or_reset_password(c)
    c.close()
    print(f"elastic: {pwd}")

    c = connect(NODES["kibana"][0])
    copy_scripts(c)
    c.close()

    fleet_info = setup_fleet(pwd)
    if not fleet_info.get("FLEET_POLICY_ID"):
        print("Fleet policy setup failed")
        return 1

    deploy_fleet_server(pwd, fleet_info["FLEET_POLICY_ID"])
    time.sleep(30)
    if not wait_for_fleet(3600):
        print("Fleet Server not ready yet; re-run finish_fleet.py later")
        return 1

    deploy_agents(fleet_info)
    print("\nDONE")
    print(f"  Kibana: http://ismelkkbnnode01.ocplab.net:5601")
    print(f"  elastic: {pwd}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)