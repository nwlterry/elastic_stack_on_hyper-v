#!/usr/bin/env python3
"""
Rollback Fleet Server VM to pre-upgrade snapshot, remove offline Fleet API ghosts,
upgrade via Fleet bulk_upgrade + Elastic artifacts mirror (not rollback_reinstall_fleet.py).

Fleet Server cannot use local ``elastic-agent upgrade`` (fleet-managed block) and
Fleet blocks cross-major bulk_upgrade while the Fleet Server binary lags
(e.g. 8.18.4/8.19.9 cannot bulk_upgrade to 9.4.1). Stepped artifact path:
  enroll @ 8.18.4 -> 8.19.9 (bulk_upgrade) -> 9.4.1 (archive enroll) -> bulk_upgrade agents.

Enrollment uses install-fleet-server.sh with local archives only for bootstrap/cross-major;
version bumps use bulk_upgrade from http://10.44.40.42:8081/downloads/.
"""
from __future__ import annotations

import shlex
import time

from agent_artifact_upgrade import (
    agents_for_hostname,
    bulk_upgrade_agents,
    ensure_agent_download_source,
    fleet_agent_binary_version,
    list_agents,
    prestage_verification_files,
    reenroll_fleet_server_inplace,
    restart_agents,
    setup_artifact_server,
    unenroll_agents,
    unenroll_hostname_agents,
    wait_agents_upgraded,
    wait_fleet_hostname_agent,
)
from deploy_ordered_stack import (
    FLEET_VM,
    NODES,
    connect,
    ensure_vm_running,
    get_elastic_password,
    ps,
    run,
    wait_fleet_server_ready,
)
from finish_agent_upgrade import fleet_server_healthy, verify_agents
from upgrade_elastic_stack import INTERMEDIATE_VERSION, SNAPSHOT_NAME, TARGET_VERSION, stage_packages

FLEET_POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
FLEET_FQDN = NODES["fleet"][1]
ROLLBACK_AGENT_VERSION = "8.18.4"


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


def unenroll_fleet_ghosts(kb, elastic_pwd: str) -> list[str]:
    """
    Remove duplicate Fleet records only. Never unenroll the sole record — snapshot
    enrollment may need time to check in after VM restore.
    """
    agents = list_agents(kb, elastic_pwd)
    matches = [
        a
        for a in agents_for_hostname(agents, FLEET_FQDN)
        if a.get("status") != "unenrolled"
    ]
    to_remove: list[str] = []
    online = [a for a in matches if a.get("status") == "online"]

    if len(matches) <= 1:
        if matches:
            a = matches[0]
            print(
                f"  keep sole Fleet record id={a['id']} "
                f"{a.get('agent', {}).get('version', '?')} {a.get('status')}",
                flush=True,
            )
        return to_remove

    for a in matches:
        if a.get("status") == "offline":
            print(
                f"  unenroll offline ghost id={a['id']} "
                f"{a.get('agent', {}).get('version', '?')}",
                flush=True,
            )
            to_remove.append(a["id"])

    if len(online) > 1:
        online.sort(key=lambda a: a.get("last_checkin_status", "") or "", reverse=True)
        for a in online[1:]:
            print(
                f"  unenroll duplicate online id={a['id']} "
                f"{a.get('agent', {}).get('version', '?')}",
                flush=True,
            )
            to_remove.append(a["id"])

    unenroll_agents(kb, elastic_pwd, to_remove)
    return to_remove


def ensure_fleet_agent_enrolled(
    kb,
    elastic_pwd: str,
    *,
    enroll_if_missing: bool = True,
) -> dict:
    """Wait for Fleet agent in API; enroll from local archive when missing after rollback."""
    fleet_ip = NODES["fleet"][0]
    ensure_vm_running(FLEET_VM, 30)
    connect(fleet_ip, attempts=30).close()

    agent = wait_fleet_hostname_agent(
        kb, elastic_pwd, FLEET_FQDN, timeout=120, require_online=True
    )
    if agent:
        return agent

    if not enroll_if_missing:
        raise RuntimeError("Fleet agent not online in Fleet API")

    binary_ver = fleet_agent_binary_version(fleet_ip)
    print(f"No Fleet API record — enrolling @ {binary_ver}", flush=True)
    if not reenroll_fleet_server_inplace(
        elastic_pwd,
        policy_id=FLEET_POLICY_ID,
        agent_version=binary_ver,
        fleet_ip=fleet_ip,
    ):
        raise RuntimeError("Fleet Server enrollment failed")

    agent = wait_fleet_hostname_agent(kb, elastic_pwd, FLEET_FQDN, timeout=900)
    if not agent:
        raise RuntimeError("Fleet Server agent did not appear in Fleet API after enroll")
    return agent


def wait_fleet_agent_version(
    kb,
    elastic_pwd: str,
    version: str,
    *,
    timeout: int = 3600,
) -> bool:
    """Poll Fleet hostname agent until it reports target version and is online."""
    fleet_ip = NODES["fleet"][0]
    deadline = time.time() + timeout
    while time.time() < deadline:
        agent = wait_fleet_hostname_agent(
            kb, elastic_pwd, FLEET_FQDN, timeout=30, require_online=False
        )
        api_ver = agent.get("agent", {}).get("version", "?") if agent else "?"
        binary_ver = fleet_agent_binary_version(fleet_ip)
        status = agent.get("status", "?") if agent else "missing"
        print(f"  Fleet upgrade poll: api={api_ver} binary={binary_ver} status={status}", flush=True)
        if (
            agent
            and agent.get("status") in ("online", "degraded")
            and api_ver == version
            and binary_ver == version
        ):
            return True
        time.sleep(30)
    return False


def upgrade_fleet_server_via_artifacts(
    kb,
    elastic_pwd: str,
    version: str,
    source_uri: str,
    fleet_agent_id: str,
) -> bool:
    print(f"\n=== Fleet Server bulk_upgrade -> {version} (Elastic artifacts) ===", flush=True)
    print(f"Pre-staging {version} verification files on fleet node...", flush=True)
    prestage_verification_files(version)
    result = bulk_upgrade_agents(
        kb, elastic_pwd, version, source_uri, agent_ids=[fleet_agent_id]
    )
    if result:
        print(f"Fleet action: {result}", flush=True)
    fleet_ip = NODES["fleet"][0]
    ok = wait_fleet_agent_version(kb, elastic_pwd, version)
    if ok and not fleet_server_healthy(fleet_ip):
        print("WARN: Fleet Server not healthy after bulk_upgrade", flush=True)
        return False
    return ok


def enroll_fleet_server_at_version(
    kb,
    elastic_pwd: str,
    version: str,
) -> bool:
    """
    Replace Fleet Server binary via local archive enroll. Fleet Server cannot reliably
    self-upgrade via bulk_upgrade while serving :8220; use archive enroll per version.
    """
    print(f"\n=== Fleet Server enroll @ {version} (local archive) ===", flush=True)
    unenroll_hostname_agents(kb, elastic_pwd, FLEET_FQDN)
    time.sleep(10)

    fleet_ip = NODES["fleet"][0]
    c = connect(fleet_ip, attempts=60)
    stage_packages(c, roles=("elastic-agent",), versions=(version,))
    c.close()

    if not reenroll_fleet_server_inplace(
        elastic_pwd,
        policy_id=FLEET_POLICY_ID,
        agent_version=version,
        fleet_ip=fleet_ip,
    ):
        return False

    agent = wait_fleet_hostname_agent(kb, elastic_pwd, FLEET_FQDN, timeout=900)
    if not agent or agent.get("agent", {}).get("version") != version:
        print(f"WARN: Fleet Server not online at {version} after enroll", flush=True)
        return False
    binary = fleet_agent_binary_version(fleet_ip)
    if binary != version:
        print(f"WARN: Fleet binary {binary} != {version}", flush=True)
        return False
    return fleet_server_healthy(fleet_ip)


def upgrade_other_agents(
    kb,
    elastic_pwd: str,
    version: str,
    source_uri: str,
) -> bool:
    agents = list_agents(kb, elastic_pwd)
    ids = [
        a["id"]
        for a in agents
        if a.get("local_metadata", {}).get("host", {}).get("hostname") != FLEET_FQDN
        and a.get("agent", {}).get("version") != version
        and a.get("status") != "unenrolled"
    ]
    if ids:
        print(f"\n=== Bulk upgrade {len(ids)} other agents -> {version} ===", flush=True)
        result = bulk_upgrade_agents(kb, elastic_pwd, version, source_uri, agent_ids=ids)
        if result:
            print(f"Fleet action (other agents): {result}", flush=True)
    else:
        print("Other agents already on target version", flush=True)

    restart_agents(("es01", "es02", "es03", "kibana"))
    return wait_agents_upgraded(kb, elastic_pwd, version, timeout=1800)


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
            f"  {a.get('agent', {}).get('version')} {a.get('status')} id={a.get('id')}",
            flush=True,
        )
    offline = [a for a in fleet_records if a.get("status") == "offline"]
    if offline:
        print(f"WARN: {len(offline)} offline ghost record(s)", flush=True)
        return False
    if len(fleet_records) != 1:
        print(f"WARN: expected 1 Fleet record, got {len(fleet_records)}", flush=True)
        return False
    return fleet_records[0].get("status") == "online"


def main() -> int:
    version = TARGET_VERSION
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    es.close()

    kb = connect(NODES["kibana"][0])
    print(f"\n=== Unenroll Fleet hostname agents before VM restore ({FLEET_FQDN}) ===", flush=True)
    print(
        "Fleet API state lives in Kibana/ES; clearing stale records before snapshot "
        "restore avoids API/binary version mismatch on bulk_upgrade.",
        flush=True,
    )
    unenroll_hostname_agents(kb, elastic_pwd, FLEET_FQDN)
    time.sleep(10)
    kb.close()

    restore_fleet_vm_snapshot()
    ensure_vm_running(FLEET_VM, 90)
    time.sleep(30)

    fleet_ip = NODES["fleet"][0]
    connect(fleet_ip, attempts=60).close()
    if not wait_fleet_server_ready(fleet_ip, max_polls=40):
        raise RuntimeError("Fleet Server not healthy after VM rollback")

    kb = connect(NODES["kibana"][0])
    fleet_agent = ensure_fleet_agent_enrolled(kb, elastic_pwd)
    api_ver = fleet_agent.get("agent", {}).get("version", "?")
    binary_ver = fleet_agent_binary_version(fleet_ip)
    print(
        f"Fleet after re-enroll: api={api_ver} binary={binary_ver} id={fleet_agent['id']}",
        flush=True,
    )
    if api_ver != binary_ver:
        raise RuntimeError(
            f"Fleet API/binary mismatch after re-enroll (api={api_ver} binary={binary_ver})"
        )
    fleet_ver = binary_ver

    for step_version in (INTERMEDIATE_VERSION, version):
        if fleet_ver == step_version:
            continue
        if step_version == INTERMEDIATE_VERSION and fleet_ver != ROLLBACK_AGENT_VERSION:
            raise RuntimeError(
                f"Cannot step fleet {fleet_ver} -> {INTERMEDIATE_VERSION}; "
                f"expected {ROLLBACK_AGENT_VERSION}"
            )
        if step_version == version and not fleet_ver.startswith(("8.18", "8.19")):
            raise RuntimeError(f"Cannot step fleet {fleet_ver} -> {version}")
        fleet_ok = enroll_fleet_server_at_version(kb, elastic_pwd, step_version)
        if not fleet_ok:
            raise RuntimeError(f"Fleet Server enroll @ {step_version} failed")
        fleet_agent = wait_fleet_hostname_agent(kb, elastic_pwd, FLEET_FQDN, timeout=120)
        fleet_ver = fleet_agent_binary_version(fleet_ip)
        print(f"Fleet after enroll @ {step_version}: binary={fleet_ver}", flush=True)

    source_uri = setup_artifact_server(fleet_ip, version)
    ensure_agent_download_source(kb, elastic_pwd, source_uri)

    if not fleet_server_healthy(fleet_ip):
        raise RuntimeError("Fleet Server not healthy before other agent upgrades")

    other_ok = upgrade_other_agents(kb, elastic_pwd, version, source_uri)
    ghosts_ok = verify_no_fleet_ghosts(kb, elastic_pwd)
    kb.close()

    print(f"\n=== Verify agents @ {version} ===", flush=True)
    verified = verify_agents(version, elastic_pwd)
    success = fleet_ok and other_ok and verified and ghosts_ok
    print(
        f"\n{'SUCCESS' if success else 'WARN'}: Fleet rollback + artifact upgrade (no reinstall)",
        flush=True,
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())