#!/usr/bin/env python3
"""
Apply cluster health fixes: ES yml bootstrap, Kibana air-gap telemetry, integration streams,
agent restarts, and re-run health review.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys

from apply_node_integrations import main as apply_integrations
from cluster_health_review import main as run_health_review
from deploy_ordered_stack import (
    ES_NODES,
    ES_PRIMARY_IP,
    NODES,
    REMOTE,
    SCRIPTS,
    connect,
    copy_scripts,
    curl_elastic_auth,
    get_elastic_password,
    run,
    wait_kibana_stable,
)
from monitoring_credentials import ensure_monitoring_user

ROOT = SCRIPTS.parent


def fix_es_bootstrap_on_all(elastic_pwd: str) -> None:
    print("=== Fix elasticsearch.yml bootstrap stanzas ===", flush=True)
    for ip, fqdn in ES_NODES:
        c = connect(ip)
        copy_scripts(c, roles=("elasticsearch",))
        out = run(c, f"bash {REMOTE}/fix-es-yml-bootstrap.sh", timeout=300, check=False)
        print(f"  {fqdn}: {out.strip()[-400:]}", flush=True)
        c.close()


def fix_kibana_airgap(elastic_pwd: str) -> None:
    print("=== Fix Kibana air-gap telemetry noise ===", flush=True)
    kb_ip = NODES["kibana"][0]
    c = connect(kb_ip)
    copy_scripts(c, roles=("kibana",))
    run(c, f"bash {REMOTE}/fix-kibana-airgap-telemetry.sh", timeout=600, check=False)
    c.close()
    wait_kibana_stable(kb_ip, elastic_pwd=elastic_pwd)


def integrations_have_streams(kb, elastic_pwd: str) -> bool:
    data = json.loads(
        run(
            kb,
            f"curl -s -u {curl_elastic_auth(elastic_pwd)} -H 'kbn-xsrf:true' "
            f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=100'",
            check=False,
        )
    )
    for pkg in data.get("items", []):
        name = pkg.get("package", {}).get("name")
        if name not in ("system", "elasticsearch", "kibana"):
            continue
        for inp in pkg.get("inputs", []):
            streams = inp.get("streams") or []
            if not streams:
                print(f"  empty streams: {pkg.get('name')} input={inp.get('type')}", flush=True)
                return False
    return True


def download_synthetics_package() -> None:
    print("=== Ensure synthetics EPR package staged ===", flush=True)
    subprocess.run([sys.executable, str(ROOT / "download_epr_packages.py")], check=False)


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    ensure_monitoring_user(es, run, elastic_pwd)
    es.close()

    download_synthetics_package()
    fix_es_bootstrap_on_all(elastic_pwd)
    fix_kibana_airgap(elastic_pwd)

    kb = connect(NODES["kibana"][0])
    needs_integrations = not integrations_have_streams(kb, elastic_pwd)
    kb.close()

    if needs_integrations:
        print("=== Re-apply node integrations (populate metric/log streams) ===", flush=True)
        rc = apply_integrations()
        if rc != 0:
            print("apply_node_integrations returned non-zero", flush=True)
    else:
        print("=== Integration streams already populated ===", flush=True)

    print("\n=== Post-fix health review ===", flush=True)
    issues = run_health_review()
    return 0 if issues == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())