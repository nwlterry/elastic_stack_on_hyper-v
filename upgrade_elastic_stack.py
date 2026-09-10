#!/usr/bin/env python3
"""
Rolling upgrade ism-elk-cluster.

Supported --to targets:
  8.19.18  Elasticsearch + Kibana + Fleet-managed agents (required before 9.1+)
  9.5.3    Major upgrade; all ES nodes must already be on 8.19.x

Node order: non-master ES first (es04, es03, es01, es02), current master last;
then Kibana; then Fleet Server + agents via Fleet bulk_upgrade + artifact mirror.

Do not run 8.19.18 and 9.5.3 in one shot. Stay on 8.19.18, run Upgrade Assistant,
take Hyper-V + NFS snapshots, then --to 9.5.3.
"""
from __future__ import annotations

import argparse
import json
import shlex
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
INTERMEDIATE_VERSION = "8.19.18"
TARGET_VERSION = "9.5.3"
SNAPSHOT_NAME = "pre-upgrade-9.5.3"
EXPECTED_ES_NODES = 4

# Fallback labels if dynamic master detection is unavailable.
ES_UPGRADE_ORDER: list[tuple[str, str, str]] = [
    ("es04", "data_hot", "Upgrade ES es04 (data_hot/ingest/transform)"),
    ("es03", "data_hot_content", "Upgrade ES es03 (data_content + data_hot)"),
    ("es01", "data_content", "Upgrade ES es01 (data_content)"),
    ("es02", "data_content_master", "Upgrade ES es02 (data_content, often elected master)"),
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


def cat_nodes(es, auth: str) -> list[dict]:
    out = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?h=name,version,master,node.role&format=json'",
        check=False,
    )
    try:
        rows = json.loads(out.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return []
    return rows if isinstance(rows, list) else []


def all_es_on_version(es, auth: str, version: str) -> bool:
    rows = cat_nodes(es, auth)
    if len(rows) != EXPECTED_ES_NODES:
        return False
    return all(r.get("version") == version for r in rows)


def node_already_on_version(es, auth: str, fqdn: str, version: str) -> bool:
    short = fqdn.split(".")[0]
    for r in cat_nodes(es, auth):
        name = r.get("name") or ""
        if (short in name or fqdn in name) and r.get("version") == version:
            return True
    return False


def es_keys_master_last(es, auth: str) -> list[tuple[str, str, str]]:
    """Non-master first; current master last. Includes es04 when present."""
    labels = {k: (tier, label) for k, tier, label in ES_UPGRADE_ORDER}
    keys = [k for k, _, _ in ES_UPGRADE_ORDER if k in NODES]
    for k in NODES:
        if k.startswith("es") and k not in keys:
            keys.insert(0, k)
            labels.setdefault(k, (k, f"Upgrade ES {k}"))

    master_key = None
    for r in cat_nodes(es, auth):
        if r.get("master") != "*":
            continue
        name = (r.get("name") or "").lower()
        for k, (_, fqdn) in NODES.items():
            if not k.startswith("es"):
                continue
            if fqdn.split(".")[0].lower() in name or fqdn.lower() in name:
                master_key = k
                break
    if master_key and master_key in keys:
        keys = [k for k in keys if k != master_key] + [master_key]
    return [(k, *labels.get(k, (k, f"Upgrade ES {k}"))) for k in keys]


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
    order = es_keys_master_last(es_primary, auth)
    print(f"  order: {[k for k, _, _ in order]}", flush=True)

    for key, _tier, label in order:
        ip, fqdn = NODES[key]
        if node_already_on_version(es_primary, auth, fqdn, version):
            print(f"SKIP {label}: already on {version}", flush=True)
            continue
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
    print(f"OK all {EXPECTED_ES_NODES} ES nodes on {version}", flush=True)


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rolling-upgrade ism-elk-cluster")
    p.add_argument(
        "--to",
        choices=(INTERMEDIATE_VERSION, TARGET_VERSION),
        default=INTERMEDIATE_VERSION,
        help=f"Stop at this version (default {INTERMEDIATE_VERSION}; do not jump 8.18 -> 9.x)",
    )
    p.add_argument("--skip-es", action="store_true")
    p.add_argument("--skip-kibana", action="store_true")
    p.add_argument("--skip-agents", action="store_true")
    return p.parse_args(argv)


def require_819_before_9(es, auth: str, target: str) -> None:
    if not target.startswith("9."):
        return
    rows = cat_nodes(es, auth)
    bad = [r for r in rows if not str(r.get("version", "")).startswith("8.19")]
    if len(rows) != EXPECTED_ES_NODES or bad:
        vers = {r.get("name"): r.get("version") for r in rows}
        raise RuntimeError(
            f"9.x upgrade requires all {EXPECTED_ES_NODES} ES nodes on 8.19.x first; "
            f"seen={vers}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target = args.to
    print("=== Pre-upgrade cluster config snapshot ===", flush=True)
    scan_cluster_config()

    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    print(run(es, f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'", check=False))
    require_819_before_9(es, auth, target)
    es.close()

    print(f"\nHyper-V checkpoint suggested: {SNAPSHOT_NAME} (all 6 VMs)", flush=True)
    print(
        f"Upgrade path: {BASELINE_VERSION} -> {INTERMEDIATE_VERSION} "
        f"(required for 9.1+) -> {TARGET_VERSION}",
        flush=True,
    )
    print(f"This run --to {target}", flush=True)

    if not args.skip_es:
        upgrade_es_cluster(target, elastic_pwd)
    else:
        print("SKIP Elasticsearch", flush=True)

    if not args.skip_kibana:
        upgrade_kibana(target)
    else:
        print("SKIP Kibana", flush=True)

    if not args.skip_agents:
        if not upgrade_fleet_managed_agents(target, elastic_pwd):
            print("WARN: agent upgrade incomplete", flush=True)
            return 1
    else:
        print("SKIP agents", flush=True)

    print("\n=== Post-upgrade verification ===", flush=True)
    issues = scan_cluster_config()
    if not verify_final(elastic_pwd):
        print("WARN: Kibana verification incomplete", flush=True)
        return 1
    if issues:
        print(f"WARN: cluster_config_snapshot issues={issues}", flush=True)
    print(f"\nSUCCESS: stack upgraded to {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())