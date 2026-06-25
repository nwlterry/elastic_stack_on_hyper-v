#!/usr/bin/env python3
"""Find current elastic password by probing recent resets (no new reset)."""
import re
import shlex
from pathlib import Path

import paramiko

ROOT = Path(__file__).parent
PWD = re.search(
    r"RootPassword\s*=\s*'([^']+)'", (ROOT / "config.psd1").read_text()
).group(1)

candidates: list[str] = []
term_dir = Path.home() / "terminals"
if term_dir.is_dir():
    files = sorted(term_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat in (r"New value:\s*(\S+)", r"elastic=(\S+)"):
            for m in re.finditer(pat, text):
                pw = m.group(1)
                if pw not in candidates:
                    candidates.append(pw)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.44.40.31", username="root", password=PWD, timeout=25)

for pw in candidates:
    auth = shlex.quote(f"elastic:{pw}")
    cmd = (
        f"curl -sk -o /dev/null -w '%{{http_code}}' -u {auth} https://localhost:9200/"
    )
    _, o, e = c.exec_command(cmd, timeout=20)
    code = (o.read() + e.read()).decode().strip()[-3:]
    if code == "200":
        print(pw)
        c.close()
        raise SystemExit(0)

c.close()
print("UNKNOWN — no cached password matched; run verify_kibana.py for a fresh reset")
raise SystemExit(1)