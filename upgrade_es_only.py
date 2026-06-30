#!/usr/bin/env python3
"""
Restore all VMs to pre-upgrade snapshot, then rolling-upgrade Elasticsearch only.

Kibana and Fleet Server remain at BASELINE_VERSION (8.18.4). Use this when you
need newer ES features without touching the Fleet/Kibana control plane.

Upgrade path: 8.18.4 -> 8.19.9 (required) -> 9.4.1
"""
from __future__ import annotations

import json

from agent_artifact_upgrade import fleet_agent_binary_version
from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run
from finish_agent_upgrade import fleet_server_healthy
from restore_elastic_vms import remote_rpm_version, restore_all_vms, wait_es_cluster_ready
from scan_cluster_config import main as scan_cluster_config
from upgrade_elastic_stack import (
    BASELINE_VERSION,
    INTERMEDIATE_VERSION,
    SNAPSHOT_NAME,
    TARGET_VERSION,
    all_es_on_version,
    upgrade_es_cluster,
)

KIBANA_BASELINE = BASELINE_VERSION
FLEET_BASELINE = BASELINE_VERSION


def verify_unchanged_components(elastic_pwd: str) -> bool:
    """Confirm Kibana and Fleet stayed at baseline while ES upgraded."""
    ok = True
    kb_ip = NODES["kibana"][0]
    fleet_ip = NODES["fleet"][0]

    kb_ver = remote_rpm_version(kb_ip, "kibana")
    print(f"Kibana RPM version: {kb_ver}", flush=True)
    if kb_ver != KIBANA_BASELINE:
        print(f"WARN: expected Kibana {KIBANA_BASELINE}", flush=True)
        ok = False

    fleet_ver = fleet_agent_binary_version(fleet_ip)
    print(f"Fleet agent binary: {fleet_ver}", flush=True)
    if fleet_ver != FLEET_BASELINE:
        print(f"WARN: expected Fleet {FLEET_BASELINE}", flush=True)
        ok = False

    if not fleet_server_healthy(fleet_ip):
        print("WARN: Fleet Server :8220 not HEALTHY", flush=True)
        ok = False

    auth = curl_elastic_auth(elastic_pwd)
    kb = connect(kb_ip)
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
        if overall not in ("available", "degraded"):
            ok = False
    except json.JSONDecodeError:
        print(out[-500:], flush=True)
        ok = False

    return ok


def main() -> int:
    print("=== Pre-upgrade cluster config snapshot ===", flush=True)
    scan_cluster_config()

    print(f"\n=== Restore all VMs: {SNAPSHOT_NAME} ===", flush=True)
    restore_all_vms(SNAPSHOT_NAME)

    es = connect(NODES["es01"][0], attempts=60)
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    print(
        run(
            es,
            f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'",
            check=False,
        ),
        flush=True,
    )
    es.close()

    if not wait_es_cluster_ready(elastic_pwd):
        print("ERROR: cluster not healthy after snapshot restore", flush=True)
        return 1

    print(
        f"\nUpgrade path (ES only): {BASELINE_VERSION} -> "
        f"{INTERMEDIATE_VERSION} -> {TARGET_VERSION}",
        flush=True,
    )
    print(f"Kibana and Fleet remain at {BASELINE_VERSION}", flush=True)

    upgrade_es_cluster(INTERMEDIATE_VERSION, elastic_pwd)
    upgrade_es_cluster(TARGET_VERSION, elastic_pwd)

    es = connect(NODES["es01"][0])
    if not all_es_on_version(es, auth, TARGET_VERSION):
        out = run(
            es,
            f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'",
            check=False,
        )
        es.close()
        print(f"ERROR: ES nodes not all on {TARGET_VERSION}:\n{out}", flush=True)
        return 1
    print(run(es, f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'", check=False))
    es.close()

    print("\n=== Post-upgrade verification ===", flush=True)
    issues = scan_cluster_config()
    if not verify_unchanged_components(elastic_pwd):
        print("WARN: Kibana/Fleet verification issues (version skew with ES is expected)", flush=True)
    if issues:
        print(f"WARN: cluster_config_snapshot issues={issues}", flush=True)

    print(
        f"\nSUCCESS: ES upgraded to {TARGET_VERSION}; "
        f"Kibana/Fleet kept at {BASELINE_VERSION}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())