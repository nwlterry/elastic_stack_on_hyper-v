#!/usr/bin/env python3
import os, re, time, paramiko
from pathlib import Path

cfg = Path("config.psd1").read_text()
PWD = re.search(r"RootPassword\s*=\s*'([^']+)'", cfg).group(1)

def run(ip, cmd, timeout=120):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PWD, timeout=30)
    print(f"\n=== {ip}: {cmd[:80]} ===", flush=True)
    chan = c.get_transport().open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    buf = b""
    start = time.time()
    while True:
        if chan.recv_ready():
            buf += chan.recv(4096)
            print(buf.decode(errors="replace")[-2000:], end="", flush=True)
            buf = buf[-0:]
        if chan.exit_status_ready():
            break
        if time.time() - start > timeout:
            print("\n[timeout]", flush=True)
            break
        time.sleep(1)
    code = chan.recv_exit_status() if chan.exit_status_ready() else -1
    c.close()
    return code

# ES connectivity from fleet
run("10.44.40.42", "curl -sk --cacert /etc/elasticsearch/certs/http_ca.crt https://ismelkesnode01.ocplab.net:9200 2>&1 | head -c 200")
run("10.44.40.42", "ps aux | grep -E 'elastic-agent|install' | grep -v grep")
run("10.44.40.42", "ls -la /var/lib/elastic-agent/ 2>&1; ls -la /opt/Elastic/ 2>&1")
run("10.44.40.42", "journalctl -u elastic-agent --no-pager 2>&1 | tail -30")
run("10.44.40.42", "rpm -q elastic-agent 2>&1; systemctl status elastic-agent --no-pager 2>&1 | head -15")