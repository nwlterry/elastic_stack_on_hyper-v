#!/usr/bin/env python3
"""Kill stuck fleet enroll and run finish_fleet with capabilities-limited install."""
import os
import re
import sys
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).parent
os.environ["SSH_PASS"] = re.search(
    r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()
).group(1)

PWD = os.environ["SSH_PASS"]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
run_cmds = [
    "pkill -9 -f 'elastic-agent.*enroll' 2>/dev/null || true",
    "pkill -9 -f install-fleet-server 2>/dev/null || true",
    "systemctl stop elastic-agent 2>/dev/null || true",
]
for cmd in run_cmds:
    c.exec_command(cmd, timeout=30)
time.sleep(3)
c.close()
print("killed stuck fleet processes", flush=True)

sys.path.insert(0, str(ROOT))
from finish_fleet import main  # noqa: E402

main()