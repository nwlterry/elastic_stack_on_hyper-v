#!/usr/bin/env python3
import re, time, paramiko
from pathlib import Path
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", Path("config.psd1").read_text()).group(1)

def check():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("10.44.40.42", username="root", password=PWD, timeout=30)
    cmds = [
        "ss -tlnp | grep 8220 || echo NO_8220",
        "systemctl is-active elastic-agent 2>&1",
        "ps aux | grep 'elastic-agent.*enroll' | grep -v grep | wc -l",
        "tail -30 /var/log/elastic-agent/elastic-agent-*.ndjson 2>/dev/null || tail -30 /var/log/elastic-agent/*.log 2>/dev/null || ls -la /var/log/elastic-agent/",
        "elastic-agent status 2>&1 | head -25",
    ]
    out_all = []
    for cmd in cmds:
        _, o, e = c.exec_command(cmd, timeout=30)
        o.channel.recv_exit_status()
        out_all.append(f"--- {cmd} ---\n{(o.read()+e.read()).decode()}")
    c.close()
    return "\n".join(out_all)

for i in range(40):
    print(f"\n===== poll {i} =====", flush=True)
    text = check()
    print(text, flush=True)
    if "8220" in text and "NO_8220" not in text.split("8220")[0][-20:]:
        print("FLEET READY", flush=True)
        break
    if "Fleet Server" in text and "HEALTHY" in text:
        print("FLEET ENROLLED", flush=True)
        break
    if "NO_8220" in text and "enroll" in text and int(re.search(r"(\d+)", text.split("enroll")[0].split("---")[-1]) or "1") == 0:
        if "inactive" not in text:
            break
    time.sleep(30)