#!/usr/bin/env python3
"""
Preflight for 8.19.18 -> 9.5.3. Does not perform the upgrade.

Checks:
  - offline packages for 9.5.3 exist locally
  - all ES nodes are on 8.19.x (8.19.18 preferred)
  - Kibana RPM is 8.19.x
  - deprecation API (_migration/deprecations)
  - NFS snapshot repo is present
Prints Hyper-V + Upgrade Assistant next steps.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent_artifact_upgrade import fleet_agent_binary_version
from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run
from restore_elastic_vms import remote_rpm_version
from upgrade_elastic_stack import EXPECTED_ES_NODES, INTERMEDIATE_VERSION, TARGET_VERSION

ROOT = Path(__file__).parent
PKG = ROOT / "packages"
NEED = [
    f"elasticsearch-{TARGET_VERSION}-x86_64.rpm",
    f"kibana-{TARGET_VERSION}-x86_64.rpm",
    f"elastic-agent-{TARGET_VERSION}-linux-x86_64.tar.gz",
    f"elastic-agent-{TARGET_VERSION}-linux-x86_64.tar.gz.sha512",
]


def _json_tail(out: str):
    start = out.rfind("{")
    if start < 0:
        start = out.rfind("[")
    if start < 0:
        return None
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return None


def main() -> int:
    issues: list[str] = []
    print(f"=== Prepare {INTERMEDIATE_VERSION} -> {TARGET_VERSION} ===", flush=True)

    print("\n-- packages --", flush=True)
    for name in NEED:
        path = PKG / name
        ok = path.is_file() and path.stat().st_size > 100
        print(f"  {'OK' if ok else 'MISSING'} {name}", flush=True)
        if not ok:
            issues.append(f"missing package {name}")

    es = connect(NODES["es01"][0])
    auth = curl_elastic_auth(get_elastic_password(es))
    nodes_out = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?h=name,version,master,node.role&format=json'",
        check=False,
    )
    rows = _json_tail(nodes_out) or []
    print("\n-- elasticsearch --", flush=True)
    if not isinstance(rows, list) or len(rows) != EXPECTED_ES_NODES:
        issues.append(f"expected {EXPECTED_ES_NODES} ES nodes, saw {rows}")
    for r in rows if isinstance(rows, list) else []:
        ver = r.get("version")
        name = r.get("name")
        print(f"  {name} {ver} master={r.get('master')} roles={r.get('node.role')}", flush=True)
        if not str(ver).startswith("8.19"):
            issues.append(f"{name} is {ver}, need 8.19.x")
        elif ver != INTERMEDIATE_VERSION:
            issues.append(f"{name} is {ver}, prefer {INTERMEDIATE_VERSION} before 9.5.3")

    dep = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_migration/deprecations?pretty'",
        check=False,
        timeout=120,
    )
    print("\n-- deprecations (truncated) --", flush=True)
    print(dep[-4000:] if len(dep) > 4000 else dep, flush=True)
    dep_json = _json_tail(dep) or {}
    if isinstance(dep_json, dict):
        for section, items in dep_json.items():
            if isinstance(items, list) and items:
                crit = [i for i in items if str(i.get("level", "")).lower() == "critical"]
                print(f"  {section}: {len(items)} issue(s), {len(crit)} critical", flush=True)
                if crit:
                    issues.append(f"critical deprecations in {section}: {len(crit)}")

    repo = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_snapshot/fs_nfs_snapshots'",
        check=False,
    )
    if "fs_nfs_snapshots" not in repo and '"type"' not in repo:
        issues.append("snapshot repo fs_nfs_snapshots not found")
    else:
        print("\n-- snapshot repo fs_nfs_snapshots present --", flush=True)
    es.close()

    kb_ver = remote_rpm_version(NODES["kibana"][0], "kibana")
    fleet_ver = fleet_agent_binary_version(NODES["fleet"][0])
    print(f"\n-- kibana RPM: {kb_ver}", flush=True)
    print(f"-- fleet agent binary: {fleet_ver}", flush=True)
    if not str(kb_ver).startswith("8.19"):
        issues.append(f"Kibana is {kb_ver}, upgrade to {INTERMEDIATE_VERSION} first")
    if not str(fleet_ver).startswith("8.19"):
        issues.append(f"Fleet agent is {fleet_ver}, upgrade to {INTERMEDIATE_VERSION} first")

    print("\n-- next steps (not executed) --", flush=True)
    print("  1. Fix any CRITICAL Upgrade Assistant / deprecation issues in Kibana 8.19.", flush=True)
    print("  2. python create_named_snapshot.py --name pre-upgrade-to-9.5.3", flush=True)
    print("  3. .\\Checkpoint-AllStackVMs.ps1 -SnapshotName pre-upgrade-to-9.5.3", flush=True)
    print("  4. python upgrade_elastic_stack.py --to 9.5.3", flush=True)
    print("     (ES rolling, then Kibana, then Fleet bulk_upgrade agents)", flush=True)

    if issues:
        print("\nPREFLIGHT ISSUES:", flush=True)
        for i in issues:
            print(f"  - {i}", flush=True)
        return 1
    print("\nPREFLIGHT OK: ready to snapshot and upgrade to 9.5.3", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
