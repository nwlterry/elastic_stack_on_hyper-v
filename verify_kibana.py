#!/usr/bin/env python3
import os
import re
import time
import paramiko
from scp import SCPClient
from pathlib import Path

ROOT = Path(__file__).parent
SCRIPTS = ROOT / "scripts"
REMOTE = "/opt/elastic-setup"
VERSION = "8.18.4"
DOMAIN = "ocplab.net"
_cfg = (ROOT / "config.psd1").read_text()
PASSWORD = os.environ.get("SSH_PASS") or re.search(r"RootPassword\s*=\s*'([^']+)'", _cfg).group(1)

ES01 = "10.44.40.31"
KIBANA = ("10.44.40.41", f"ismelkkbnnode01.{DOMAIN}")


def connect(ip):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    for _ in range(30):
        try:
            c.connect(ip, username="root", password=PASSWORD, timeout=20)
            return c
        except Exception:
            time.sleep(5)
    raise RuntimeError(f"SSH failed: {ip}")


def run(c, cmd, check=False, timeout=300):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    return (o.read() + e.read()).decode()


def copy_scripts(c):
    run(c, f"mkdir -p {REMOTE}/rpms")
    with SCPClient(c.get_transport()) as scp:
        for f in SCRIPTS.glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
    run(c, f"chmod +x {REMOTE}/*.sh")


def main():
    c = connect(ES01)
    out = run(c, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b 2>&1", timeout=120)
    m = re.search(r"New (?:password|value):\s*(\S+)", out)
    pwd = m.group(1)
    print(f"elastic={pwd}")
    print(run(c, f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cluster/health?pretty'"))
    print(run(c, f"curl -sk -u elastic:{pwd} 'https://localhost:9200/_cat/nodes?v'"))
    c.close()

    ip, fqdn = KIBANA
    c = connect(ip)
    copy_scripts(c)
    run(c, f"bash {REMOTE}/update-cluster-hosts.sh")
    run(c, f"bash {REMOTE}/configure-firewall.sh kibana")
    if "YES" not in run(c, f"rpm -q kibana-{VERSION} 2>/dev/null && echo YES || echo NO"):
        tok = run(connect(ES01), "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana").strip()
        run(
            c,
            f"bash {REMOTE}/install-kibana.sh --version {VERSION} --es-host 10.44.40.31 "
            f"--enrollment-token '{tok}'",
            timeout=900,
        )
    else:
        run(c, "systemctl enable kibana; systemctl restart kibana")

    for i in range(30):
        code = run(
            c,
            "curl -s -o /dev/null -w '%{http_code}' http://localhost:5601 2>/dev/null",
        ).strip()
        active = run(c, "systemctl is-active kibana 2>&1").strip()
        print(f"kibana attempt {i+1}: active={active} http={code[-3:] if code else '???'}")
        if active == "active" and code and code[-3:] in ("200", "302", "401"):
            break
        time.sleep(10)

    out = run(c, f"ELASTIC_PASS='{pwd}' bash {REMOTE}/setup-fleet-kibana.sh", timeout=300)
    for line in out.splitlines():
        if line.startswith(("FLEET_", "ES_", "KIBANA_")):
            print(line)
    c.close()
    print(f"Kibana: http://{fqdn}:5601")


if __name__ == "__main__":
    main()