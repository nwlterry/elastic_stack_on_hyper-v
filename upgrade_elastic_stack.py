#!/usr/bin/env python3
"""
Rolling upgrade ism-elk-cluster: 8.18.4 -> 8.19.9 -> 9.4.1 (Elastic requires 8.19 before 9.4).

Node order (homogeneous data+master roles; master upgraded last):
  ES data_cold  -> ismelkesnode03
  ES data_warm  -> ismelkesnode01
  ES data_hot   -> ismelkesnode02 (current master)
  Kibana        -> ismelkkbnnode01
  Fleet Server  -> ismelkflnode01
  Elastic Agent -> all enrolled agents (ES, Kibana, Fleet) via Fleet bulk_upgrade + artifact mirror
"""
from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

from deploy_ordered_stack import (
    NODES,
    REMOTE,
    connect,
    copy_scripts,
    curl_elastic_auth,
    get_elastic_password,
    run,
    wait_kibana_stable,
)
from agent_artifact_upgrade import upgrade_fleet_managed_agents
from scan_cluster_config import main as scan_cluster_config

ROOT = Path(__file__).parent
BASELINE_VERSION = "8.18.4"
INTERMEDIATE_VERSION = "8.19.9"
TARGET_VERSION = "9.4.1"
SNAPSHOT_NAME = "pre-upgrade-9.4.1-20260629-1535"

# Elastic rolling order: cold tier -> warm -> hot/master (3 homogeneous nodes).
ES_UPGRADE_ORDER: list[tuple[str, str, str]] = [
    ("es03", "data_cold", "Upgrade ES data_cold tier (ismelkesnode03)"),
    ("es01", "data_warm", "Upgrade ES data_warm tier (ismelkesnode01)"),
    ("es02", "data_hot_master", "Upgrade ES data_hot + master (ismelkesnode02)"),
]


def stage_packages(c, roles: tuple[str, ...], versions: tuple[str, ...] = ()) -> None:
    copy_scripts(c, roles=roles)
    pkg = ROOT / "packages"
    if not pkg.is_dir():
        raise FileNotFoundError(f"Missing {pkg} — run download_upgrade_packages.py first")
    run(c, f"mkdir -p {REMOTE}/rpms {REMOTE}/archives", check=False)
    want_es = "elasticsearch" in roles
    want_kb = "kibana" in roles
    want_agent = "elastic-agent" in roles
    from scp import SCPClient

    with SCPClient(c.get_transport()) as scp:
        for f in pkg.iterdir():
            if not f.is_file():
                continue
            name = f.name
            if versions and not any(v in name for v in versions):
                continue
            if name.endswith(".rpm") and name.startswith("elasticsearch-") and want_es:
                scp.put(str(f), f"{REMOTE}/rpms/{name}")
            elif name.endswith(".rpm") and name.startswith("kibana-") and want_kb:
                scp.put(str(f), f"{REMOTE}/rpms/{name}")
            elif name.endswith(".tar.gz") and "elastic-agent" in name and want_agent:
                scp.put(str(f), f"{REMOTE}/archives/{name}")
            elif name.endswith((".sha512", ".asc")) and "elastic-agent" in name and want_agent:
                scp.put(str(f), f"{REMOTE}/archives/{name}")
            elif name == "GPG-KEY-elasticsearch" and want_es:
                scp.put(str(f), f"{REMOTE}/rpms/{name}")
            elif name == "GPG-KEY-elastic-agent" and want_agent:
                scp.put(str(f), f"{REMOTE}/archives/{name}")
    run(c, f"chmod +x {REMOTE}/*.sh", check=False)


def cluster_health(es, auth: str) -> dict:
    out = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health'",
        check=False,
    )
    try:
        return json.loads(out.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {}


def wait_cluster_green(es, auth: str, timeout: int = 900) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        health = cluster_health(es, auth)
        status = health.get("status", "")
        print(f"  cluster status={status} nodes={health.get('number_of_nodes')}", flush=True)
        if status in ("green", "yellow") and health.get("relocating_shards", 1) == 0:
            return True
        time.sleep(15)
    return False


def all_es_on_version(es, auth: str, version: str) -> bool:
    out = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?h=name,version&format=json'",
        check=False,
    )
    try:
        rows = json.loads(out.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return False
    if len(rows) != 3:
        return False
    return all(r.get("version") == version for r in rows)


def node_reports_version(es, auth: str, fqdn: str, version: str) -> bool:
    out = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?h=name,version'",
        check=False,
    )
    for line in out.splitlines():
        if fqdn in line:
            return version in line
    return False


def upgrade_es_cluster(version: str, elastic_pwd: str) -> None:
    print(f"\n=== Elasticsearch rolling upgrade -> {version} ===", flush=True)
    auth = curl_elastic_auth(elastic_pwd)
    es_primary = connect(NODES["es01"][0])

    for key, tier, label in ES_UPGRADE_ORDER:
        ip, fqdn = NODES[key]
        print(f"\n--- {label} ({fqdn}) ---", flush=True)
        c = connect(ip)
        stage_packages(c, roles=("elasticsearch",), versions=(version,))
        run(
            c,
            f"bash {REMOTE}/upgrade-elasticsearch-node.sh "
            f"--version {shlex.quote(version)} "
            f"--es-auth {shlex.quote(f'elastic:{elastic_pwd}')}",
            timeout=1800,
        )
        c.close()
        if not wait_cluster_green(es_primary, auth):
            raise RuntimeError(f"Cluster not healthy after upgrading {fqdn}")
        if not node_reports_version(es_primary, auth, fqdn, version):
            raise RuntimeError(f"{fqdn} did not report version {version}")

    es_primary.close()
    es_primary = connect(NODES["es01"][0])
    if not all_es_on_version(es_primary, auth, version):
        out = run(
            es_primary,
            f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'",
            check=False,
        )
        es_primary.close()
        raise RuntimeError(f"Not all nodes on {version} after rolling upgrade:\n{out}")
    es_primary.close()
    print(f"OK all ES nodes on {version}", flush=True)


def upgrade_kibana(version: str) -> None:
    print(f"\n=== Kibana upgrade -> {version} ===", flush=True)
    c = connect(NODES["kibana"][0])
    stage_packages(c, roles=("kibana",), versions=(version,))
    run(c, f"bash {REMOTE}/upgrade-kibana.sh --version {shlex.quote(version)}", timeout=900)
    c.close()
    if not wait_kibana_stable(NODES["kibana"][0], max_attempts=60):
        raise RuntimeError("Kibana not stable after upgrade")





def verify_final(elastic_pwd: str) -> bool:
    auth = curl_elastic_auth(elastic_pwd)
    es = connect(NODES["es01"][0])
    print(run(es, f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,node.role,master,version'", check=False))
    es.close()
    ok = wait_kibana_stable(NODES["kibana"][0], elastic_pwd=elastic_pwd, max_attempts=30)
    kb = connect(NODES["kibana"][0])
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' 'http://127.0.0.1:5601/api/status'",
        check=False,
    )
    kb.close()
    try:
        status = json.loads(out.strip().splitlines()[-1])
        overall = status.get("status", {}).get("overall", {}).get("level", "")
        print(f"Kibana overall status: {overall}", flush=True)
    except json.JSONDecodeError:
        print(out[-500:], flush=True)
    return ok


def main() -> int:
    print("=== Pre-upgrade cluster config snapshot ===", flush=True)
    scan_cluster_config()

    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    print(run(es, f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'", check=False))
    es.close()

    print(f"\nHyper-V checkpoint: {SNAPSHOT_NAME} (all 5 VMs)", flush=True)
    print(
        "Upgrade path: 8.18.4 -> "
        f"{INTERMEDIATE_VERSION} (required for 9.4+) -> {TARGET_VERSION}",
        flush=True,
    )

    upgrade_es_cluster(INTERMEDIATE_VERSION, elastic_pwd)
    upgrade_es_cluster(TARGET_VERSION, elastic_pwd)
    upgrade_kibana(TARGET_VERSION)
    if not upgrade_fleet_managed_agents(TARGET_VERSION, elastic_pwd):
        print("WARN: agent upgrade incomplete", flush=True)
        return 1

    print("\n=== Post-upgrade verification ===", flush=True)
    issues = scan_cluster_config()
    if not verify_final(elastic_pwd):
        print("WARN: Kibana verification incomplete", flush=True)
        return 1
    if issues:
        print(f"WARN: cluster_config_snapshot issues={issues}", flush=True)
    print(f"\nSUCCESS: stack upgraded to {TARGET_VERSION}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())