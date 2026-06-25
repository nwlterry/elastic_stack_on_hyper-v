#!/usr/bin/env python3
import paramiko, time, re
from pathlib import Path
pwd = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)
for attempt in range(15):
    try:
        print(f"attempt {attempt}...", flush=True)
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("10.44.40.42", username="root", password=pwd, timeout=30, banner_timeout=120)
        for cmd in [
            "uptime",
            "systemctl is-active sshd",
            "ps aux | grep elastic | grep -v grep",
            "ss -tn state established '( sport = :22 )' | wc -l",
            "journalctl -u sshd -n 5 --no-pager",
        ]:
            _, o, e = c.exec_command(cmd, timeout=30)
            print(f"\n=== {cmd} ===\n{(o.read()+e.read()).decode()}", flush=True)
        c.close()
        break
    except Exception as ex:
        print(f"  failed: {ex}", flush=True)
        time.sleep(8)
else:
    raise SystemExit("SSH never connected")