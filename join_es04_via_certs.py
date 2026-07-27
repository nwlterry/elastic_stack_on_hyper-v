#!/usr/bin/env python3
"""
Join es04 8.18.4 by copying cluster TLS material from es01
(reconfigure-node refuses non-default yml on this install).
"""
from __future__ import annotations

import base64
import io
import tarfile
import tempfile
import time
from pathlib import Path

from scp import SCPClient

from deploy_ordered_stack import (
    NODES,
    connect,
    curl_elastic_auth,
    get_elastic_password,
    run,
)

ES01 = NODES["es01"][0]
ES04 = NODES["es04"][0]
HOT = "[data_hot, ingest, transform, remote_cluster_client]"

YML = """\
cluster.name: ism-elk-cluster
node.name: ismelkesnode04.ocplab.net
path.data: /data/elasticsearch
path.logs: /var/log/elasticsearch
path.repo: ["/mnt/es-snapshots"]
network.host: 0.0.0.0
http.port: 9200
transport.port: 9300
node.roles: [data_hot, ingest, transform, remote_cluster_client]
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


def main() -> int:
    c1 = connect(ES01)
    auth = curl_elastic_auth(get_elastic_password(c1))

    # extract keystore secure settings names + dump certs tarball
    print("=== pack certs + read keystore keys from es01 ===", flush=True)
    ks_list = run(
        c1,
        "/usr/share/elasticsearch/bin/elasticsearch-keystore list 2>&1",
        check=False,
    )
    print(ks_list, flush=True)

    # tar certs + keystore on es01, pull locally, push to es04
    run(
        c1,
        "tar -C /etc/elasticsearch -czf /tmp/es-tls.tgz certs elasticsearch.keystore 2>&1; "
        "ls -la /tmp/es-tls.tgz /etc/elasticsearch/certs",
        check=False,
    )

    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "es-tls.tgz"
        with SCPClient(c1.get_transport()) as scp:
            scp.get("/tmp/es-tls.tgz", str(local))
        c4 = connect(ES04)
        with SCPClient(c4.get_transport()) as scp:
            scp.put(str(local), "/tmp/es-tls.tgz")

    c1.close()

    print("=== apply on es04 ===", flush=True)
    # write yml via base64 to avoid shell quoting issues
    yml_b64 = base64.b64encode(YML.encode()).decode()
    print(
        run(
            c4,
            f"""
set -e
systemctl stop elasticsearch 2>/dev/null || true
pkill -9 -u elasticsearch 2>/dev/null || true
sleep 2
rm -rf /etc/elasticsearch/certs /etc/elasticsearch/elasticsearch.keystore
rm -rf /data/elasticsearch/* 2>/dev/null || true
mkdir -p /data/elasticsearch /var/log/elasticsearch /etc/elasticsearch
tar -C /etc/elasticsearch -xzf /tmp/es-tls.tgz
echo {yml_b64} | base64 -d > /etc/elasticsearch/elasticsearch.yml
# http.p12 from master may be node-specific — for ES 8 cluster join,
# transport.p12 (CA + node key) is what matters for internode; HTTP can reuse CA-based http.p12
# If http.p12 is node-specific, generate http cert later; many lab clusters use shared CA p12.
chown -R root:elasticsearch /etc/elasticsearch
chmod 750 /etc/elasticsearch
chmod 660 /etc/elasticsearch/elasticsearch.keystore
chown root:elasticsearch /etc/elasticsearch/elasticsearch.keystore
chmod 640 /etc/elasticsearch/certs/*
chown -R root:elasticsearch /etc/elasticsearch/certs
chown -R elasticsearch:elasticsearch /data/elasticsearch /var/log/elasticsearch
ls -la /etc/elasticsearch /etc/elasticsearch/certs
cat /etc/elasticsearch/elasticsearch.yml
mountpoint -q /mnt/es-snapshots || mount /mnt/es-snapshots || true
""",
            check=False,
            timeout=120,
        ),
        flush=True,
    )

    print("=== start ===", flush=True)
    print(
        run(
            c4,
            "systemctl reset-failed elasticsearch; systemctl daemon-reload; "
            "systemctl start elasticsearch 2>&1; sleep 10; "
            "systemctl is-active elasticsearch; "
            "journalctl -u elasticsearch -n 50 --no-pager 2>&1 | tail -50",
            check=False,
            timeout=120,
        ),
        flush=True,
    )
    c4.close()

    for i in range(48):
        c4 = connect(ES04, attempts=2)
        st = run(
            c4,
            "systemctl is-active elasticsearch; "
            "curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:9200 || echo 000",
            check=False,
        )
        c4.close()
        print(f"wait {i}: {st}", flush=True)
        if "active" in st and ("401" in st or "200" in st):
            break
        if "failed" in st and i in (2, 5, 10):
            c4 = connect(ES04)
            print(run(c4, "journalctl -u elasticsearch -n 40 --no-pager | tail -40", check=False), flush=True)
            c4.close()
        time.sleep(10)
    else:
        return 1

    for i in range(48):
        c1 = connect(ES01)
        nodes = run(
            c1,
            f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,ip,node.role,master,version'",
            check=False,
        )
        c1.close()
        print(nodes, flush=True)
        if "ismelkesnode04" in nodes and "8.18.4" in nodes:
            print("JOINED OK", flush=True)
            break
        time.sleep(10)
    else:
        return 2

    c1 = connect(ES01)
    print(
        run(
            c1,
            f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'; "
            f"curl -sk -u {auth} 'https://localhost:9200/_snapshot?pretty'; "
            f"curl -sk -u {auth} 'https://localhost:9200/_nodes/ismelkesnode04*/settings?filter_path=**.node.roles,**.path.repo&pretty'",
            check=False,
        ),
        flush=True,
    )
    c1.close()
    print("ALL DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
