#!/usr/bin/env python3
import os, re, paramiko, sys
from pathlib import Path
cfg = (Path(__file__).parent / "config.psd1").read_text()
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", cfg).group(1)
for ip in ["10.44.40.31","10.44.40.42"]:
    print(f"connecting {ip}...", flush=True)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PWD, timeout=15)
    _, o, e = c.exec_command("hostname; uptime", timeout=15)
    print((o.read()+e.read()).decode(), flush=True)
    c.close()
print("ok", flush=True)