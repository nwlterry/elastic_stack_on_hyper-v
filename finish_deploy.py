#!/usr/bin/env python3
import os
import re
import time
from pathlib import Path

import paramiko
from scp import SCPClient

PWD = os.environ["SSH_PASS"]
SCRIPTS = Path(__file__).parent / "scripts"
REMOTE = "/opt/elastic-setup"
MASTERS = [
    "ismelkesnode01.ocplab.net",
    "ismelkesnode02.ocplab.net",
    "ismelkesnode03.ocplab.net",
]


def connect(ip: str) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=PWD, timeout=20)
    return c


def run(c, cmd, check=True, timeout=600) -> str:
    print(f"  $ {cmd[:100]}")
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode()
    err = e.read().decode()
    code = o.channel.recv_exit_status()
    if out.strip():
        print(out[-2500:])
    if check and code != 0:
        raise RuntimeError(f"FAIL({code}): {err or out}")
    return out


def copy_scripts(c):
    run(c, f"mkdir -p {REMOTE}", check=False)
    with SCPClient(c.get_transport()) as scp:
        for f in SCRIPTS.glob("*.sh"):
            scp.put(str(f), f"{REMOTE}/{f.name}")
    run(c, f"chmod +x {REMOTE}/*.sh")


def apply_cluster_yml(c, fqdn: str, first_boot: bool = False):
    if first_boot:
        masters_block = f"  - {MASTERS[0]}"
        seeds_block = "  - 10.44.40.31:9300"
    else:
        masters_block = "\n".join(f"  - {m}" for m in MASTERS)
        seeds_block = "\n".join(
            f"  - {ip}:9300" for ip in ("10.44.40.31", "10.44.40.32", "10.44.40.33")
        )
    run(
        c,
        f"""cat > /tmp/es-cluster-snippet <<'EOF'
node.name: {fqdn}
cluster.initial_master_nodes:
{masters_block}
discovery.seed_hosts:
{seeds_block}
EOF""",
    )
    run(
        c,
        r"grep -vE '^(node\.name:|cluster\.initial_master_nodes:|discovery\.seed_hosts:|  - )' "
        r"/etc/elasticsearch/elasticsearch.yml > /tmp/es.base || true",
    )
    run(c, "cat /tmp/es.base /tmp/es-cluster-snippet > /etc/elasticsearch/elasticsearch.yml")
    run(c, "chown root:elasticsearch /etc/elasticsearch/elasticsearch.yml")
    run(c, "chmod 660 /etc/elasticsearch/elasticsearch.yml")


def fix_perms(c):
    run(c, "chown -R root:elasticsearch /etc/elasticsearch")
    run(c, "chmod 2770 /etc/elasticsearch")
    run(c, "test -f /etc/elasticsearch/elasticsearch.keystore && chown elasticsearch:elasticsearch /etc/elasticsearch/elasticsearch.keystore || true")
    run(c, "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch")


def wait_es(c):
    for _ in range(40):
        if "UP" in run(
            c,
            "curl -sk --connect-timeout 2 https://localhost:9200 >/dev/null && echo UP || echo WAIT",
            check=False,
        ):
            return
        time.sleep(5)
    run(c, "tail -20 /var/log/elasticsearch/ism-elk-cluster.log", check=False)
    raise RuntimeError("Elasticsearch not ready")


def bootstrap_node01():
    print("=== Node01: reinstall for TLS certs ===")
    c = connect("10.44.40.31")
    run(c, "systemctl stop elasticsearch || true")
    run(c, "dnf remove -y --disablerepo='*' elasticsearch")
    run(c, "rm -rf /etc/elasticsearch/certs /etc/elasticsearch/elasticsearch.keystore")
    run(c, "dnf install -y --disablerepo='*' --enablerepo=elasticsearch elasticsearch-8.18.4")
    run(c, "test -f /etc/elasticsearch/certs/http.p12 && echo CERTS_OK")
    copy_scripts(c)
    run(
        c,
        f"bash {REMOTE}/install-elasticsearch.sh --version 8.18.4 --node {MASTERS[0]} --cluster ism-elk-cluster",
    )
    apply_cluster_yml(c, MASTERS[0], first_boot=True)
    fix_perms(c)
    run(c, "systemctl start elasticsearch")
    wait_es(c)

    # Capture auto-generated password from install output in journal/rpm logs
    install_log = run(
        c,
        "grep -oP 'generated password.*?:\\s*\\K\\S+' /var/log/messages 2>/dev/null | tail -1",
        check=False,
    ).strip()
    pwd_out = run(c, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b", check=False)
    m = re.search(r"New (?:password|value):\s*(\S+)", pwd_out)
    elastic_pwd = m.group(1) if m else install_log
    if not elastic_pwd:
        elastic_pwd = run(
            c,
            "grep -r 'generated password' /var/log/ 2>/dev/null | tail -1 | awk '{print $NF}'",
            check=False,
        ).strip()
    print(run(c, f"curl -sk -u elastic:{elastic_pwd} https://localhost:9200/_cluster/health?pretty", check=False))

    node2_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
    node3_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
    kibana_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana").strip()
    c.close()
    return elastic_pwd, node2_t, node3_t, kibana_t


def enroll_node(ip: str, fqdn: str, token: str):
    print(f"=== Enroll {fqdn} ===")
    c = connect(ip)
    run(c, "systemctl stop elasticsearch || true")
    run(c, "dnf remove -y --disablerepo='*' elasticsearch || true")
    run(c, "rm -rf /etc/elasticsearch/certs /etc/elasticsearch/elasticsearch.keystore")
    run(c, "dnf install -y --disablerepo='*' --enablerepo=elasticsearch elasticsearch-8.18.4")
    copy_scripts(c)
    run(
        c,
        f"bash {REMOTE}/install-elasticsearch.sh --version 8.18.4 --node {fqdn} --cluster ism-elk-cluster",
    )
    run(c, "chown -R root:elasticsearch /etc/elasticsearch")
    run(c, "chmod 2770 /etc/elasticsearch")
    run(
        c,
        f"echo y | /usr/share/elasticsearch/bin/elasticsearch-reconfigure-node --enrollment-token '{token}'",
    )
    fix_perms(c)
    run(c, "systemctl start elasticsearch")
    wait_es(c)
    c.close()


def deploy_kibana(token: str):
    print("=== Kibana ===")
    c = connect("10.44.40.41")
    copy_scripts(c)
    run(c, "systemctl stop kibana || true")
    run(c, "dnf remove -y --disablerepo='*' kibana || true")
    run(
        c,
        f"bash {REMOTE}/install-kibana.sh --version 8.18.4 --es-host 10.44.40.31 --enrollment-token '{token}'",
    )
    print("kibana status:", run(c, "systemctl is-active kibana", check=False).strip())
    c.close()


def node01_ready() -> bool:
    c = connect("10.44.40.31")
    ok = "UP" in run(
        c,
        "curl -sk --connect-timeout 2 https://localhost:9200 >/dev/null && echo UP || echo WAIT",
        check=False,
    )
    c.close()
    return ok


def resume_from_node01():
    c = connect("10.44.40.31")
    pwd_out = run(c, "/usr/share/elasticsearch/bin/elasticsearch-reset-password -u elastic -b", check=False)
    m = re.search(r"New (?:password|value):\s*(\S+)", pwd_out)
    elastic_pwd = m.group(1) if m else ""
    node2_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
    node3_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node").strip()
    kibana_t = run(c, "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s kibana").strip()
    c.close()
    return elastic_pwd, node2_t, node3_t, kibana_t


def main():
    if node01_ready():
        print("=== Node01 already running — resuming enrollment ===")
        elastic_pwd, node2_t, node3_t, kibana_t = resume_from_node01()
    else:
        elastic_pwd, node2_t, node3_t, kibana_t = bootstrap_node01()

    enroll_node("10.44.40.32", MASTERS[1], node2_t)
    enroll_node("10.44.40.33", MASTERS[2], node3_t)

    time.sleep(20)
    c = connect("10.44.40.31")
    print(run(c, f"curl -sk -u elastic:{elastic_pwd} https://localhost:9200/_cluster/health?pretty"))
    for ip in ["10.44.40.31", "10.44.40.32", "10.44.40.33"]:
        cc = connect(ip)
        print(ip, run(cc, "rpm -q elasticsearch; df -h /data/elasticsearch | tail -1", check=False))
        cc.close()
    c.close()

    deploy_kibana(kibana_t)
    print(f"\nDONE\nelastic password: {elastic_pwd}\nKibana: https://10.44.40.41:5601")


if __name__ == "__main__":
    main()