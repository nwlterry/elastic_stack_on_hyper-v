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

import os

from apply_dashboard_patch import main as apply_dashboard_patch
from scan_cluster_config import main as scan_cluster_config
from apply_node_integrations import main as apply_integrations, restart_agents_on_nodes
from cluster_health_review import main as run_health_review
from fix_dashboard_search import fix_monitoring_ui_creds
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
    auth_env = shlex.quote(elastic_pwd)
    for ip, fqdn in ES_NODES:
        c = connect(ip)
        copy_scripts(c, roles=("elasticsearch",))
        out = run(
            c,
            f"ELASTIC_PASS={auth_env} bash {REMOTE}/fix-es-yml-bootstrap.sh",
            timeout=300,
            check=False,
        )
        print(f"  {fqdn}: {out.strip()[-400:]}", flush=True)
        c.close()


def fix_kibana_airgap(elastic_pwd: str) -> bool:
    print("=== Fix Kibana air-gap telemetry noise ===", flush=True)
    kb_ip = NODES["kibana"][0]
    c = connect(kb_ip)
    copy_scripts(c, roles=("kibana",))
    out = run(c, f"bash {REMOTE}/fix-kibana-airgap-telemetry.sh", timeout=600, check=False)
    print(out.strip()[-500:], flush=True)
    c.close()
    if "skip_restart" in out:
        return True
    if not wait_kibana_stable(kb_ip, elastic_pwd=elastic_pwd, max_attempts=90):
        print("ERROR: Kibana did not stabilize after telemetry fix", flush=True)
        return False
    return True


def integrations_have_streams(kb, elastic_pwd: str) -> bool:
    raw = run(
        kb,
        f"curl -s -u {curl_elastic_auth(elastic_pwd)} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=100'",
        check=False,
    )
    if not raw or not raw.strip():
        print("  Fleet API returned empty response — assuming streams need check", flush=True)
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("  Fleet API returned non-JSON — Kibana may still be starting", flush=True)
        return False
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
    _user, monitoring_pwd = ensure_monitoring_user(es, run, elastic_pwd)
    es.close()

    download_synthetics_package()
    fix_es_bootstrap_on_all(elastic_pwd)
    if not fix_kibana_airgap(elastic_pwd):
        return 1

    kb = connect(NODES["kibana"][0])
    if not wait_kibana_stable(NODES["kibana"][0], elastic_pwd=elastic_pwd, max_attempts=12):
        kb.close()
        print("ERROR: Kibana not ready for Fleet API checks", flush=True)
        return 1
    needs_integrations = not integrations_have_streams(kb, elastic_pwd)
    kb.close()

    if needs_integrations:
        print("=== Re-apply node integrations (populate metric/log streams) ===", flush=True)
        rc = apply_integrations()
        if rc != 0:
            print("apply_node_integrations returned non-zero", flush=True)
    else:
        print("=== Integration streams already populated ===", flush=True)

    print("=== Fix dashboard search (monitoring UI creds + Lens patches) ===", flush=True)
    kb = connect(NODES["kibana"][0])
    fix_monitoring_ui_creds(kb, monitoring_pwd)
    kb.close()
    if not wait_kibana_stable(NODES["kibana"][0], elastic_pwd=elastic_pwd, max_attempts=30):
        print("ERROR: Kibana not stable after monitoring UI config", flush=True)
        return 1
    os.environ.setdefault("SKIP_FLEET_REINSTALL", "1")
    if apply_dashboard_patch() != 0:
        print("ERROR: apply_dashboard_patch failed", flush=True)
        return 1

    print("=== Restart elastic-agents (refresh Fleet/ES connections) ===", flush=True)
    restart_agents_on_nodes(elastic_pwd)

    print("\n=== Post-fix cluster config scan ===", flush=True)
    scan_issues = scan_cluster_config()

    print("\n=== Post-fix health review ===", flush=True)
    issues = run_health_review()
    return 0 if issues == 0 and scan_issues == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())