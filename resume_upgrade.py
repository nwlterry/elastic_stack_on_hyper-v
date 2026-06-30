#!/usr/bin/env python3
"""Resume stack upgrade after partial rolling ES upgrade."""
from __future__ import annotations

import shlex

from deploy_ordered_stack import NODES, REMOTE, connect, curl_elastic_auth, get_elastic_password, run
from agent_artifact_upgrade import upgrade_fleet_managed_agents
from upgrade_elastic_stack import (
    ES_UPGRADE_ORDER,
    INTERMEDIATE_VERSION,
    TARGET_VERSION,
    all_es_on_version,
    stage_packages,
    upgrade_es_cluster,
    upgrade_kibana,
    verify_final,
    wait_cluster_green,
)
from scan_cluster_config import main as scan_cluster_config

SKIP_UNTIL_AFTER = "es03"  # already on 8.19.9


def upgrade_es_cluster_resume(version: str, elastic_pwd: str) -> None:
    auth = curl_elastic_auth(elastic_pwd)
    es_primary = connect(NODES["es01"][0])
    for key, tier, label in ES_UPGRADE_ORDER:
        if key == SKIP_UNTIL_AFTER:
            print(f"SKIP {label} (already upgraded)", flush=True)
            continue
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
            raise RuntimeError(f"Cluster not healthy after {fqdn}")
    es_primary.close()
    es_primary = connect(NODES["es01"][0])
    if not all_es_on_version(es_primary, auth, version):
        raise RuntimeError(run(es_primary, f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'", check=False))
    es_primary.close()
    print(f"OK all ES nodes on {version}", flush=True)


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    print(run(es, f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'", check=False))
    es.close()

    es_chk = connect(NODES["es01"][0])
    need_intermediate = not all_es_on_version(es_chk, auth, INTERMEDIATE_VERSION)
    es_chk.close()
    if need_intermediate:
        upgrade_es_cluster_resume(INTERMEDIATE_VERSION, elastic_pwd)
    else:
        print(f"All ES nodes already on {INTERMEDIATE_VERSION}", flush=True)

    upgrade_es_cluster(TARGET_VERSION, elastic_pwd)
    upgrade_kibana(TARGET_VERSION)
    if not upgrade_fleet_managed_agents(TARGET_VERSION, elastic_pwd):
        print("WARN: agent upgrade incomplete", flush=True)
        return 1

    scan_cluster_config()
    ok = verify_final(elastic_pwd)
    print(f"\nSUCCESS: stack upgraded to {TARGET_VERSION}" if ok else "\nWARN: verify incomplete", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())