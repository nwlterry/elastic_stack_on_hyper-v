#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from continue_stack import NODES, connect, run

c = connect(NODES["fleet"][0])
raw = run(
    c,
    "ls -t /var/log/elastic-agent/*.ndjson 2>/dev/null | head -1 | xargs tail -8",
    check=False,
)
c.close()
for line in raw.splitlines():
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        obj = json.loads(line)
        msg = obj.get("message", "")
        lvl = obj.get("log.level", "")
        if msg:
            print(f"[{lvl}] {msg[:200]}")
    except json.JSONDecodeError:
        print(line[:200])