#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from continue_stack import NODES, connect, run

c = connect(NODES["fleet"][0])
for cmd in [
    "date; free -h; swapon --show",
    "dmesg -T 2>/dev/null | grep -i 'killed process' | tail -5 || true",
    "tail -5 /var/log/fleet-install.log",
    "ss -tlnp | grep 8220 || echo NO_8220",
]:
    print(f"=== {cmd}")
    print(run(c, cmd, check=False))
c.close()