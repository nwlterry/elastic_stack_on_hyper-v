#!/usr/bin/env python3
"""
1) UID/GID alignment + NFS path.repo on es01-03 (and kibana export check)
2) Re-register fs snapshot repository
3) Fresh-install Elasticsearch 8.18.4 on es04 and join the 8.18.4 cluster
"""
from __future__ import annotations

import base64
import json
import shlex
import tempfile
import time
from pathlib import Path

from scp import SCPClient

from deploy_ordered_stack import (
    CLUSTER,
    NODES,
    REMOTE,
    connect,
    copy_scripts,
    curl_elastic_auth,
    get_elastic_password,
    run,
)

ES01_IP, ES01_FQDN = NODES["es01"]
ES04_IP, ES04_FQDN = NODES["es04"]
ES_NODES_818 = [NODES["es01"], NODES["es02"], NODES["es03"]]
NFS_SERVER = NODES["kibana"][1]
NFS_EXPORT = "/exports/elasticsearch-snapshots"
MOUNT = "/mnt/es-snapshots"
REPO = "fs_nfs_snapshots"
ES_UID, ES_GID = 994, 991
HOT_ROLES = "[data_hot, ingest, transform, remote_cluster_client]"
RPM_REMOTE = "/opt/elastic-setup/rpms/elasticsearch-8.18.4-x86_64.rpm"


def wait_es(ip: str, auth: str | None = None, timeout: int = 600) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = connect(ip, attempts=2)
            if auth:
                code = run(
                    c,
                    f"curl -sk -u {auth} -o /dev/null -w '%{{http_code}}' https://localhost:9200",
                    check=False,
                )
            else:
                code = run(
                    c,
                    "curl -sk -o /dev/null -w '%{http_code}' https://localhost:9200",
                    check=False,
                )
            c.close()
            if "200" in code or "401" in code:
                return True
        except Exception as e:
            print(f"  wait {ip}: {e}", flush=True)
        time.sleep(8)
    return False


def wait_green(auth: str, timeout: int = 900) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = connect(ES01_IP)
        out = run(
            c,
            f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'",
            check=False,
        )
        c.close()
        print(out[:400], flush=True)
        if '"status" : "green"' in out or '"status":"green"' in out:
            return True
        if '"status" : "yellow"' in out or '"status":"yellow"' in out:
            # yellow ok if only replica issues
            pass
        time.sleep(10)
    return False


def phase_nfs_and_uid(auth: str) -> None:
    print("=== Phase 1: Kibana NFS export + UID/GID + clients ===", flush=True)
    kb = connect(NODES["kibana"][0])
    copy_scripts(kb, roles=("kibana",))
    run(
        kb,
        f"ES_UID={ES_UID} ES_GID={ES_GID} EXPORT_ROOT={shlex.quote(NFS_EXPORT)} "
        f"EXPORT_CLIENTS=10.44.40.0/24 bash {REMOTE}/setup-nfs-snapshot-export.sh",
        timeout=600,
        check=False,
    )
    # ensure hosts + exportfs
    run(
        kb,
        "exportfs -ra; exportfs -v; "
        "firewall-cmd --permanent --add-service=nfs 2>/dev/null; "
        "firewall-cmd --permanent --add-service=rpc-bind 2>/dev/null; "
        "firewall-cmd --permanent --add-service=mountd 2>/dev/null; "
        "firewall-cmd --reload 2>/dev/null; "
        "systemctl enable --now nfs-server rpcbind; "
        "df -h /exports/elasticsearch-snapshots; ls -la /exports/elasticsearch-snapshots | head",
        check=False,
        timeout=120,
    )
    kb.close()

    for ip, fqdn in ES_NODES_818:
        print(f"--- NFS client {fqdn} ---", flush=True)
        c = connect(ip)
        copy_scripts(c, roles=("elasticsearch",))
        # hosts for kibana NFS
        run(
            c,
            f"grep -q ismelkkbnnode01 /etc/hosts || "
            f"echo '{NODES['kibana'][0]} ismelkkbnnode01.ocplab.net ismelkkbnnode01' >> /etc/hosts",
            check=False,
        )
        run(
            c,
            f"ES_UID={ES_UID} ES_GID={ES_GID} "
            f"NFS_SERVER={shlex.quote(NFS_SERVER)} "
            f"NFS_EXPORT={shlex.quote(NFS_EXPORT)} "
            f"MOUNT_POINT={shlex.quote(MOUNT)} "
            f"bash {REMOTE}/setup-es-nfs-repo-client.sh",
            timeout=600,
            check=False,
        )
        c.close()

    # rolling restart for path.repo
    print("=== Rolling restart es01-03 for path.repo ===", flush=True)
    for ip, fqdn in ES_NODES_818:
        print(f"restart {fqdn}", flush=True)
        c = connect(ip)
        run(c, "systemctl restart elasticsearch", timeout=180, check=False)
        c.close()
        wait_es(ip, auth, timeout=300)
        wait_green(auth, timeout=600)


def phase_register_repo(auth: str) -> None:
    print("=== Phase 2: Register snapshot repository ===", flush=True)
    c = connect(ES01_IP)
    body = {
        "type": "fs",
        "settings": {"location": MOUNT, "compress": True},
    }
    print(
        run(
            c,
            f"curl -sk -u {auth} -X PUT -H 'Content-Type: application/json' "
            f"-d {shlex.quote(json.dumps(body))} "
            f"'https://localhost:9200/_snapshot/{REPO}?verify=true&pretty'",
            check=False,
            timeout=180,
        ),
        flush=True,
    )
    print(
        run(
            c,
            f"curl -sk -u {auth} -X POST "
            f"'https://localhost:9200/_snapshot/{REPO}/_verify?pretty'",
            check=False,
            timeout=180,
        ),
        flush=True,
    )
    print(
        run(
            c,
            f"curl -sk -u {auth} 'https://localhost:9200/_snapshot/{REPO}/_all?pretty' | head -c 1500",
            check=False,
        ),
        flush=True,
    )
    c.close()


def phase_fresh_es04(auth: str, elastic_pwd: str) -> None:
    print("=== Phase 3: Fresh install ES 8.18.4 on es04 ===", flush=True)
    # copy rpm to es04
    es1 = connect(ES01_IP)
    c4 = connect(ES04_IP)
    copy_scripts(c4, roles=("elasticsearch",))
    run(c4, "mkdir -p /opt/elastic-setup/rpms /etc/elastic-agent/certs /opt/elastic-setup/certs", check=False)

    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "elasticsearch-8.18.4-x86_64.rpm"
        with SCPClient(es1.get_transport()) as scp:
            scp.get(RPM_REMOTE, str(local))
        with SCPClient(c4.get_transport()) as scp:
            scp.put(str(local), RPM_REMOTE)
    print("rpm copied", flush=True)

    # hosts for cluster + kibana + fleet
    hosts = f"""
# ISM-ELK
{NODES['es01'][0]} ismelkesnode01.ocplab.net ismelkesnode01
{NODES['es02'][0]} ismelkesnode02.ocplab.net ismelkesnode02
{NODES['es03'][0]} ismelkesnode03.ocplab.net ismelkesnode03
{NODES['es04'][0]} ismelkesnode04.ocplab.net ismelkesnode04
{NODES['kibana'][0]} ismelkkbnnode01.ocplab.net ismelkkbnnode01
{NODES['fleet'][0]} ismelkflnode01.ocplab.net ismelkflnode01
"""
    run(
        c4,
        "grep -q ISM-ELK /etc/hosts || cat >> /etc/hosts <<'EOF'\n" + hosts + "EOF\n",
        check=False,
    )

    print("Stop and remove ES 9.4.1, wipe data/config security...", flush=True)
    run(
        c4,
        r"""
set -e
systemctl stop elasticsearch 2>/dev/null || true
systemctl disable elasticsearch 2>/dev/null || true
# wipe data so no 9.x lucene metadata remains
if mountpoint -q /data/elasticsearch; then
  rm -rf /data/elasticsearch/*
else
  mkdir -p /data/elasticsearch
fi
rm -rf /var/log/elasticsearch/* 2>/dev/null || true
rm -rf /etc/elasticsearch/certs /etc/elasticsearch/elasticsearch.keystore 2>/dev/null || true
rpm -e elasticsearch 2>/dev/null || true
# ensure data disk still mounted
if ! mountpoint -q /data/elasticsearch; then
  mount -a || true
fi
df -h /data/elasticsearch
ls -la /data/elasticsearch
""",
        check=False,
        timeout=300,
    )

    print("Install elasticsearch-8.18.4...", flush=True)
    print(
        run(
            c4,
            f"rpm -ivh {shlex.quote(RPM_REMOTE)} 2>&1 | tail -40",
            check=False,
            timeout=600,
        ),
        flush=True,
    )
    # sysctl/limits + path.data/logs (package already installed — skips rpm)
    run(
        c4,
        f"bash {REMOTE}/install-elasticsearch.sh --version 8.18.4 "
        f"--node {ES04_FQDN} --cluster {CLUSTER}",
        timeout=300,
        check=False,
    )
    # Prefer TLS material from es01 (reconfigure-node often fails on reinstalls).
    print("Copy certs + keystore from es01, write join yml...", flush=True)
    run(
        es1,
        "tar -C /etc/elasticsearch -czf /tmp/es-tls.tgz certs elasticsearch.keystore; ls -la /tmp/es-tls.tgz",
        check=False,
    )
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "es-tls.tgz"
        with SCPClient(es1.get_transport()) as scp:
            scp.get("/tmp/es-tls.tgz", str(local))
        with SCPClient(c4.get_transport()) as scp:
            scp.put(str(local), "/tmp/es-tls.tgz")

    yml = f"""cluster.name: {CLUSTER}
node.name: {ES04_FQDN}
path.data: /data/elasticsearch
path.logs: /var/log/elasticsearch
path.repo: ["/mnt/es-snapshots"]
network.host: 0.0.0.0
http.port: 9200
transport.port: 9300
node.roles: {HOT_ROLES}
discovery.seed_hosts: ["10.44.40.31:9300", "10.44.40.32:9300", "10.44.40.33:9300"]
xpack.security.enabled: true
xpack.security.enrollment.enabled: true
xpack.security.http.ssl:
  enabled: true
  keystore.path: certs/http.p12
xpack.security.transport.ssl:
  enabled: true
  verification_mode: certificate
  keystore.path: certs/transport.p12
  truststore.path: certs/transport.p12
"""
    yml_b64 = base64.b64encode(yml.encode()).decode()
    run(
        c4,
        f"""
set -e
systemctl stop elasticsearch 2>/dev/null || true
pkill -9 -u elasticsearch 2>/dev/null || true
sleep 2
rm -rf /etc/elasticsearch/certs /etc/elasticsearch/elasticsearch.keystore
rm -rf /data/elasticsearch/* /var/lib/elasticsearch/* 2>/dev/null || true
mkdir -p /data/elasticsearch /var/log/elasticsearch /etc/elasticsearch
tar -C /etc/elasticsearch -xzf /tmp/es-tls.tgz
echo {yml_b64} | base64 -d > /etc/elasticsearch/elasticsearch.yml
chown -R root:elasticsearch /etc/elasticsearch
chmod 750 /etc/elasticsearch
chmod 660 /etc/elasticsearch/elasticsearch.keystore
chown root:elasticsearch /etc/elasticsearch/elasticsearch.keystore
chmod 640 /etc/elasticsearch/certs/*
chown -R root:elasticsearch /etc/elasticsearch/certs
chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch
cat /etc/elasticsearch/elasticsearch.yml
ls -la /etc/elasticsearch/certs
""",
        check=False,
        timeout=120,
    )

    # NFS client again
    run(
        c4,
        f"ES_UID={ES_UID} ES_GID={ES_GID} "
        f"NFS_SERVER={shlex.quote(NFS_SERVER)} "
        f"NFS_EXPORT={shlex.quote(NFS_EXPORT)} "
        f"MOUNT_POINT={shlex.quote(MOUNT)} "
        f"bash {REMOTE}/setup-es-nfs-repo-client.sh",
        timeout=600,
        check=False,
    )

    # ownership + firewall (config stays root:elasticsearch)
    run(
        c4,
        "chown -R root:elasticsearch /etc/elasticsearch; chmod 750 /etc/elasticsearch; "
        "chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch 2>/dev/null || true; "
        "systemctl enable --now firewalld 2>/dev/null || true; "
        "firewall-cmd --permanent --add-port=9200/tcp; "
        "firewall-cmd --permanent --add-port=9300/tcp; "
        "firewall-cmd --reload 2>/dev/null || true; "
        "id elasticsearch; getent group elasticsearch; rpm -q elasticsearch",
        check=False,
    )

    print("Start elasticsearch on es04...", flush=True)
    run(c4, "systemctl daemon-reload; systemctl enable --now elasticsearch", timeout=180, check=False)
    c4.close()
    es1.close()

    if not wait_es(ES04_IP, auth, timeout=600):
        raise RuntimeError("es04 API not up")

    # wait join
    print("=== Wait for es04 in _cat/nodes ===", flush=True)
    for i in range(60):
        c = connect(ES01_IP)
        out = run(
            c,
            f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,ip,node.role,master,version'",
            check=False,
        )
        c.close()
        print(out, flush=True)
        if "ismelkesnode04" in out and "8.18.4" in out:
            print("JOINED as 8.18.4", flush=True)
            break
        time.sleep(10)
    else:
        # dump es04 logs
        c4 = connect(ES04_IP)
        print(
            run(
                c4,
                "journalctl -u elasticsearch -n 80 --no-pager; "
                "tail -n 80 /var/log/elasticsearch/*.log 2>/dev/null | tail -80",
                check=False,
            )[-3000:],
            flush=True,
        )
        c4.close()
        raise RuntimeError("es04 did not join")

    wait_green(auth, timeout=600)


def main() -> int:
    es = connect(ES01_IP)
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    es.close()
    print(f"cluster password ok, auth ready", flush=True)

    phase_nfs_and_uid(auth)
    phase_register_repo(auth)
    phase_fresh_es04(auth, elastic_pwd)

    c = connect(ES01_IP)
    print("=== FINAL STATUS ===", flush=True)
    print(
        run(
            c,
            f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,ip,node.role,master,version'; "
            f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'; "
            f"curl -sk -u {auth} 'https://localhost:9200/_snapshot?pretty'; "
            f"curl -sk -u {auth} 'https://localhost:9200/_snapshot/{REPO}/_all?pretty' | head -c 1200",
            check=False,
        ),
        flush=True,
    )
    c.close()
    print("\nALL DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
