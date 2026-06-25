#!/usr/bin/env python3
"""Deploy Fleet Server only with fixed install script."""
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
cfg = (ROOT / "config.psd1").read_text()
os.environ["SSH_PASS"] = re.search(r"RootPassword\s*=\s*'([^']+)'", cfg).group(1)

sys.path.insert(0, str(ROOT))
from continue_stack import (  # noqa: E402
    NODES,
    connect,
    run,
    setup_fleet,
    deploy_fleet_server,
    deploy_agents,
    get_or_reset_password,
)


def main():
    print("=== Fleet deploy start ===", flush=True)
    c = connect(NODES["es01"][0])
    pwd = get_or_reset_password(c)
    c.close()
    print(f"elastic: {pwd}", flush=True)

    fleet_info = setup_fleet(pwd)
    print(fleet_info, flush=True)

    deploy_fleet_server(pwd, fleet_info["FLEET_POLICY_ID"])

    for i in range(30):
        c = connect(NODES["fleet"][0])
        out = run(
            c,
            "ss -tlnp | grep 8220 || elastic-agent status 2>&1 | head -15",
            check=False,
            timeout=30,
        )
        c.close()
        print(f"fleet check {i}: {out[-500:]}", flush=True)
        if "8220" in out:
            print("Fleet Server listening on 8220", flush=True)
            break
        time.sleep(10)

    deploy_agents(fleet_info)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()