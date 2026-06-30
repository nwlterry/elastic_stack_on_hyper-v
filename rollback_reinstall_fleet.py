#!/usr/bin/env python3
"""
Rollback Fleet Server VM, unenroll stale Fleet agent records, reinstall Fleet Server,
then upgrade all Fleet-managed agents via the Elastic artifacts mirror.

Prefer rollback_upgrade_fleet.py for artifact upgrade without reinstall.
"""
from __future__ import annotations

import shlex
import time

from agent_artifact_upgrade import (
    list_agents,
    unenroll_hostname_agents,
    upgrade_fleet_managed_agents,
)
from deploy_ordered_stack import (
    AGENT_CLEANUP,
    FLEET_VM,
    NODES,
    REMOTE,
    connect,
    create_service_token,
    curl_elastic_auth,
    ensure_vm_running,
    get_elastic_password,
    install_es_ca_on_node,
    ps,
    run,
    set_fleet_vm_memory,
    wait_fleet_server_ready,
)
from finish_agent_upgrade import fleet_server_healthy, verify_agents
from upgrade_elastic_stack import SNAPSHOT_NAME, TARGET_VERSION, stage_packages

FLEET_POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
FLEET_FQDN = NODES["fleet"][1]


def restore_fleet_vm_snapshot(snapshot_name: str = SNAPSHOT_NAME) -> None:
    print(f"\n=== Restore Fleet VM snapshot: {snapshot_name} ===", flush=True)
    snap = ps(
        f"(Get-VMSnapshot -VMName {FLEET_VM} -Name {shlex.quote(snapshot_name)} "
        f"-ErrorAction SilentlyContinue).Name"
    )
    if snapshot_name not in snap:
        raise RuntimeError(f"Snapshot {snapshot_name} not found on {FLEET_VM}: {snap!r}")
    ps(f"Stop-VM -Name {FLEET_VM} -Force -ErrorAction SilentlyContinue")
    time.sleep(8)
    ps(
        f"Restore-VMSnapshot -VMName {FLEET_VM} -Name {shlex.quote(snapshot_name)} -Confirm:$false"
    )
    ps(f"Start-VM -Name {FLEET_VM}")
    print(f"Restored {FLEET_VM} from {snapshot_name}", flush=True)


def reinstall_fleet_server(version: str, elastic_pwd: str) -> None:
    print(f"\n=== Reinstall Fleet Server @ {version} ===", flush=True)
    set_fleet_vm_memory()
    ensure_vm_running(FLEET_VM, 60)
    svc_token, ca = create_service_token(elastic_pwd)
    ip, es_fqdn = NODES["fleet"][0], NODES["es01"][1]
    c = connect(ip, attempts=60)
    stage_packages(c, roles=("elastic-agent",), versions=(version,))
    run(c, AGENT_CLEANUP, check=False, timeout=300)
    install_es_ca_on_node(c, ca)
    run(
        c,
        f"nohup env FLEET_MEMORY_MAX=8G FLEET_MEMORY_HIGH=6G bash {REMOTE}/install-fleet-server.sh "
        f"--version {shlex.quote(version)} --es-host {es_fqdn} "
        f"--ca-file {REMOTE}/certs/http_ca.crt "
        f"--service-token {shlex.quote(svc_token)} "
        f"--policy-id {shlex.quote(FLEET_POLICY_ID)} "
        f"> /var/log/fleet-install.log 2>&1 &",
        timeout=30,
    )
    c.close()
    if not wait_fleet_server_ready(ip):
        raise RuntimeError("Fleet Server reinstall did not become healthy")
    print("Fleet Server reinstalled and healthy", flush=True)


def verify_no_fleet_ghosts(kb, elastic_pwd: str) -> bool:
    agents = list_agents(kb, elastic_pwd)
    fleet_records = [
        a
        for a in agents
        if a.get("local_metadata", {}).get("host", {}).get("hostname") == FLEET_FQDN
        and a.get("status") != "unenrolled"
    ]
    print("\n=== Fleet hostname agent records ===", flush=True)
    for a in fleet_records:
        print(
            f"  {a.get('local_metadata', {}).get('host', {}).get('hostname')} "
            f"{a.get('agent', {}).get('version')} {a.get('status')} id={a.get('id')}",
            flush=True,
        )
    offline = [a for a in fleet_records if a.get("status") == "offline"]
    if len(fleet_records) != 1 or offline:
        print(f"WARN: expected 1 online Fleet record, got {len(fleet_records)} (offline={len(offline)})", flush=True)
        return False
    if fleet_records[0].get("agent", {}).get("version") != TARGET_VERSION:
        return False
    return fleet_records[0].get("status") == "online"


def main() -> int:
    version = TARGET_VERSION
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    es.close()

    kb = connect(NODES["kibana"][0])
    print(f"\n=== Unenroll all Fleet hostname agents ({FLEET_FQDN}) ===", flush=True)
    unenroll_hostname_agents(kb, elastic_pwd, FLEET_FQDN)
    time.sleep(10)
    kb.close()

    restore_fleet_vm_snapshot()
    ensure_vm_running(FLEET_VM, 60)

    reinstall_fleet_server(version, elastic_pwd)

    fleet_ip = NODES["fleet"][0]
    if not fleet_server_healthy(fleet_ip):
        raise RuntimeError("Fleet Server not healthy after reinstall")

    ok = upgrade_fleet_managed_agents(version, elastic_pwd)

    kb = connect(NODES["kibana"][0])
    ghosts_ok = verify_no_fleet_ghosts(kb, elastic_pwd)
    kb.close()

    print(f"\n=== Verify agents @ {version} ===", flush=True)
    verified = verify_agents(version, elastic_pwd)
    success = ok and verified and ghosts_ok
    print(f"\n{'SUCCESS' if success else 'WARN'}: Fleet rollback + agent upgrade", flush=True)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())