#!/usr/bin/env python3
"""Wait for Fleet Server on 8220, then deploy agents."""
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
from finish_fleet import (  # noqa: E402
    wait_for_fleet,
    fleet_ready,
    setup_fleet,
    deploy_agents,
    get_or_reset_password,
    NODES,
    connect,
)


def main():
    print("Waiting for Fleet Server enrollment to complete...")
    if not wait_for_fleet(7200):
        print("Fleet still not ready after 120m")
        return 1
    c = connect(NODES["es01"][0])
    pwd = get_or_reset_password(c)
    c.close()
    fleet_info = setup_fleet(pwd)
    deploy_agents(fleet_info)
    print(f"\nDONE  elastic={pwd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())