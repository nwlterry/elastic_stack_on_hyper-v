#!/usr/bin/env python3
import re, time, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)

for i in range(25):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
    _, o, e = c.exec_command(
        "date; ss -tlnp | grep 8220 || echo NO_8220; "
        "systemctl is-active elastic-agent; "
        "ps aux | grep 'enroll' | grep -v grep | awk '{print $3,$4,$11}' | head -1; "
        "tail -3 /var/log/fleet-install.log 2>/dev/null",
        timeout=30,
    )
    o.channel.recv_exit_status()
    text = (o.read() + e.read()).decode()
    c.close()
    print(f"\n=== poll {i} ===\n{text}", flush=True)
    if ":8220" in text and "NO_8220" not in text.split("8220")[0][-30:]:
        print("SUCCESS", flush=True)
        break
    if "active" in text.split("elastic-agent")[-1][:20] and "NO_8220" not in text:
        if "8220" in text:
            print("SUCCESS", flush=True)
            break
    time.sleep(60)