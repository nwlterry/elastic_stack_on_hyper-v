#!/usr/bin/env python3
"""Restore all ELK Hyper-V VMs from a named checkpoint."""
from __future__ import annotations

import shlex
import time

from deploy_ordered_stack import VM_NAMES, connect, ps, run
from upgrade_elastic_stack import SNAPSHOT_NAME

# VM_NAMES is role-keyed (es01, kibana, fleet); use Hyper-V VMName values.
ALL_VMS = list(VM_NAMES.values())

# Start order after restore: ES cluster first, then Kibana, then Fleet.
RESTORE_START_ORDER = (
    VM_NAMES["es01"],
    VM_NAMES["es02"],
    VM_NAMES["es03"],
    VM_NAMES["kibana"],
    VM_NAMES["fleet"],
)


def snapshot_exists(vm_name: str, snapshot_name: str) -> bool:
    snap = ps(
        f"(Get-VMSnapshot -VMName {shlex.quote(vm_name)} "
        f"-Name {shlex.quote(snapshot_name)} -ErrorAction SilentlyContinue).Name"
    )
    return snapshot_name in snap


def restore_all_vms(snapshot_name: str = SNAPSHOT_NAME) -> None:
    """Stop, restore, and restart all stack VMs from a Hyper-V checkpoint."""
    print(f"\n=== Restore all VMs from snapshot: {snapshot_name} ===", flush=True)
    missing = [vm for vm in ALL_VMS if not snapshot_exists(vm, snapshot_name)]
    if missing:
        raise RuntimeError(
            f"Snapshot {snapshot_name!r} missing on: {', '.join(missing)}"
        )

    for vm in ALL_VMS:
        print(f"  stop {vm}", flush=True)
        ps(f"Stop-VM -Name {shlex.quote(vm)} -Force -ErrorAction SilentlyContinue")
    time.sleep(10)

    for vm in ALL_VMS:
        print(f"  restore {vm} <- {snapshot_name}", flush=True)
        ps(
            f"Restore-VMSnapshot -VMName {shlex.quote(vm)} "
            f"-Name {shlex.quote(snapshot_name)} -Confirm:$false"
        )

    for vm in RESTORE_START_ORDER:
        print(f"  start {vm}", flush=True)
        ps(f"Start-VM -Name {shlex.quote(vm)}")
        time.sleep(5)

    print("Waiting for SSH on ES primary...", flush=True)
    connect("10.44.40.31", attempts=60).close()
    print(f"Restored {len(ALL_VMS)} VMs from {snapshot_name}", flush=True)


def wait_for_es_api(ip: str, timeout: int = 600) -> None:
    """Wait until Elasticsearch HTTPS API responds on a node."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = connect(ip, attempts=3)
            code = (
                run(
                    c,
                    "curl -sk -o /dev/null -w '%{http_code}' "
                    "https://localhost:9200/ 2>/dev/null",
                    check=False,
                    timeout=15,
                )
                .strip()
                .splitlines()[-1]
            )
            c.close()
            if code and code != "000":
                print(f"  ES API up on {ip} (http={code})", flush=True)
                return
        except (TimeoutError, OSError):
            pass
        print(f"  waiting for ES API on {ip}...", flush=True)
        time.sleep(10)
    raise RuntimeError(f"Elasticsearch API not ready on {ip} after {timeout}s")


def wait_es_cluster_ready(elastic_pwd: str, timeout: int = 900) -> bool:
    from upgrade_elastic_stack import cluster_health, wait_cluster_green
    from deploy_ordered_stack import NODES, curl_elastic_auth

    auth = curl_elastic_auth(elastic_pwd)
    es = connect(NODES["es01"][0], attempts=60)
    ok = wait_cluster_green(es, auth, timeout=timeout)
    health = cluster_health(es, auth)
    print(
        f"  cluster status={health.get('status')} nodes={health.get('number_of_nodes')}",
        flush=True,
    )
    es.close()
    return ok


def remote_rpm_version(ip: str, package: str) -> str:
    c = connect(ip, attempts=40)
    ver = (
        run(
            c,
            f"rpm -q {shlex.quote(package)} 2>/dev/null | "
            f"grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1 || echo missing",
            check=False,
        )
        .strip()
        .splitlines()[-1]
    )
    c.close()
    return ver


def main() -> int:
    restore_all_vms()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())