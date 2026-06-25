#!/usr/bin/env python3
"""Deploy Fleet Server only. Defers if enrollment already in progress."""
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from finish_fleet import (  # noqa: E402
    fleet_ready,
    fleet_starting,
    wait_for_fleet,
    complete_after_fleet,
)
from continue_stack import (  # noqa: E402
    NODES,
    connect,
    copy_scripts,
    run,
    deploy_fleet_server,
    setup_fleet,
    get_or_reset_password,
    VERSION,
    REMOTE,
)

os.environ.setdefault(
    "SSH_PASS",
    re.search(r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()).group(1),
)


def main():
    if fleet_ready():
        print("Fleet Server already on 8220 — deploying agents")
        return complete_after_fleet()

    if fleet_starting():
        print("Fleet enrollment already in progress — waiting only (no redeploy)")
        if not wait_for_fleet(7200):
            print("Timed out. Monitor: python run_with_pass.py fleet_ps.py")
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
        print("Fleet not ready; re-run later")
        return 1

    return complete_after_fleet(pwd)


if __name__ == "__main__":
    sys.exit(main() or 0)